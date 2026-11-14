from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from . import embedder
from . import rerank as rr
from ..db.bgm import queries
from ..db.bgm.schema import Database
from .vector_index import VectorIndex

# 词法命中的 target_kind → 展示名
# org = 公司 / 组合（Bangumi person.type 2 和 3），不是真实人物
LEX_KINDS = ("work", "character", "person", "org")
# 没有向量块的 kind，稠密层直接跳过
NO_DENSE_KINDS = ("person", "org")
# 强命中阈值
STRONG_WEIGHT = 90
DEFAULT_MIN_SCORE = 0.55


class Retriever:
    def __init__(
        self,
        anime_db: str,
        vec_dir: str | Path,
        rerank_enabled: bool = False,
        rerank_model: str = "BAAI/bge-reranker-v2-m3",
        rerank_top_n: int = 20,
        rerank_min_score: float = 0.0,
    ) -> None:
        self.db = Database(anime_db)
        self.idx = VectorIndex.load(vec_dir)
        self._titles: dict[tuple[str, int], tuple[str, str]] = {}
        self.rerank_enabled = bool(rerank_enabled)
        self.rerank_model = rerank_model or "BAAI/bge-reranker-v2-m3"
        self.rerank_top_n = max(int(rerank_top_n or 20), 2)
        self.rerank_min_score = max(float(rerank_min_score or 0.0), 0.0)

    def _resolve(self, kind: str, target_id: int) -> tuple[str, str]:
        """(标题, 正文)。带缓存 —— 同一角色在一次会话里会被反复取。"""
        key = (kind, target_id)
        hit = self._titles.get(key)
        if hit is not None:
            return hit
        title, text = "", ""
        if kind == "character":
            row = queries.get_character(self.db.conn, target_id)
            if row:
                title = row.get("name_cn") or row.get("name") or ""
                text = row.get("summary") or ""
        elif kind == "work":
            row = queries.get_work(self.db.conn, target_id)
            if row:
                title = row.get("name_cn") or row.get("name") or ""
                text = row.get("summary") or ""
        elif kind in ("person", "org"):
            row = queries.get_person(self.db.conn, target_id)
            if row:
                title = row["name_cn"] or row["name"] or ""
                text = "声优/制作人员" if kind == "person" else "公司/组合"
        self._titles[key] = (title, text)
        return title, text

    def _lexical(self, query: str, kinds: set[str] | None, limit: int) -> list[dict[str, Any]]:
        """L1 精确 + L2 前缀/包含。按 (kind, ref_id) 去重，保留权重最高的那次命中。"""
        want = [k for k in LEX_KINDS if kinds is None or k in kinds]
        if not want:
            return []

        out: dict[tuple[str, int], dict[str, Any]] = {}
        for k in want:
            # L1：别名精确匹配
            for r in queries.search(self.db.conn, query, kind=k, limit=limit):
                key = (r["target_kind"], r["target_id"])
                cur = out.get(key)
                if cur is None or r["weight"] > cur["weight"]:
                    out[key] = {
                        "source": "lexical", "kind": r["target_kind"],
                        "ref_id": r["target_id"], "weight": r["weight"],
                        "matched_term": r["term_norm"], "exact": True,
                    }
            # L2：子串匹配
            for r in queries.search_like(self.db.conn, query, kind=k, limit=limit):
                key = (r["target_kind"], r["target_id"])
                if key in out:
                    continue
                out[key] = {
                    "source": "lexical", "kind": r["target_kind"],
                    "ref_id": r["target_id"], "weight": r["weight"],
                    "matched_term": r["term_norm"], "exact": False,
                }

        hits = sorted(out.values(), key=lambda h: (-h["weight"], not h["exact"], h["ref_id"]))
        for h in hits:
            h["title"], h["text"] = self._resolve(h["kind"], h["ref_id"])
            h["score"] = None
        return hits

    async def _dense(self, query: str, kinds: set[str] | None, top_k: int,
                     min_score: float, qv=None) -> list[dict[str, Any]]:
        if qv is None:
            qv = await embedder.embed_one(query)
        raw = self.idx.search_vec(qv, top_k=top_k, kinds=kinds, min_score=min_score)
        for h in raw:
            h["source"] = "dense"
            h["weight"] = None
            h["matched_term"] = None
            h["exact"] = False
        return raw

    async def retrieve(
        self,
        query: str,
        kinds: Iterable[str] | None = None,
        top_k: int = 10,
        min_score: float = DEFAULT_MIN_SCORE,
        force_dense: bool = False,
        qv=None,
    ) -> list[dict[str, Any]]:
        ks = set(kinds) if kinds else None
        lex = self._lexical(query, ks, limit=max(top_k, 10))

        strong = [h for h in lex if h["weight"] >= STRONG_WEIGHT and h["exact"]]
        if strong and not force_dense:
            out: list[dict[str, Any]] = []
            for h in strong[:2]:
                out.append(h)
                if h["kind"] not in ("character", "work"):
                    continue
                base = str(h.get("text") or "")
                for ch in self.idx.chunks_by_ref(h["kind"], h["ref_id"], limit=4):
                    t = ch["text"]
                    def norm(s: str) -> str:
                        return "".join(str(s).split())[:40]

                    if t and base and norm(t) and norm(t) == norm(base):
                        continue
                    out.append({
                        "source": "expand", "kind": ch["kind"],
                        "ref_id": ch["ref_id"], "title": ch["title"],
                        "text": t, "score": None, "weight": None,
                        "matched_term": None, "exact": False,
                    })
            return out[:max(top_k, 4)]

        dense_kinds = (
            None if ks is None else {k for k in ks if k not in NO_DENSE_KINDS}
        )
        dense = (
            await self._dense(query, dense_kinds, top_k, min_score, qv=qv)
            if (dense_kinds is None or dense_kinds)
            else []
        )

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for h in lex + dense:
            key = (h["kind"], h["ref_id"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)
            if len(merged) >= self.rerank_top_n:
                break

        if self.rerank_enabled and dense and len(merged) > 1:

            docs = []
            for h in merged:
                title = str(h.get("title") or "").strip()
                text = str(h.get("text") or "").strip()
                docs.append((title + " " + text).strip() if title else text)
            scored = await rr.rerank(
                query, docs, model=self.rerank_model, top_n=self.rerank_top_n
            )
            if scored:
                ordered: list[dict[str, Any]] = []
                for i, sc in scored:
                    if i >= len(merged):
                        continue
                    h = dict(merged[i])
                    h["rerank_score"] = sc
                    ordered.append(h)
                tail = [dict(h) for h in merged[len(scored):]]
                merged = ordered + tail

                if self.rerank_min_score > 0:
                    kept = [h for h in merged
                            if h.get("rerank_score") is None
                            or h["rerank_score"] >= self.rerank_min_score]
                    if kept:
                        merged = kept

        return merged[:top_k]

    def close(self) -> None:
        self.db.close()
