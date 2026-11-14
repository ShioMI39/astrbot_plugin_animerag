from __future__ import annotations

import asyncio
import aiohttp

from .. import config

_SESSION: aiohttp.ClientSession | None = None
_CONN: dict | None = None

DOC_MAX_CHARS = 400

# 运行时注入的 AstrBot RerankProvider 实例。
_PROVIDER = None


def set_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


def get_provider():
    return _PROVIDER


def use_conn(conn: dict | None) -> None:
    global _CONN
    _CONN = conn


def resolve_conn() -> dict | None:
    if _CONN is not None:
        return _CONN
    return config.rerank_provider(str(config.get("rerank_provider_id") or ""))


async def _session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession()
    return _SESSION


async def close() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


async def _rerank_via_provider(query: str, docs: list[str]) -> list[tuple[int, float]]:
    """走 AstrBot 的 RerankProvider，转成 (原下标, 分数) 并按分数降序。"""
    try:
        results = await _PROVIDER.rerank(query, docs)
    except Exception:
        return []
    out = [(int(x.index), float(x.relevance_score)) for x in (results or [])]
    out.sort(key=lambda x: -x[1])
    return out


async def rerank(
    query: str,
    docs: list[str],
    model: str | None = None,
    top_n: int = 20,
    tries: int = 2,
) -> list[tuple[int, float]]:
    """对 docs 重排，返回 [(原下标, 相关度分数)]，按分数降序。"""
    if not query or not docs:
        return []
    use = docs[:top_n]
    if len(use) < 2:
        return []
    if _PROVIDER is not None:
        return await _rerank_via_provider(query, use)

    conn = resolve_conn()
    if conn is None:
        return []
    url = conn["api_base"].rstrip("/") + conn["suffix"]
    headers = {"Authorization": "Bearer " + conn["key"],
               "Content-Type": "application/json"}
    payload = {
        "model": model or conn["model"],
        "query": query,
        "documents": [d[:DOC_MAX_CHARS] for d in use],
    }
    s = await _session()
    for attempt in range(tries):
        try:
            async with s.post(url, headers=headers, json=payload,
                              timeout=aiohttp.ClientTimeout(total=40)) as r:
                if r.status == 200:
                    body = await r.json()
                    out = [(int(x["index"]), float(x["relevance_score"]))
                           for x in body.get("results") or []]
                    out.sort(key=lambda x: -x[1])
                    return out
        except Exception:
            pass
        if attempt + 1 < tries:
            await asyncio.sleep(0.8 * (attempt + 1))
    return []
