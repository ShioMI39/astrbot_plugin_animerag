from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import aiohttp

from pathlib import Path
from core import config
from core.db.translate import schema as tr
from core.db.translate import queries as tr_queries
from core.db.bgm import queries
from core.db.bgm.schema import Database

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


CONCURRENCY = 10     # 并发数
COMMIT_EVERY = 20    # 每译提交一次
MAX_TRIES = 3


SUMMARY_PROMPT = (
    "你是日文到简体中文的翻译。只输出译文，不要解释、不要加引号。"
    "专有名词（人名、作品名、组织名）用通用的中文译名；没有通用译名时音译，"
    "并可在首次出现时用括号保留日文原名。"
    "如果原文已经是中文（或中英日混排），已有的中文原样保留，只翻译其中的日文部分。"
    "只有当某段日文的信息在中文里已经完整出现时才可省略；"
    "如果日文包含中文没提到的信息，必须完整翻译出来接在后面，"
    "不要因为主题相近就整段丢弃。"
    "保持原文的语气和长度，不要扩写。"
)

WORK_PROMPT = (
    "你是日文到简体中文的动画作品简介翻译。只输出译文，不要解释、不要加引号。"
    "专有名词（人名、作品名、公司名、组织名）用通行的中文译名；"
    "没有通行译名时音译，并可在首次出现时用括号保留日文原名。"
    "原文里的英文单词要译成中文，不要混在译文里；"
    "但 OVA、OAD、TV、VR、CV、OP、ED、BD 这类业界通用缩写保留原样。"
    "用假名写的作品名：有通行中文译名就用译名，确实没有就音译成中文，不要留假名。"
    "简介里的播出时间、制作人员、集数、每集时长等信息要完整保留，不要省略或合并。"
    "如果原文已经是中文（或中日混排），已有的中文原样保留，只翻译其中的日文部分。"
    "只有当某段日文的信息在中文里已经完整出现时才可省略；"
    "如果日文包含中文没提到的信息，必须完整翻译出来接在后面，"
    "不要因为主题相近就整段丢弃。"
    "保持原文的语气和长度，不要扩写、不要加评论。"
)

PROMPTS = {"summary": SUMMARY_PROMPT, "work": WORK_PROMPT}
SYSTEM_PROMPT = SUMMARY_PROMPT       # 默认（角色/单集）


def prompt_for(kind: str) -> str:
    """按翻译层选 prompt。"""
    return WORK_PROMPT if kind == "work" else SUMMARY_PROMPT


# DeepSeek 价格（元 / 百万 token），估算成本
PRICE_IN = 2.0
PRICE_OUT = 8.0

def resolve_engine(provider_id: str | None) -> tuple[dict, str]:
    """定出 (连接参数, 来源说明)：--provider-id > 插件配置 translate_provider_id。"""
    pid = (provider_id or config.get("translate_provider_id") or "").strip()
    if not pid:
        raise SystemExit(
            "没有可用的翻译模型。请在：\n"
            "插件配置里填 translate_provider_id\n"
            "ID 是 AstrBot「服务提供商」里 chat_completion 条目的 id（形如 deepseek/deepseek-flash）"
        )
    conn = config.chat_provider(pid)
    if conn is None:
        raise SystemExit(
            "在 AstrBot 里找不到 provider {!r}。\n"
            "去「服务提供商」页面确认 ID（形如 deepseek/deepseek-flash）。".format(pid)
        )
    return conn, "AstrBot provider {}".format(pid)


