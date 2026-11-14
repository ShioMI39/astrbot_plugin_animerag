from __future__ import annotations

import asyncio
import aiohttp
import numpy as np

from .. import config

# 默认维度；实际维度由向量库/模型在启动时注入
DEFAULT_EMBED_DIM = 1024
_DIM: int | None = None

_SESSION: aiohttp.ClientSession | None = None
_CONN: dict | None = None

_PROVIDER = None


def set_dim(dim: int | None) -> None:
    """设置当前应使用的向量维度；None 表示等第一次 embedding 结果推断。"""
    global _DIM
    _DIM = int(dim) if dim else None


def get_dim() -> int:
    return _DIM or DEFAULT_EMBED_DIM


def set_provider(provider) -> None:
    """注入 AstrBot 的 EmbeddingProvider。"""
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
    return config.embedding_provider(
        str(config.get("embedding_provider_id") or "")
    )


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


async def embed(texts: list[str], tries: int = 3) -> np.ndarray:
    """批量向量化，返回 (N, DIM) float32，已 L2 归一化。"""
    if not texts:
        return np.zeros((0, get_dim()), dtype=np.float32)
    if _PROVIDER is not None:
        return _check_dim(await _embed_via_provider(texts))

    conn = resolve_conn()
    if conn is None:
        raise RuntimeError(
            "没有可用的 Embedding Provider。"
            "请在插件配置里填 embedding_provider_id（AstrBot「服务提供商」里的 ID）"
        )
    url = conn["api_base"].rstrip("/") + "/embeddings"
    headers = {"Authorization": "Bearer " + conn["key"],
               "Content-Type": "application/json"}
    payload = {"model": conn["model"], "input": texts, "encoding_format": "float"}
    s = await _session()
    last = ""
    for attempt in range(tries):
        try:
            async with s.post(url, headers=headers, json=payload,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status == 200:
                    body = await r.json()
                    data = sorted(body["data"], key=lambda d: d["index"])
                    v = np.array([d["embedding"] for d in data], dtype=np.float32)
                    norms = np.linalg.norm(v, axis=1, keepdims=True)
                    norms[norms == 0] = 1.0
                    return _check_dim(v / norms)
                last = "HTTP {} {}".format(r.status, (await r.text())[:120])
        except Exception as e:
            last = "{}: {}".format(type(e).__name__, str(e)[:120])
        await asyncio.sleep(1.0 * (1 + attempt))
    raise RuntimeError("embedding 失败: " + last)


async def _embed_via_provider(texts: list[str]) -> np.ndarray:
    """走 AstrBot 的 EmbeddingProvider。批量接口失败时退回逐条。"""
    vecs: list[list[float]] = []
    try:
        vecs = list(await _PROVIDER.get_embeddings(texts))
    except (AttributeError, NotImplementedError):
        for t in texts:
            vecs.append(await _PROVIDER.get_embedding(t))
    v = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return v / norms


def _check_dim(v: np.ndarray) -> np.ndarray:
    global _DIM
    if v.ndim != 2:
        raise ValueError("embedding 结果不是二维矩阵: {}".format(v.shape))
    dim = int(v.shape[1])
    if _DIM is None:
        _DIM = dim
    elif dim != _DIM:
        raise RuntimeError(
            "embedding 维度 {} 与当前向量库/模型维度 {} 不一致；"
            "请换回同维度模型，或重建向量库".format(dim, _DIM))
    return v


async def embed_one(text: str) -> np.ndarray:
    v = await embed([text])
    return v[0]
