from __future__ import annotations

import json
from pathlib import Path

VERSION = 2


def new_cache() -> dict:
    return {"version": VERSION, "updated_at": 0, "pages": {}}


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return new_cache()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return new_cache()
    if not isinstance(data, dict) or "pages" not in data:
        return new_cache()
    data.setdefault("pages", {})
    migrated = False
    for entry in data["pages"].values():
        sec = entry.get("sections")
        if isinstance(sec, list):
            entry["sections"] = {t: x for t, x in sec}
            migrated = True
    if migrated or int(data.get("version") or 0) < VERSION:
        data["version"] = VERSION
    return data


def get(cache: dict, char_id: int) -> dict | None:
    return cache["pages"].get(str(char_id))


def sections_of(entry: dict) -> dict[str, str]:
    """取章节字典"""
    sec = entry.get("sections") or {}
    if isinstance(sec, list):
        return {t: x for t, x in sec}
    return sec
