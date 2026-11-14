from __future__ import annotations

import hashlib
import re
import sqlite3
import time

from .. import base
from pathlib import Path

SCHEMA_VERSION = 2

KINDS = ("character", "work", "episode")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS translations (
    kind       TEXT NOT NULL,
    ref_id     INTEGER NOT NULL,
    src_hash   TEXT NOT NULL,
    dst_text   TEXT NOT NULL,
    engine     TEXT NOT NULL,
    chars      INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_tr_hash ON translations(src_hash);
CREATE TABLE IF NOT EXISTS tr_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def src_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def is_japanese(text: str) -> bool:
    """判断需要翻译的日文。"""
    if not text:
        return False
    kana = len(_KANA_RE.findall(text))
    han = len(_HAN_RE.findall(text))
    return kana > 0 and kana * 4 >= han


def open_db(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）译文库。"""
    conn = base.connect(path)
    conn.executescript(SCHEMA_SQL)
    base.set_meta(conn, "schema_version", str(SCHEMA_VERSION), table="tr_meta")
    conn.commit()
    return conn


def put(
    conn: sqlite3.Connection,
    kind: str,
    ref_id: int,
    source: str,
    dst_text: str,
    engine: str,
) -> None:
    if kind not in KINDS:
        raise ValueError("unknown kind: {}".format(kind))
    conn.execute(
        "INSERT OR REPLACE INTO translations"
        "(kind, ref_id, src_hash, dst_text, engine, chars, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (kind, ref_id, src_hash(source), dst_text, engine, len(source),
         int(time.time())),
    )
