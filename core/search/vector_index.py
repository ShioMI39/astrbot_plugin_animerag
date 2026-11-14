from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence
from .embedder import embed_one

import numpy as np

KIND_IDS = {"work": 0, "character": 1, "episode": 2, "moegirl": 3}
ID_KINDS = {v: k for k, v in KIND_IDS.items()}

class VectorIndex:
    """内存里的向量索引。加载 search 纯 numpy"""

    def __init__(self, chunks: list[dict], E: np.ndarray,
                 kinds: np.ndarray, refs: np.ndarray, works: np.ndarray) -> None:
        self.chunks = chunks
        self.E = E
        self.kinds = kinds
        self.refs = refs
        self.works = works
        self.n = len(chunks)

    @staticmethod
    def dim_of(path: str | Path) -> int:
        """只读 npy 头，取索引维度，不加载整份向量。"""
        emb = np.load(Path(path) / "embeddings.npy", mmap_mode="r")
        return int(emb.shape[1])

    @classmethod
    def load(cls, path: str | Path, mmap: bool = False) -> "VectorIndex":
        d = Path(path)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        chunks: list[dict] = []
        with (d / "chunks.jsonl").open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    chunks.append(json.loads(line))

        emb = np.load(d / "embeddings.npy", mmap_mode="r" if mmap else None)
        if emb.shape[0] != len(chunks):
            raise ValueError(
                "embeddings 行数 {} 与 chunks 数 {} 不一致，需要重跑向量"
                .format(emb.shape[0], len(chunks)))
        dim = int(emb.shape[1])
        meta_dim = int(meta.get("dim") or 0)
        if meta_dim and meta_dim != dim:
            raise ValueError(
                "embeddings 维度 {} 与 meta.json 记录的 {} 不一致".format(dim, meta_dim))
        if not mmap:
            emb = np.asarray(emb, dtype=np.float32)

        n = len(chunks)
        kinds = np.zeros(n, dtype=np.int8)
        refs = np.zeros(n, dtype=np.int64)
        works = np.zeros(n, dtype=np.int64)
        for i, c in enumerate(chunks):
            kinds[i] = KIND_IDS.get(c.get("kind", ""), -1)
            refs[i] = c.get("ref_id") or 0
            works[i] = c.get("work_id") or 0

        idx = cls(chunks, emb, kinds, refs, works)
        idx.meta = meta
        idx.dim = dim
        return idx

    def chunks_by_ref(self, kind: str, ref_id: int, limit: int = 6) -> list[dict]:
        """取出某个实体的所有块（不需要查询向量）。"""
        kid = KIND_IDS.get(kind)
        if kid is None:
            return []
        rows = np.flatnonzero((self.kinds == kid) & (self.refs == ref_id))
        if rows.size == 0:
            return []
        # 按 chunk id 里的序号排，保证 :0（角色卡）在最前
        def seq(i: int) -> tuple:
            cid = self.chunks[int(rows[i])]["id"]
            tail = cid.rsplit(":", 1)[-1]
            return (0, int(tail)) if tail.isdigit() else (1, 0)
        order = sorted(range(rows.size), key=seq)[:limit]
        return [self.chunks[int(rows[j])] for j in order]

    def search_vec(
        self,
        qvec: Sequence[float] | np.ndarray,
        top_k: int = 10,
        kinds: Iterable[str] | None = None,
        work_id: int | None = None,
        ref_ids: Iterable[int] | None = None,
        min_score: float = 0.0,
        per_work: int | None = None,
    ) -> list[dict]:
        """单条查询向量的检索。返回 [{...chunk, "score": 余弦}, ...]。"""
        q = np.asarray(qvec, dtype=np.float32).reshape(-1)
        if q.size != self.E.shape[1]:
            raise ValueError(
                "查询向量维度 {} 与索引维度 {} 不一致".format(
                    q.size, self.E.shape[1]))
        norm = float(np.linalg.norm(q))
        if norm == 0:
            return []
        q = q / norm

        mask = np.ones(self.n, dtype=bool)
        if kinds is not None:
            wanted = [KIND_IDS[k] for k in kinds if k in KIND_IDS]
            mask &= np.isin(self.kinds, wanted)
        if work_id is not None:
            mask &= (self.works == work_id)
        if ref_ids is not None:
            mask &= np.isin(self.refs, np.asarray(list(ref_ids), dtype=np.int64))

        rows = np.flatnonzero(mask)
        if rows.size == 0:
            return []

        if rows.size < self.n * 0.1:
            sims = self.E[rows] @ q
        else:
            full = self.E @ q
            sims = full[rows]
        k = min(top_k * 3 if per_work else top_k, rows.size)
        if k < rows.size:
            part = np.argpartition(-sims, k - 1)[:k]
        else:
            part = np.arange(rows.size)
        part = part[np.argsort(-sims[part])]

        out: list[dict] = []
        seen_work: dict[int, int] = {}
        for j in part:
            s = float(sims[j])
            if s < min_score:
                break
            ch = self.chunks[int(rows[j])]
            wid = ch.get("work_id") or 0
            if per_work and wid:
                if seen_work.get(wid, 0) >= per_work:
                    continue
                seen_work[wid] = seen_work.get(wid, 0) + 1
            out.append({**ch, "score": s})
            if len(out) >= top_k:
                break
        return out

    async def search(
        self,
        query: str,
        top_k: int = 10,
        **kwargs,
    ) -> list[dict]:
        """带 query 向量化的检索。"""
        qvec = await embed_one(query)
        return self.search_vec(qvec, top_k=top_k, **kwargs)

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for k in ID_KINDS.values():
            counts[k] = int((self.kinds == KIND_IDS[k]).sum())
        return {
            "chunks": self.n,
            "kinds": counts,
            "dtype": str(self.E.dtype),
            "size_mb": round(self.E.nbytes / 2**20, 1),
            "dim": int(self.E.shape[1]),
        }
