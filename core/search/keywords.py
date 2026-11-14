from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..db.moe import attrs
from ..db.moe import queries as moe_queries
from ..normalize import to_simplified

KEY_ORDER = ("萌点", "发色", "瞳色", "所属团体")
MAX_TERM = 12

# 这些「属性」太泛，不能当角色检索条件
# 「主角 + 可爱」的属性查询，把真正的语义描述挤掉。
GENERIC_TRAITS = {
    "主角", "配角", "客串", "闲角", "旁白",
    "人类", "学生", "高中生", "中学生", "小学生", "大学生", "老师", "教师",
    "姐姐", "妹妹", "哥哥", "弟弟", "姐妹", "兄弟",
    "女性", "男性", "少女", "少年", "孩子", "大人",
    "朋友", "同伴", "同学", "双胞胎", "三胞胎", "父母", "母亲", "父亲",
}

_SPLIT_RE = re.compile(r"[、,，/／;；\s]+")

_HAIR = ("头发", "頭髮", "毛发", "毛髮", "发", "髮", "毛")
_EYE = ("眼睛", "眼瞳", "瞳", "眼", "眸", "目")
_COLOR_SUFFIX = ("色系", "颜色", "色")

_COLORS = sorted(attrs.COLOR_GROUPS, key=len, reverse=True)

_CACHE: dict[int, dict[str, Any]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _build(conn) -> dict[str, Any]:
    lex: dict[str, set[tuple[str, str]]] = {}
    token_norms: dict[tuple[str, str], set[str]] = {}

    def add(term: str, key: str, norm: str) -> None:
        lex.setdefault(term, set()).add((key, norm))

    for key in KEY_ORDER:
        for (norm,) in moe_queries.distinct_norms(conn, key):
            if not norm:
                continue
            text = norm.strip()
            if 2 <= len(text) <= MAX_TERM:
                add(text, key, text)
            for token in _SPLIT_RE.split(text):
                token = token.strip()
                if 2 <= len(token) <= MAX_TERM:
                    add(token, key, text)
                    token_norms.setdefault((key, token), set()).add(text)

    return {"lex": lex, "token_norms": token_norms}


def _state(conn) -> dict[str, Any]:
    key = id(conn)
    st = _CACHE.get(key)
    if st is None or st.get("conn") is not conn:
        st = _build(conn)
        st["conn"] = conn
        _CACHE[key] = st
    return st


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", to_simplified(text or "")).lower()


def _color_word_at(q: str, i: int) -> tuple[str, int] | None:
    """返回 (色系, 结束位置)，不处理后缀。"""
    for term in _COLORS:
        if len(term) > 1 and q.startswith(term, i):
            return attrs.COLOR_GROUPS[term], i + len(term)
    if q[i] not in attrs.COLOR_GROUPS:
        return None
    j = i
    while j < len(q) and q[j] in attrs.COLOR_GROUPS:
        j += 1
    return attrs.COLOR_GROUPS[q[i]], j


def _color_hit(q: str, i: int) -> tuple[list[tuple[str, str, bool]], int] | None:
    got = _color_word_at(q, i)
    if got is None:
        return None
    group, j = got
    for suf in _COLOR_SUFFIX:
        if q.startswith(suf, j):
            j += len(suf)
            break
    for suf in _HAIR:
        if q.startswith(suf, j):
            return [("发色", group, True)], j + len(suf)
    for suf in _EYE:
        if q.startswith(suf, j):
            return [("瞳色", group, True)], j + len(suf)
    return [("发色", group, True), ("瞳色", group, True)], j


def extract(conn, query: str) -> list[dict[str, Any]]:
    """返回 [{kid, key, target, label, group}]。"""
    q = _norm(query)
    if not q:
        return []
    st = _state(conn)
    lex: dict[str, set[tuple[str, str]]] = st["lex"]
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str, bool]] = set()
    kid = 0
    i = 0
    while i < len(q):
        color = _color_hit(q, i)
        if color is not None:
            pairs, ni = color
            for key, target, group in pairs:
                item = (kid, key, target, group)
                if item not in seen:
                    seen.add(item)
                    out.append({
                        "kid": kid, "key": key, "target": target,
                        "label": key + "=" + target, "group": group,
                    })
            kid += 1
            i = max(ni, i + 1)
            continue

        hit = None
        for length in range(min(MAX_TERM, len(q) - i), 1, -1):
            term = q[i:i + length]
            if term in lex:
                hit = (term, lex[term])
                break
        if hit is None:
            i += 1
            continue
        term, pairs = hit
        for key, target in sorted(pairs):
            item = (kid, key, target, False)
            if item not in seen:
                seen.add(item)
                out.append({
                    "kid": kid, "key": key, "target": target,
                    "label": key + "=" + target, "group": False,
                })
        kid += 1
        i += len(term)
    return out


def _rows_in(conn, key: str, target: str, group: bool) -> list:
    if group:
        values = attrs.values_in_group(conn, key, target)
        return moe_queries.chars_by_raw_values(conn, key, values)

    st = _state(conn)
    norms = st["token_norms"].get((key, target)) or {target}
    if len(norms) > 500:
        norms = set(list(norms)[:500])
    return moe_queries.chars_by_norms(conn, key, norms)


def search(
    conn, query: str, ignore: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (关键词列表, 角色命中列表)。

    ignore: 不参与检索的属性值（泛化词、角色定位等）。
    角色命中：[{char_id, matched, labels: [str]}]，按 matched 降序。
    """
    keywords = extract(conn, query)
    if ignore:
        keywords = [k for k in keywords if k["target"] not in ignore]
    if not keywords:
        return [], []

    kids: dict[int, set[int]] = {}
    labels: dict[int, set[str]] = {}
    for kw in keywords:
        for row in _rows_in(conn, kw["key"], kw["target"], kw["group"]):
            cid = int(row[0])
            kids.setdefault(cid, set()).add(kw["kid"])
            labels.setdefault(cid, set()).add(kw["label"])

    rows = [
        {"char_id": cid, "matched": len(kids[cid]), "labels": sorted(labels[cid])}
        for cid in kids
    ]
    rows.sort(key=lambda r: (-r["matched"], r["char_id"]))
    return keywords, rows
