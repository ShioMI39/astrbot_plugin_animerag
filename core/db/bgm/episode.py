from __future__ import annotations

import re

_ROLE_KEYWORDS = (
    "脚本", "コンテ", "演出", "作監", "作画", "監督", "监督", "分镜", "分鏡",
    "設定", "设定", "キャラ", "メカ", "美術", "美术", "撮影", "摄影",
    "音響", "音响", "色彩", "編集", "剪辑", "音楽", "音乐", "制作", "製作",
    "プロデューサー", "制片", "原画", "動画", "动画", "背景", "仕上げ",
    "レイアウト", "CG", "主題歌", "主题歌", "挿入歌", "修正", "配色",
    "作监", "カラー", "スクリプト", "アニメーション", "シナリオ",
    "ストーリーボード", "協力", "协力", "マンガ", "漫画", "原作",
)

# 职名和人名之间的分隔符
_SEP_RE = re.compile(r"[/／:：]")
# 装饰符 / 全角空格
_DECOR_RE = re.compile(r"[■□◆◇●○・▪▶→\s]")
# 多个职名之间的连接符
_PART_SPLIT_RE = re.compile(r"[・、,&＆]")
# 括号注释：脚本（编剧）
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def _is_role_word(part: str) -> bool:
    part = part.strip()
    if not part or len(part) > 12:
        return False
    return any(k in part for k in _ROLE_KEYWORDS)


def _is_credit_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    m = _SEP_RE.search(s)
    if not m:
        return False
    head = s[: m.start()].strip()
    if not head or len(head) > 24:
        return False
    head = _PAREN_RE.sub("", head).strip()
    head = _DECOR_RE.sub("", head)
    if not head:
        return False
    parts = [p for p in _PART_SPLIT_RE.split(head) if p.strip()]
    return bool(parts) and all(_is_role_word(p) for p in parts)


_SENT_END = ("。", "！", "？", "…", ".", "!", "?")


def _is_bare_role(line: str) -> bool:
    """整行只有一个职名、没有分隔符 —— 名字在下一行。"""
    s = (line or "").strip()
    if not s or len(s) > 12 or _SEP_RE.search(s):
        return False
    s = _PAREN_RE.sub("", s).strip()
    s = _DECOR_RE.sub("", s)
    return bool(s) and _is_role_word(s)


def split_description(text: str | None) -> str | None:
    """剥掉制作信息行，只留剧情简介。"""
    if not text or not text.strip():
        return None
    raw_lines = text.strip().splitlines()
    kept: list[str] = []
    drop_next = False
    for idx, raw in enumerate(raw_lines):
        line = raw.rstrip()
        if drop_next:
            drop_next = False
            continue
        if _is_credit_line(line):
            continue
        if _is_bare_role(line):
            nxt = raw_lines[idx + 1].strip() if idx + 1 < len(raw_lines) else ""
            if nxt and len(nxt) <= 30 and not nxt.endswith(_SENT_END):
                drop_next = True
                continue
        kept.append(line)
    # 去掉首尾空行（中间的空行保留 —— 那是剧情内的段落分隔）
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    out = "\n".join(kept).strip()
    return out or None


_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_MIN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*m(?:in)?$", re.I)


def parse_duration(text: str | None) -> int | None:
    """剧集时长 -> 秒。"""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None

    m = _CLOCK_RE.match(s)
    if m:
        h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if mi > 59 or sec > 59:
            return None
        return (h * 3600 + mi * 60 + sec) or None

    m = _MIN_RE.match(s)
    if m:
        return int(round(float(m.group(1)) * 60)) or None

    return None
