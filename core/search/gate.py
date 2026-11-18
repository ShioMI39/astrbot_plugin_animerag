from __future__ import annotations

from typing import Any, Iterable

MAX_QUERY_CHARS = 20
MAX_LEFTOVER = 2
MIN_UNION = 3
MIN_UNION_SINGLE = 20
BEST_RATIO = 0.6

STOP_CHARS = frozenset("的了是在和与又很都也一个有点这那些个之其为怎什吗呢吧啊")


def _need_match(n: int, ratio: float = BEST_RATIO) -> int:
    """头部候选至少要命中几个条件。1~2 个条件要求全中，再多就按比例放。"""
    if n <= 2:
        return n
    return int(n * ratio + 0.999)


def _covered_mask(norm_query: str, keywords: Iterable[dict[str, Any]]) -> bytearray:
    """按 span 涂出被属性词覆盖的字符。"""
    n = len(norm_query)
    covered = bytearray(n)
    for kw in keywords:
        span = kw.get("span")
        if not span:
            continue
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        start = max(0, min(start, n))
        end = max(start, min(end, n))
        covered[start:end] = b"\x01" * (end - start)
    return covered


def evaluate(
    norm_query: str,
    keywords: Iterable[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    *,
    max_query_chars: int = MAX_QUERY_CHARS,
    max_leftover: int = MAX_LEFTOVER,
    min_union: int = MIN_UNION,
    min_union_single: int = MIN_UNION_SINGLE,
    best_ratio: float = BEST_RATIO,
) -> dict[str, Any]:
    """判定属性层能不能短路语义层。"""
    norm_query = norm_query or ""
    keywords = list(keywords or ())
    rows = list(rows or ())

    n = len(norm_query)
    covered = _covered_mask(norm_query, keywords)
    content = [
        i for i, ch in enumerate(norm_query)
        if ch not in STOP_CHARS and not ch.isspace()
    ]
    leftover = sum(1 for i in content if not covered[i])
    coverage = 1.0 - (leftover / len(content)) if content else 0.0

    distinct = len({(k["key"], k["target"]) for k in keywords})
    spans = len({tuple(k["span"]) for k in keywords if k.get("span")})
    effective = 1 if spans == 1 else distinct
    need = _need_match(effective, best_ratio)

    union = len(rows)
    best = int(rows[0].get("matched") or 0) if rows else 0

    reasons: list[str] = []
    if not norm_query:
        reasons.append("empty")
    if not keywords:
        reasons.append("no_keyword")
    if n > max_query_chars:
        reasons.append("long")
    if leftover > max_leftover:
        reasons.append("leftover")
    floor = min_union_single if effective <= 1 else min_union
    if union < floor:
        reasons.append("narrow")
    if best < need:
        reasons.append("best")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "length": n,
        "leftover": leftover,
        "coverage": round(coverage, 3),
        "distinct": distinct,
        "spans": spans,
        "need": need,
        "union": union,
        "best": best,
    }
