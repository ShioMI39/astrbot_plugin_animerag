from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import opencc

# 字母 / 数字 / CJK / 假名 之外的一律去掉（含下划线）
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)

_COMBINING_RE = re.compile(r"[\u0300-\u036f]")

_ITERATION_RE = re.compile(r"(.)[\u3005\u303B]")

_cc_jp2t = opencc.OpenCC("jp2t.json")
_cc_t2s = opencc.OpenCC("t2s.json")


def expand_iteration_marks(text: str) -> str:
    """展开汉字叠字符号：奈々 -> 奈奈"""
    return _ITERATION_RE.sub(r"\1\1", text)


@lru_cache(maxsize=100_000)
def to_simplified(text: str) -> str:
    """日语汉字转简体中文"""
    return _cc_t2s.convert(_cc_jp2t.convert(expand_iteration_marks(text)))


def strip_diacritics(text: str) -> str:
    """去掉拉丁变音符号：Gotō -> Goto。"""
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", _COMBINING_RE.sub("", decomposed))


def normalize_alias(text: str | None) -> str:
    """完整归一化，作为检索 key"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = strip_diacritics(s)
    s = to_simplified(s)
    return _NON_WORD_RE.sub("", s.lower())
