from __future__ import annotations

import unicodedata
from typing import Any, Iterable

from ..db.bgm import queries
from ..db.bgm.schema import Database
from ..normalize import to_simplified
from .keywords import GENERIC_TRAITS

STRONG_WEIGHT = 90
MIN_POPULARITY = {"character": 50, "work": 500, "person": 3, "org": 3}
MAX_CANDIDATES = 5
MAX_TERM = 12
MIN_SCAN_COVERAGE = 0.5

_LEX: dict[str, tuple[dict[str, list[tuple[str, int, int]]], int]] = {}

_SCAN_SKIP = {
    "主角", "配角", "客串", "闲角", "旁白", "路人", "我", "你", "他", "她", "它",
} | set(GENERIC_TRAITS)


def _popularity(db: Database, kind: str, ref_id: int) -> int:
    if kind == "character":
        return queries.get_character_popularity(db.conn, ref_id)
    if kind == "work":
        return queries.get_work_popularity(db.conn, ref_id)
    if kind in ("person", "org"):
        return queries.get_person_popularity(db.conn, ref_id)
    return 0


def _title(db: Database, kind: str, ref_id: int) -> str:
    if kind == "character":
        row = queries.get_character(db.conn, ref_id)
        return (row.get("name_cn") or row.get("name") or "") if row else ""
    if kind == "work":
        row = queries.get_work(db.conn, ref_id)
        return (row.get("name_cn") or row.get("name") or "") if row else ""
    if kind in ("person", "org"):
        return queries.get_person_title(db.conn, ref_id)
    return ""


def _lexicon(db: Database) -> tuple[dict[str, list[tuple[str, int, int]]], int]:
    key = str(db.path)
    st = _LEX.get(key)
    if st is not None:
        return st
    terms: dict[str, list[tuple[str, int, int]]] = {}
    for term, kind, ref_id, weight in queries.search_terms(db.conn, MAX_TERM):
        terms.setdefault(term, []).append((kind, int(ref_id), int(weight)))
    st = (terms, MAX_TERM)
    _LEX[key] = st
    return st


def _pick(
    db: Database,
    items: dict[tuple[str, int], dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    for item in items.values():
        item["title"] = _title(db, item["kind"], item["ref_id"])
        item["popularity"] = _popularity(db, item["kind"], item["ref_id"])

    by_weight = sorted(
        items.values(),
        key=lambda x: (-x["weight"], -x["popularity"], x["ref_id"]),
    )[:3]
    by_pop = sorted(
        items.values(),
        key=lambda x: (-x["popularity"], -x["weight"], x["ref_id"]),
    )[:2]

    picked: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in by_weight + by_pop:
        key = (item["kind"], item["ref_id"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
    for item in sorted(
        items.values(),
        key=lambda x: (-x["weight"], -x["popularity"], x["ref_id"]),
    ):
        if len(picked) >= limit:
            break
        key = (item["kind"], item["ref_id"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
    picked = picked[:limit]
    confident = any(
        c["weight"] >= STRONG_WEIGHT
        and c["popularity"] >= MIN_POPULARITY.get(c["kind"], 0)
        and (c.get("exact") or float(c.get("coverage") or 0.0) >= MIN_SCAN_COVERAGE)
        for c in picked
    )
    return picked, confident


def _add(
    items: dict[tuple[str, int], dict[str, Any]],
    kind: str,
    ref_id: int,
    weight: int,
    wanted: set[str] | None,
    exact: bool,
    coverage: float = 1.0,
) -> None:
    if wanted is not None and kind not in wanted:
        return
    key = (kind, ref_id)
    cur = items.get(key)
    if cur is None:
        items[key] = {
            "kind": kind, "ref_id": ref_id, "weight": weight,
            "exact": exact, "coverage": coverage,
        }
        return
    if exact:
        cur["exact"] = True
        cur["coverage"] = 1.0
    elif coverage > cur["coverage"]:
        cur["coverage"] = coverage
    if weight > cur["weight"]:
        cur["weight"] = weight


def scan(
    db: Database,
    query: str,
    kinds: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """在 query 里扫描 search_index 已知名称（长句子里的实体名）。"""
    text = unicodedata.normalize("NFKC", to_simplified(query or "")).lower()
    if not text:
        return []
    terms, maxlen = _lexicon(db)
    wanted = set(kinds) if kinds else None
    items: dict[tuple[str, int], dict[str, Any]] = {}
    i = 0
    while i < len(text):
        hit = None
        for length in range(min(maxlen, len(text) - i), 1, -1):
            term = text[i:i + length]
            if term in terms:
                hit = (term, terms[term])
                break
        if hit is None:
            i += 1
            continue
        term, refs = hit
        if term in _SCAN_SKIP:
            i += len(term)
            continue
        for kind, ref_id, weight in refs:
            _add(items, kind, ref_id, weight, wanted, exact=False,
                 coverage=len(term) / len(text))
        i += len(term)
    return list(items.values())


def find(
    db: Database,
    query: str,
    kinds: Iterable[str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> tuple[list[dict[str, Any]], bool]:
    """整个 query 精确查 + query 内名称扫描，合并去重。"""
    q = (query or "").strip()
    if not q:
        return [], False
    wanted = set(kinds) if kinds else None
    items: dict[tuple[str, int], dict[str, Any]] = {}
    for r in queries.search(db.conn, q, limit=50):
        _add(items, r["target_kind"], int(r["target_id"]), int(r["weight"]),
             wanted, exact=True)
    for r in scan(db, q, kinds=wanted):
        _add(items, r["kind"], r["ref_id"], r["weight"], wanted, exact=False,
             coverage=float(r.get("coverage") or 0.0))
    return _pick(db, items, limit)
