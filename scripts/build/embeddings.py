from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import config

BATCH = 32
CONCURRENCY = 4
MAX_TRIES = 8
BACKOFF_BASE = 8.0
TPM = 450_000
CHARS_PER_TOKEN = 1.0


class TokenBucket:
    """令牌桶限速。按估算 token 数扣费，不够就等。"""

    def __init__(self, tpm: int) -> None:
        self.rate = tpm / 60.0          # token / 秒
        self.capacity = self.rate * 5   # 允许 5 秒的突发
        self.tokens = self.capacity
        self.last = time.time()

    async def take(self, n: float) -> None:
        while True:
            now = time.time()
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep(min((n - self.tokens) / self.rate, 2.0))


def resolve_conn(provider_id: str | None) -> dict:
    """定出 embedding 连接参数：--provider-id > 插件配置 embedding_provider_id。"""
    pid = (provider_id or config.get("embedding_provider_id") or "").strip()
    if not pid:
        raise SystemExit(
            "没有可用的 Embedding Provider。两种办法：\n"
            "  ① 插件配置里填 embedding_provider_id（推荐）\n"
            "  ② 设环境变量 ANIMERAG_EMBEDDING_PROVIDER_ID=<ID>"
        )
    conn = config.embedding_provider(pid)
    if conn is None:
        raise SystemExit(
            "AstrBot 里找不到 embedding provider {!r}。\n"
            "去「服务提供商」页面确认 ID（如 openai_embedding）。".format(pid)
        )
    return conn


