from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from typing import Callable

import aiohttp

LATEST_URL = (
    "https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json"
)
UA = "astrbot-anime-rag/0.1 (+knowledge base updater)"
CHUNK = 1 << 20

REQUIRED = (
    "subject.jsonlines",
    "character.jsonlines",
    "subject-characters.jsonlines",
    "person-characters.jsonlines",
)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "{:.1f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} TB".format(n)


async def fetch_latest(proxy: str | None = None) -> dict:
    """读 latest.json，拿到最新 dump"""
    timeout = aiohttp.ClientTimeout(total=30)
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    LATEST_URL, headers={"User-Agent": UA}, proxy=proxy
                ) as r:
                    r.raise_for_status()
                    return await r.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("读取 latest.json 失败：{}".format(last))


def dump_name(meta: dict) -> str:
    return str(meta.get("name") or "").replace(".zip", "")


def verify(path: Path, digest: str) -> bool:
    want = (digest or "").split(":", 1)[-1].lower()
    if not want:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest() == want


async def download(
    url: str,
    dest: Path,
    size: int = 0,
    digest: str = "",
    on_progress: Callable[[int, int], None] | None = None,
    proxy: str | None = None,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists() and (not size or dest.stat().st_size == size) and verify(dest, digest):
        return dest
    if dest.exists():
        dest.unlink()
    if part.exists() and size and part.stat().st_size == size:
        if verify(part, digest):
            part.replace(dest)
            return dest
        part.unlink()

    timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
    last: Exception | None = None
    tries = 8
    for attempt in range(tries):
        done = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": UA}
        if done:
            headers["Range"] = "bytes={}-".format(done)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, headers=headers, proxy=proxy) as r:
                    r.raise_for_status()
                    if done and r.status != 206:
                        done = 0
                        mode = "wb"
                    else:
                        mode = "ab" if done else "wb"
                    total = size
                    if not total:
                        cr = r.headers.get("Content-Range") or ""
                        if "/" in cr:
                            total = int(cr.rsplit("/", 1)[-1] or 0)
                        else:
                            total = int(r.headers.get("Content-Length") or 0) or 0
                            if done and total:
                                total += done
                    with open(part, mode) as f:
                        async for chunk in r.content.iter_chunked(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            if on_progress:
                                on_progress(done, total or done)
            got = part.stat().st_size if part.exists() else 0
            if size and got < size:
                raise RuntimeError("下载不完整：{}/{}".format(got, size))
            if not verify(part, digest):
                part.unlink(missing_ok=True)
                raise RuntimeError("sha256 校验失败")
            part.replace(dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not part.exists():
                pass
            elif size and part.stat().st_size > size:
                part.unlink(missing_ok=True)
            if attempt + 1 >= tries:
                break
            await asyncio.sleep(min(10.0, 2.0 * (attempt + 1)))
    raise RuntimeError("下载失败（已重试 {} 次）：{}".format(tries, last))

def extract(zip_path: Path, out_dir: Path) -> Path:
    """解压到 out_dir，返回真正的 dump 目录"""
    if out_dir.exists() and all((out_dir / f).exists() for f in REQUIRED):
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        root = ""
        if names and "/" in names[0]:
            root = names[0].split("/")[0] + "/"
        z.extractall(out_dir)
    target = out_dir / root.rstrip("/") if root else out_dir
    if not all((target / f).exists() for f in REQUIRED) and all(
        (out_dir / f).exists() for f in REQUIRED
    ):
        return out_dir
    return target


async def fetch_and_extract(
    cache_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    proxy: str | None = None,
) -> tuple[Path, str]:
    """查最新 -> 下载 -> 解压。返回 (dump 目录, 版本名)。"""
    meta = await fetch_latest(proxy=proxy)
    name = dump_name(meta)
    url = str(meta.get("browser_download_url") or "")
    if not url:
        raise RuntimeError("latest.json 里没有 browser_download_url")
    zip_path = cache_dir / (name + ".zip")
    await download(
        url, zip_path, int(meta.get("size") or 0),
        str(meta.get("digest") or ""), on_progress, proxy=proxy,
    )
    return extract(zip_path, cache_dir / name), name