class Translator:
    def __init__(self, session: aiohttp.ClientSession, conn: dict) -> None:
        self.session = session
        self.base = conn["api_base"].rstrip("/").removesuffix("/chat/completions")
        self.key = conn["key"]
        self.model = conn["model"]
        self.tokens_in = 0
        self.tokens_out = 0

    @property
    def cost(self) -> float:
        return (self.tokens_in / 1e6 * PRICE_IN
                + self.tokens_out / 1e6 * PRICE_OUT)

    async def translate(self, text: str, prompt: str | None = None) -> str | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt or SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "max_tokens": max(1024, min(8192, int(len(text) * 4))),
        }
        for attempt in range(MAX_TRIES):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            try:
                async with self.session.post(
                    self.base + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    if r.status == 429 or r.status >= 500:
                        continue
                    body = await r.json()
                    if r.status != 200:
                        print("      HTTP {}: {}".format(
                            r.status, json.dumps(body, ensure_ascii=False)[:200]))
                        return None
                    usage = body.get("usage") or {}
                    self.tokens_in += usage.get("prompt_tokens", 0)
                    self.tokens_out += usage.get("completion_tokens", 0)
                    choice = body["choices"][0]
                    out = (choice["message"].get("content") or "").strip()
                    if out:
                        return out
                    if choice.get("finish_reason") == "length" and payload["max_tokens"] < 8192:
                        payload["max_tokens"] = min(8192, payload["max_tokens"] * 2)
                        continue
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                continue
        return None


async def run(
    anime_db: str,
    translate_db: str,
    kinds: list[str],
    limit: int | None,
    force: bool,
    dry_run: bool,
    log=print,
    provider_id: str | None = None,
    concurrency: int = CONCURRENCY,
) -> None:
    conn_info, source = resolve_engine(provider_id)
    engine = conn_info["model"]

    db = Database(anime_db)
    conn = tr.open_db(translate_db)

    plan: list[tuple[str, list[dict]]] = []
    for kind in kinds:
        rows = [
            {"ref_id": r["ref_id"], "text": r["text"], "label": r["label"] or ""}
            for r in queries.get_translation_sources(db.conn, kind)
        ]
        todo = tr_queries.pending(conn, kind, rows, force=force)
        plan.append((kind, todo))
        log("   {:<10} 有文本 {:>7,}   待译 {:>7,}  ({:,} 字符)".format(
            kind, len(rows), len(todo), sum(len(r["text"]) for r in todo)))
    db.close()

    total_todo = sum(len(t) for _, t in plan)
    total_chars = sum(len(r["text"]) for _, t in plan for r in t)
    s = tr_queries.stats(conn)
    log("")
    log("模型 {}  （{}）".format(engine, source))
    log("已完成 {:,} 条（库内源字符 {:,}）".format(s["total"], s["source_chars"]))
    log("本次待译 {:,} 条 / {:,} 字符".format(total_todo, total_chars))

    if limit:
        plan = [(k, t[:limit]) for k, t in plan]
        total_todo = sum(len(t) for _, t in plan)
        total_chars = sum(len(r["text"]) for _, t in plan for r in t)
        log("--limit {} -> 本次实际译 {:,} 条 / {:,} 字符".format(
            limit, total_todo, total_chars))

    # 粗估：1 token ≈ 1.5 汉字，中文输出约原文的 0.7 倍 token
    # 单价按 DeepSeek 算，换模型只作数量级参考
    est_in = total_chars / 1.5
    est_out = est_in * 0.7
    log("预估成本 约 {:.2f} 元（按 DeepSeek 单价，换模型仅供参考）".format(
        est_in / 1e6 * PRICE_IN + est_out / 1e6 * PRICE_OUT))

    if dry_run:
        log("--dry-run：不做实际翻译")
        conn.close()
        return
    if not total_todo:
        log("没有待译条目")
        conn.close()
        return

    t0 = time.time()
    grand_ok = grand_fail = 0

    async with aiohttp.ClientSession() as session:
        asst = Translator(session, conn_info)

        for kind, todo in plan:
            if not todo:
                log("\n[{}] 无待译条目".format(kind))
                continue
            log("\n[{}] 开始，{} 条".format(kind, len(todo)))
            stats = {"ok": 0, "fail": 0}
            sem = asyncio.Semaphore(max(1, min(int(concurrency), 20)))

            async def one(row: dict, kind=kind, stats=stats, sem=sem) -> None:
                async with sem:
                    dst = await asst.translate(row["text"], prompt_for(kind))
                if not dst:
                    stats["fail"] += 1
                    log("   ✗ [{}#{}] {}".format(kind, row["ref_id"], row["label"][:24]))
                    return
                tr.put(conn, kind, row["ref_id"], row["text"], dst, engine)
                stats["ok"] += 1
                done = stats["ok"] + stats["fail"]
                if done % COMMIT_EVERY == 0:
                    conn.commit()
                    rate = done / max(1e-6, time.time() - t0)
                    log("   {}/{}  ok={} fail={}  {:.2f} 条/秒  累计 {:.2f} 元".format(
                        done, len(todo), stats["ok"], stats["fail"], rate, asst.cost))

            await asyncio.gather(*(one(r) for r in todo))
            conn.commit()
            log("   {} 完成：ok={} fail={}".format(kind, stats["ok"], stats["fail"]))
            grand_ok += stats["ok"]
            grand_fail += stats["fail"]

    s = tr_queries.stats(conn)
    log("")
    log("全部完成，耗时 {:.0f}s  ok={} fail={}".format(
        time.time() - t0, grand_ok, grand_fail))
    log("实际用量: 输入 {:,} / 输出 {:,} token  约 {:.2f} 元".format(
        asst.tokens_in, asst.tokens_out, asst.cost))
    log("库内累计 {:,} 条: {}".format(
        s["total"],
        "  ".join("{}={:,}".format(k, v["n"]) for k, v in sorted(s["by_kind"].items())),
    ))
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anime-db", default=str(config.anime_db()))
    ap.add_argument("--translate-db", default=str(config.translate_db()))
    ap.add_argument(
        "--kind", choices=tr.KINDS, action="append",
        help="只处理指定层；可重复。默认三层全跑",
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="忽略已有译文，全部重译")
    ap.add_argument("--dry-run", action="store_true", help="只统计不翻译")
    ap.add_argument(
        "--concurrency", type=int, default=CONCURRENCY,
        help="并发请求数，默认 {}；DeepSeek 建议 8-12，最高 20".format(CONCURRENCY),
    )
    ap.add_argument(
        "--provider-id",
        default=None,
        help="AstrBot 的 chat_completion provider ID（形如 deepseek/deepseek-flash）。"
             "留空则读插件配置 translate_provider_id",
    )
    args = ap.parse_args()
    kinds = args.kind or list(tr.KINDS)
    asyncio.run(run(args.anime_db, args.translate_db, kinds, args.limit,
                    args.force, args.dry_run, lambda m: print(m, flush=True),
                    provider_id=args.provider_id,
                    concurrency=args.concurrency))


if __name__ == "__main__":
    main()
