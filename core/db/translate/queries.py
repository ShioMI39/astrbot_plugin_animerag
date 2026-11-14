from __future__ import annotations

import sqlite3

from .schema import KINDS, is_japanese, src_hash


def get(conn: sqlite3.Connection, kind: str, ref_id: int) -> str | None:
    row = conn.execute(
        "SELECT dst_text FROM translations WHERE kind = ? AND ref_id = ?",
        (kind, ref_id),
    ).fetchone()
    return row["dst_text"] if row else None


def all_translations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT kind, ref_id, dst_text FROM translations"
    ))


def pending(conn: sqlite3.Connection, kind: str, rows: list[dict],
            force: bool = False) -> list[dict]:
    """挑出还没译、或原文已变的条目。"""
    if kind not in KINDS:
        raise ValueError("unknown kind: {}".format(kind))
    have = {
        r["ref_id"]: r["src_hash"]
        for r in conn.execute(
            "SELECT ref_id, src_hash FROM translations WHERE kind = ?", (kind,)
        )
    }
    out = []
    for r in rows:
        text = r.get("text") or ""
        if not is_japanese(text):
            continue
        if not force and have.get(r["ref_id"]) == src_hash(text):
            continue
        out.append(r)
    return out


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM translations"
    ).fetchone()[0])


def stats(conn: sqlite3.Connection) -> dict:
    by_kind = {
        r["kind"]: {"n": r["n"], "chars": r["chars"]}
        for r in conn.execute(
            "SELECT kind, COUNT(*) n, COALESCE(SUM(chars),0) chars"
            " FROM translations GROUP BY kind"
        )
    }
    engines = {
        r["engine"]: r["n"]
        for r in conn.execute(
            "SELECT engine, COUNT(*) n FROM translations GROUP BY engine"
        )
    }
    return {
        "total": sum(v["n"] for v in by_kind.values()),
        "source_chars": sum(v["chars"] for v in by_kind.values()),
        "by_kind": by_kind,
        "engines": engines,
    }
