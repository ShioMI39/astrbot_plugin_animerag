from __future__ import annotations

import sqlite3
from pathlib import Path

# 读写都用这个，和只读连接区分开
Row = sqlite3.Row


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）一个可写连接，带上项目统一的 PRAGMA。"""
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_readonly(
    path: str | Path, timeout: float = 30.0, *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """只读连接。"""
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(path), uri=True, timeout=timeout,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def table_names(conn: sqlite3.Connection, internal: str | None = None) -> set[str]:
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    args: tuple = ()
    if internal:
        sql += " AND name != ?"
        args = (internal,)
    return {r["name"] for r in conn.execute(sql, args).fetchall()}


def ensure_meta(conn: sqlite3.Connection, table: str = "meta") -> None:
    """建 key/value 元数据表。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS {} (key TEXT PRIMARY KEY, value TEXT)".format(table)
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None,
             table: str = "meta") -> str | None:
    if not table_exists(conn, table):
        return default
    row = conn.execute(
        "SELECT value FROM {} WHERE key = ?".format(table), (key,)
    ).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str,
             table: str = "meta") -> None:
    ensure_meta(conn, table)
    conn.execute(
        "INSERT OR REPLACE INTO {}(key, value) VALUES(?, ?)".format(table),
        (key, value),
    )
