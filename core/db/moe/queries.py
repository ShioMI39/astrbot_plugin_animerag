from __future__ import annotations

import sqlite3
from typing import Iterable


def distinct_values(conn: sqlite3.Connection,
                    attr_key: str) -> list[sqlite3.Row]:
    """某属性的所有 (attr_value, attr_norm)。"""
    return list(conn.execute(
        "SELECT DISTINCT attr_value, attr_norm FROM character_attributes"
        " WHERE attr_key = ?", (attr_key,)
    ))


def distinct_norms(conn: sqlite3.Connection,
                   attr_key: str) -> list[sqlite3.Row]:
    """某属性的所有归一化值。"""
    return list(conn.execute(
        "SELECT DISTINCT attr_norm FROM character_attributes"
        " WHERE attr_key = ?", (attr_key,)
    ))


def character_attrs(conn: sqlite3.Connection,
                    char_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT attr_key, attr_value FROM character_attributes"
        " WHERE char_id = ?", (char_id,)
    ))


def chars_by_raw_values(conn: sqlite3.Connection, attr_key: str,
                        values: Iterable[str]) -> list[sqlite3.Row]:
    values = list(values)
    if not values:
        return []
    ph = ",".join("?" for _ in values)
    return list(conn.execute(
        "SELECT char_id, attr_value FROM character_attributes"
        " WHERE attr_key = ? AND attr_value IN ({})".format(ph),
        [attr_key] + values,
    ))


def chars_by_norms(conn: sqlite3.Connection, attr_key: str,
                   norms: Iterable[str]) -> list[sqlite3.Row]:
    norms = list(norms)
    if not norms:
        return []
    ph = ",".join("?" for _ in norms)
    return list(conn.execute(
        "SELECT char_id, attr_value FROM character_attributes"
        " WHERE attr_key = ? AND attr_norm IN ({})".format(ph),
        [attr_key] + norms,
    ))


def count_ok_pages(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM moegirl_pages WHERE status='ok'"
    ).fetchone()[0])