def read_chunks(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def resolve_dim(conn: dict, emb_path: Path, explicit: int | None) -> int:
    """确定输出维度：--dim > provider 配置 > 旧 embeddings > 1024。"""
    provider_dim = int(conn.get("dim") or 0)
    old_dim = 0
    if emb_path.exists():
        try:
            old_dim = int(np.load(emb_path, mmap_mode="r").shape[1])
        except Exception:
            old_dim = 0
    dim = int(explicit or provider_dim or old_dim or 1024)
    if explicit and provider_dim and explicit != provider_dim:
        raise SystemExit("--dim {} 与 provider 维度 {} 不一致".format(explicit, provider_dim))
    if old_dim and old_dim != dim:
        print("  旧向量维度 {} != 目标 {}，不复用旧向量".format(old_dim, dim), flush=True)
    return dim


def load_old(emb_path: Path, manifest_path: Path, dim: int) -> dict[str, np.ndarray]:
    """上一次的 hash -> 向量。用于增量复用。"""
    if not emb_path.exists() or not manifest_path.exists():
        return {}
    try:
        hashes = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_emb = np.load(emb_path, mmap_mode="r")
        if len(hashes) != old_emb.shape[0]:
            print("  hash 清单 {} 条 与向量 {} 行不一致，放弃复用".format(
                len(hashes), old_emb.shape[0]), flush=True)
            return {}
        out: dict[str, np.ndarray] = {}
        for i, h in enumerate(hashes):
            if not h:
                continue
            v = np.array(old_emb[i], dtype=np.float32)
            if v.shape == (dim,) and np.any(v):
                out[h] = v
        del old_emb
        return out
    except Exception as e:
        print("  读取上次结果失败（{}），放弃复用".format(e), flush=True)
        return {}


async def embed_batch(session: aiohttp.ClientSession, base: str, headers: dict,
                      model: str, texts: list[str], sem: asyncio.Semaphore,
                      log, bucket: "TokenBucket") -> np.ndarray | None:
    await bucket.take(sum(len(t) for t in texts) / CHARS_PER_TOKEN + 8 * len(texts))
    payload = {"model": model, "input": texts, "encoding_format": "float"}
    for attempt in range(MAX_TRIES):
        try:
            async with sem:
                async with session.post(
                    base + "/embeddings", headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as r:
                    if r.status == 200:
                        body = await r.json()
                        data = sorted(body["data"], key=lambda d: d["index"])
                        return np.array([d["embedding"] for d in data], dtype=np.float32)
                    txt = (await r.text())[:160]
                    if r.status == 429:
                        wait = BACKOFF_BASE * (2 ** attempt)
                        log("    429 限流，等 {:.0f}s（第 {} 次）".format(wait, attempt + 1))
                        await asyncio.sleep(wait)
                        continue
                    log("    HTTP {}  {}".format(r.status, txt))
        except Exception as e:
            log("    {}: {}".format(type(e).__name__, str(e)[:100]))
        await asyncio.sleep(1.5 * (1 + attempt))
    return None


async def run(args) -> None:
    def log(s: str) -> None:
        print(s, flush=True)

    conn = resolve_conn(getattr(args, "provider_id", None))
    base = conn["api_base"].rstrip("/")
    headers = {"Authorization": "Bearer " + conn["key"],
               "Content-Type": "application/json"}
    model = conn["model"]
    log("模型 {}  （{}）".format(model, base))

    chunks_path = Path(args.chunks)
    emb_path = Path(args.emb)
    vectors = read_chunks(chunks_path)
    if args.limit:
        vectors = vectors[:args.limit]
    n = len(vectors)
    log("待处理 {:,} 块".format(n))

    dim = resolve_dim(conn, emb_path, getattr(args, "dim", None))
    log("向量维度 {}（provider dim={}）".format(dim, conn.get("dim") or "未知"))

    manifest_path = emb_path.parent / "emb_hashes.json"
    meta_path = emb_path.parent / "meta.json"
    old_meta = {}
    if meta_path.exists():
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            old_meta = {}
    old = {}
    if not args.rebuild:
        if old_meta.get("embed_model") and old_meta["embed_model"] != model:
            log("模型从 {} 换成 {}，不复用旧向量".format(
                old_meta["embed_model"], model))
        else:
            old = load_old(emb_path, manifest_path, dim)
    log("上次结果 {:,} 条可复用".format(len(old)))

    tmp_path = emb_path.with_suffix(".tmp.npy")
    if tmp_path.exists():
        tmp_path.unlink()
    arr = np.lib.format.open_memmap(
        tmp_path, mode="w+", dtype=np.float32, shape=(n, dim))

    todo: list[int] = []
    reuse = 0
    for i, c in enumerate(vectors):
        v = old.get(c.get("hash") or "")
        if v is not None and v.shape == (dim,):
            arr[i] = v
            reuse += 1
        else:
            todo.append(i)
    arr.flush()
    log("  复用 {:,} 块，需重算 {:,} 块".format(reuse, len(todo)))

    if not todo:
        arr.flush()
        del arr
        tmp_path.replace(emb_path)
        write_manifest(manifest_path, vectors)
        log("全部命中缓存，完成")
        write_meta(chunks_path.parent, chunks_path, emb_path, n, reuse, 0, model, dim)
        return

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    log("  分 {:,} 批（每批 {}），并发 {}".format(len(batches), BATCH, CONCURRENCY))

    sem = asyncio.Semaphore(CONCURRENCY)
    bucket = TokenBucket(TPM)
    done = {"n": 0, "fail": 0}
    t0 = time.time()

    async with aiohttp.ClientSession() as session:
        async def one(batch: list[int]) -> None:
            texts = [vectors[i]["embed"] for i in batch]
            v = await embed_batch(session, base, headers, model, texts, sem, log, bucket)
            if v is None:
                done["fail"] += len(batch)
                log("    批失败（{} 块）".format(len(batch)))
                return
            if v.shape != (len(batch), dim):
                done["fail"] += len(batch)
                log("    维度异常 {}".format(v.shape))
                return
            for k, i in enumerate(batch):
                arr[i] = v[k]
            done["n"] += len(batch)
            if done["n"] % 4000 < BATCH:
                el = time.time() - t0
                rate = done["n"] / max(el, 0.01)
                log("    {:,}/{:,}  {:.0f} 块/秒  剩 {:.0f} 分钟".format(
                    done["n"], len(todo), rate, (len(todo) - done["n"]) / max(rate, 0.01) / 60))

        await asyncio.gather(*(one(b) for b in batches))

    arr.flush()
    del arr
    tmp_path.replace(emb_path)
    write_manifest(manifest_path, vectors)
    el = time.time() - t0
    log("\n完成 {:.0f}s   成功 {:,}  失败 {:,}".format(el, done["n"], done["fail"]))
    write_meta(chunks_path.parent, chunks_path, emb_path, n, reuse, done["n"], model, dim)

    if done["fail"]:
        log("⚠️ 有 {} 块失败，重跑本脚本会只补这些（增量按 hash 比对）".format(done["fail"]))


def write_manifest(path: Path, vectors: list[dict]) -> None:
    """按行序存 hash 清单。"""
    path.write_text(
        json.dumps([c.get("hash") or "" for c in vectors], ensure_ascii=False),
        encoding="utf-8")


def write_meta(out_dir: Path, chunks_path: Path, emb_path: Path,
               n: int, reused: int, computed: int, model: str, dim: int) -> None:
    meta_path = out_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update({
        "embed_model": model,
        "dim": dim,
        "total": n,
        "embeddings_built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "embeddings_reused": reused,
        "embeddings_computed": computed,
        "size_mb": round(emb_path.stat().st_size / 2**20, 1),
    })
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=str(config.vec_dir() / "chunks.jsonl"))
    ap.add_argument("--emb", default=str(config.vec_dir() / "embeddings.npy"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dim", type=int, default=None,
                    help="向量维度；留空按 provider 配置/旧向量推断")
    ap.add_argument("--rebuild", action="store_true", help="忽略上次结果，全量重算")
    ap.add_argument(
        "--provider-id",
        default=None,
        help="AstrBot 的 embedding provider ID。留空则读插件配置 embedding_provider_id",
    )
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
