from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
VEC_FILES = (
    "chunks.jsonl", "embeddings.npy", "emb_hashes.json", "meta.json",
    "tags.json", "tag_embeddings.npy", "tag_meta.json",
)


def run(cmd: list[str]) -> None:
    print("$ " + " ".join(str(x) for x in cmd), flush=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen(
        [str(x) for x in cmd], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert p.stdout is not None
    for line in p.stdout:
        print(line, end="", flush=True)
    code = p.wait()
    if code != 0:
        raise SystemExit("命令失败（exit {}）".format(code))


def verify(stage: Path) -> None:
    import numpy as np

    chunks = stage / "chunks.jsonl"
    emb = stage / "embeddings.npy"
    manifest = stage / "emb_hashes.json"
    tags_path = stage / "tags.json"
    tag_emb = stage / "tag_embeddings.npy"
    n_chunks = sum(1 for _ in chunks.open("r", encoding="utf-8"))
    arr = np.load(emb, mmap_mode="r")
    dim = int(arr.shape[1])
    hashes = json.loads(manifest.read_text(encoding="utf-8"))
    if arr.shape[0] != n_chunks or len(hashes) != n_chunks:
        raise SystemExit(
            "校验失败：chunks={} embeddings={} manifest={}".format(
                n_chunks, arr.shape[0], len(hashes)))
    zeros = 0
    for i in range(0, arr.shape[0], 2000):
        block = np.asarray(arr[i:i + 2000])
        zeros += int((np.abs(block).sum(axis=1) == 0).sum())
    del arr
    if zeros:
        raise SystemExit("校验失败：{} 行零向量，未替换线上文件".format(zeros))

    if not (tags_path.is_file() and tag_emb.is_file()):
        raise SystemExit("校验失败：缺少 tags.json / tag_embeddings.npy")
    tags = json.loads(tags_path.read_text(encoding="utf-8"))
    tag_arr = np.load(tag_emb, mmap_mode="r")
    if tag_arr.shape != (len(tags), arr.shape[1]):
        raise SystemExit(
            "校验失败：tag 向量 {}，期望 ({}, {})".format(
                tag_arr.shape, len(tags), dim))
    print("校验通过：{} 块，{} 个 tag，0 零向量".format(
        n_chunks, len(tags)), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anime-db", default="data/anime.db")
    ap.add_argument("--moegirl-db", default="data/moegirl.db")
    ap.add_argument("--bodies", default="data/moegirl_bodies.json")
    ap.add_argument("--vec-dir", default="data/vec")
    ap.add_argument("--provider-id", default=None)
    ap.add_argument("--with-moegirl", action="store_true")
    ap.add_argument("--keep-staging", action="store_true")
    args = ap.parse_args()

    vec = Path(args.vec_dir)
    stage = vec / ".sync-tmp"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ok = False
    try:
        cmd = [
            PY, str(ROOT / "scripts" / "build" / "chunks.py"),
            "--anime-db", args.anime_db,
            "--moegirl-db", args.moegirl_db,
            "--bodies", args.bodies,
            "--out", str(stage / "chunks.jsonl"),
        ]
        if args.with_moegirl:
            cmd.append("--with-moegirl")
        run(cmd)

        for name in ("embeddings.npy", "emb_hashes.json"):
            src = vec / name
            if src.exists():
                shutil.copy2(src, stage / name)

        cmd = [
            PY, str(ROOT / "scripts" / "build" / "embeddings.py"),
            "--chunks", str(stage / "chunks.jsonl"),
            "--emb", str(stage / "embeddings.npy"),
        ]
        if args.provider_id:
            cmd.extend(["--provider-id", args.provider_id])
        run(cmd)

        cmd = [
            PY, str(ROOT / "scripts" / "build" / "tag_embeddings.py"),
            "--out", str(stage),
        ]
        if args.provider_id:
            cmd.extend(["--provider-id", args.provider_id])
        run(cmd)
        verify(stage)

        # 先把旧文件备份到 *.old，再替换；任何一步失败回滚
        for name in VEC_FILES:
            src = vec / name
            if src.exists():
                shutil.copy2(src, vec / (name + ".old"))
        for name in VEC_FILES:
            src = stage / name
            if src.exists():
                os.replace(src, vec / name)
        ok = True
        print("\n完成：{:.1f}s，已替换 {}。重启插件后加载新索引。".format(
            time.time() - t0, vec), flush=True)
    finally:
        if ok and not args.keep_staging:
            shutil.rmtree(stage, ignore_errors=True)
        elif not ok:
            print("失败，线上文件未替换；staging 保留在 {}".format(stage), flush=True)


if __name__ == "__main__":
    main()
