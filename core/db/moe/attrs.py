from __future__ import annotations

import re

from . import queries as moe_queries

# 颜色字 → 色系。一个值命中多个字就属于多个色系
COLOR_GROUPS: dict[str, str] = {
    "黑": "黑色系", "白": "白色系", "银": "银色系", "灰": "灰色系", "铅": "灰色系",
    "金": "金色系", "黄": "黄色系", "亚麻": "金色系", "米": "金色系", "杏": "金色系",
    "棕": "棕色系", "褐": "棕色系", "茶": "棕色系", "栗": "棕色系",
    "咖啡": "棕色系", "咖": "棕色系",
    "红": "红色系", "赤": "红色系", "玫": "红色系", "绯": "红色系",
    "粉": "粉色系", "桃": "粉色系", "樱": "粉色系", "草莓": "粉色系",
    "橙": "橙色系", "橘": "橙色系", "桔": "橙色系",
    "绿": "绿色系",
    "青": "蓝色系", "蓝": "蓝色系", "靛": "蓝色系", "碧": "蓝色系",
    "紫": "紫色系",
    "琥珀": "黄色系",
    "彩虹": "彩色系",
}

_TRAIT_SUFFIXES = ("系", "少女", "娘", "属性", "控")

_SPLIT_RE = re.compile(r"[、,，/／;；\s]+")

def groups_for(attr_key: str, value: str, norm: str | None = None) -> set[str]:
    """把单个属性值映射成若干个组名。

    norm 传 character_attributes.attr_norm 时优先用它匹配
    """
    raw = (value or "").strip()
    if not raw:
        return set()
    v = (norm or raw).strip()
    if attr_key in ("发色", "瞳色"):
        return {g for ch, g in COLOR_GROUPS.items() if ch in v}
    if attr_key == "萌点":
        out = set()
        for p in _SPLIT_RE.split(v):
            p = p.strip()
            if not p:
                continue
            base = p
            for suf in _TRAIT_SUFFIXES:
                if base.endswith(suf) and len(base) > len(suf) + 1:
                    base = base[: -len(suf)]
                    break
            out.add(base)
        return out
    return set()


_DISTINCT: dict[str, dict[str, str]] = {}      # attr_key -> {value: norm}
_GROUP_VALUES: dict[tuple[str, str], list[str]] = {}


def load_distinct(conn, attr_key: str) -> dict[str, str]:
    """某个属性下的所有取值（value -> norm）。结果缓存，进程内只查一次。"""
    hit = _DISTINCT.get(attr_key)
    if hit is None:
        hit = {r[0]: r[1] for r in moe_queries.distinct_values(conn, attr_key)}
        _DISTINCT[attr_key] = hit
    return hit


def values_in_group(conn, attr_key: str, group: str) -> list[str]:
    """该属性下所有归到这一组的原始取值。"""
    key = (attr_key, group)
    hit = _GROUP_VALUES.get(key)
    if hit is None:
        hit = sorted(
            v for v, norm in load_distinct(conn, attr_key).items()
            if group in groups_for(attr_key, v, norm)
        )
        _GROUP_VALUES[key] = hit
    return hit


def clear_cache() -> None:
    _DISTINCT.clear()
    _GROUP_VALUES.clear()
