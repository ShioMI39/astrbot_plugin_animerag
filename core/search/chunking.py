from __future__ import annotations

import re
from typing import Any, Iterable

MAX_CHARS = 400          # 目标上限
HARD_MAX = 600           # 超过这个必须切
MIN_CHARS = 60           # 短于这个的段落尽量并入相邻块
MOE_MIN = 120            # 萌百章节短于此值和下一章合并

# 萌百白名单：这 9 个键会拼成「角色卡」的属性前缀
ATTR_KEYS = ("发色", "瞳色", "萌点", "所属团体", "身高", "生日", "星座", "体重", "血型")

# 只取这些 platform
PLATFORM_NAMES = {1: "TV", 2: "OVA", 3: "剧场版", 5: "Web"}

# 控制字符会让 embedding API 返回 400 parameter invalid
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: str | None) -> str:
    """压掉控制字符和多余空白，保留段落分隔。"""
    if not text:
        return ""
    text = _CTRL_RE.sub("", str(text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def split_text(text: str, max_chars: int = MAX_CHARS,
               hard_max: int = HARD_MAX) -> list[str]:
    """按段落 → 句子 → 硬切 三级降级切分。"""
    text = _clean(text)
    if not text:
        return []
    if len(text) <= hard_max:
        return [text]

    # ① 段落切
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) == 1:
        paras = [p.strip() for p in text.split("\n") if p.strip()]

    pieces: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > hard_max:
            # ② 句子切
            sents = re.split(r"(?<=[。！？!?；;])\s*", p)
            for s in sents:
                if not s:
                    continue
                if len(s) > hard_max:
                    # ③ 硬切
                    for i in range(0, len(s), max_chars):
                        pieces.append(s[i:i + max_chars])
                    continue
                if len(buf) + len(s) <= max_chars:
                    buf += s
                else:
                    if buf:
                        pieces.append(buf)
                    buf = s
            continue
        if len(buf) + len(p) <= max_chars:
            buf = (buf + "\n" + p) if buf else p
        else:
            if buf:
                pieces.append(buf)
            buf = p
    if buf:
        pieces.append(buf)

    # 合并过短的块
    merged: list[str] = []
    for p in pieces:
        if merged and len(merged[-1]) < MIN_CHARS and len(merged[-1]) + len(p) <= hard_max:
            merged[-1] = merged[-1] + "\n" + p
        else:
            merged.append(p)
    return [m for m in merged if m.strip()]


def attr_block(attrs: dict[str, list[str]]) -> str:
    """按白名单拼属性前缀。attrs: {key: [value, ...]}"""
    lines = []
    for key in ATTR_KEYS:
        vals = attrs.get(key)
        if not vals:
            continue
        lines.append(f"{key}：{'、'.join(vals)}")
    return "\n".join(lines)


def name_line(names: Iterable[str]) -> str:
    """去重保序，压成一行空格分隔的检索用名字串。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        n = (n or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return " ".join(out)


def make_chunk(cid: str, kind: str, ref_id: int, title: str, text: str,
               embed_head: str, **meta: Any) -> dict[str, Any]:
    """统一构造一个 chunk。"""
    body = _clean(text)
    return {
        "id": cid,
        "kind": kind,
        "ref_id": ref_id,
        "title": title,
        "text": body,
        "embed": _clean(embed_head + " " + body),
        **meta,
    }
