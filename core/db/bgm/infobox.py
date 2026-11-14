from __future__ import annotations

import re
from typing import Any

# 多值块里的条目：[值] 或 [类型|值]
_LIST_ITEM_RE = re.compile(r"\[([^\[\]]*)\]")

# 生日：「1994年2月1日(水瓶座)」-> 先去掉括号注释
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_DATE_YMD_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
_DATE_MD_RE = re.compile(r"(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _strip_outer_template(text: str) -> str | None:
    """剥掉最外层双层花括号，用括号配平。"""
    s = text.strip()
    if not s.startswith("{{"):
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # s[0:2] 是开头的双花括号，s[i] 是与之配对的右花括号
                return s[2 : i - 1]
    # 括号不配平：退化为去掉首尾各两个字符
    return s[2:-2] if s.endswith("}}") else s[2:]


def _split_top_level(body: str, sep: str = "|") -> list[str]:
    """按分隔符切分。"""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _parse_list_value(value: str) -> list[tuple[str | None, str]]:
    """解析多值块，返回 [(类型 或 None, 值), ...]，空项丢弃。"""
    v = value.strip()
    if v.startswith("{"):
        v = v[1:]
    if v.endswith("}"):
        v = v[:-1]

    items: list[tuple[str | None, str]] = []
    for raw in _LIST_ITEM_RE.findall(v):
        raw = raw.strip()
        if not raw:
            continue
        if "|" in raw:
            kind, _, val = raw.partition("|")
            kind, val = kind.strip(), val.strip()
            if val:
                items.append((kind or None, val))
        else:
            items.append((None, raw))
    return items


def parse_infobox(raw: str | None) -> dict[str, Any]:
    """把原始 wiki infobox 解析成 {字段名: 字符串 或 [(类型, 值), ...]}。"""
    if not raw:
        return {}

    body = _strip_outer_template(raw)
    if body is None:
        return {}

    result: dict[str, Any] = {}
    for field in _split_top_level(body)[1:]:  # [0] 是模板名
        if "=" not in field:
            continue
        key, _, value = field.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        result[key] = _parse_list_value(value) if value.startswith("{") else value
    return result


_ALIAS_SPLIT_RE = re.compile(r"[、;；]")


def _expand_alias_items(
    items: list[tuple[str | None, str]],
) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for kind, val in items:
        parts = _ALIAS_SPLIT_RE.split(val)
        if len(parts) == 1:
            out.append((kind, val.strip()))
        else:
            out.extend((kind, p.strip()) for p in parts if p.strip())
    return [(k, v) for k, v in out if v]


def parse_alias_list(infobox: dict[str, Any]) -> list[tuple[str | None, str]]:
    """取出「别名」字段，并拆开一条里塞多个名字的情况。"""
    v = infobox.get("别名")
    if isinstance(v, list):
        return _expand_alias_items(v)
    if isinstance(v, str) and v.strip():
        if "[" in v:
            return _expand_alias_items(_parse_list_value(v))
        return _expand_alias_items(
            [(None, p) for p in re.split(r"[、,，;；]", v) if p.strip()]
        )
    return []


def _as_text(value: Any) -> str:
    """把字段值强制成文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(x) for x in value)
    if isinstance(value, dict):
        return " ".join(_as_text(x) for x in value.values())
    return str(value)


def _clean(text: Any) -> str:
    return _PAREN_RE.sub("", _as_text(text).strip()).strip()


def parse_birthday(text: Any) -> tuple[int | None, int | None, int | None]:
    """生日 -> (年, 月, 日)，识别不出的位置为 None。"""
    s = _clean(text)
    if not s:
        return (None, None, None)

    m = _DATE_YMD_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
        return (None, None, None)

    m = _DATE_MD_RE.search(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (None, mo, d)

    return (None, None, None)


def parse_blood_type(text: Any) -> str | None:
    """O型 / A 型 / AB -> O / A / AB；不明 等返回 None。"""
    s = _clean(text).upper().replace(" ", "")
    if not s:
        return None
    if s.startswith("AB"):
        return "AB"
    for t in ("A", "B", "O"):
        if s.startswith(t):
            return t
    return None


def parse_gender(text: Any) -> str | None:
    """归一化性别；无法判断返回 None。"""
    s = _clean(text)
    if not s:
        return None
    if s in {"男", "男性", "male", "Male"}:
        return "男"
    if s in {"女", "女性", "female", "Female"}:
        return "女"
    return "其他"


def _parse_measure(text: Any, units: tuple[str, ...], scale: float) -> float | None:
    """从 178cm / 1.78m / 54kg 里取数值并换算成基础单位。"""
    s = _clean(text).lower()
    if not s:
        return None
    # 只取第一个数字：178cm→181cm 取 178
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if not any(u in s for u in units):
        return None
    return round(val * scale, 1)


def parse_length_cm(text: Any) -> float | None:
    """身高 -> 厘米。1.78m / 1.78米 会被换算成 178。"""
    s = _clean(text).lower()
    if not s:
        return None
    if "cm" in s or "厘米" in s:
        return _parse_measure(s, ("cm", "厘米"), 1.0)
    if "m" in s or "米" in s:
        return _parse_measure(s, ("m", "米"), 100.0)
    return None


def parse_weight_kg(text: Any) -> float | None:
    """体重 -> 千克。54kg / 98Kg。"""
    return _parse_measure(text, ("kg", "公斤", "千克"), 1.0)


def parse_int(text: Any) -> int | None:
    """从任意文本里抠出第一个整数（用于「话数= 12」）。"""
    m = _NUM_RE.search(_clean(text))
    if not m:
        return None
    try:
        return int(float(m.group(1)))
    except ValueError:
        return None


def parse_date(text: Any) -> str | None:
    """把 2022年10月8日 / 2022-10-08 归一化成 YYYY-MM-DD。"""
    y, mo, d = parse_birthday(text)
    if y and mo and d:
        return "{:04d}-{:02d}-{:02d}".format(y, mo, d)
    return None
