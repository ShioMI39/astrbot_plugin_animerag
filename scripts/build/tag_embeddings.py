from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from core import config
from core.search import embedder
from core.search import tags as tag_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def run(out_dir: Path, provider_id: str | None = None) -> None:
    if provider_id:
        conn = config.embedding_provider(provider_id)
        if conn is None:
            raise SystemExit("找不到 embedding provider: {}".format(provider_id))
        embedder.use_conn(conn)
    names = list(tag_mod.GENRE_TAGS)
    texts = [
        "{}：{}".format(name, tag_mod.GLOSSES.get(name, tag_mod.TAG_TEMPLATE.format(name)))
        for name in names
    ]
    parts = []
    for i in range(0, len(texts), 32):
        parts.append(await embedder.embed(texts[i:i + 32]))
    import numpy as np
    vec = np.vstack(parts)
    emb_path = out_dir / "embeddings.npy"
    if emb_path.exists():
        old_dim = int(np.load(emb_path, mmap_mode="r").shape[1])
        if vec.shape[1] != old_dim:
            raise SystemExit(
                "embedding 维度 {} 与主向量库 {} 不一致；"
                "tag 向量必须和主向量同模型同维度".format(
                    vec.shape[1], old_dim))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tags.json").write_text(
        json.dumps(names, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    np.save(out_dir / "tag_embeddings.npy", vec.astype("float32"))
    conn = embedder.resolve_conn() or {}
    meta = {
        "embed_model": conn.get("model") or "",
        "dim": int(vec.shape[1]),
        "total": len(names),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out_dir / "tag_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("完成：{} 个 tag -> {}（{} x {}）".format(
        len(names), out_dir, vec.shape[0], vec.shape[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.vec_dir()))
    ap.add_argument("--provider-id", default=None)
    args = ap.parse_args()
    asyncio.run(run(Path(args.out), args.provider_id))
    asyncio.run(embedder.close())


if __name__ == "__main__":
    main()
