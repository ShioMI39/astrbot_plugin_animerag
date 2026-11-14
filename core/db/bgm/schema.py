from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .. import base

PLATFORM = {0: "其他", 1: "TV", 2: "OVA", 3: "剧场版", 5: "WEB", 2006: "动态漫画"}

REL_DISPLAY_ORDER = (1, 2, 3, 4, 6, 8, 10, 9, 11, 12, 14, 7, 99)

STAFF_POSITIONS = {
    2:  "导演",
    72: "副导演",
    10: "系列构成",
    3:  "脚本",
    8:  "人物设定",
    6:  "音乐",
    17: "摄影监督",
    44: "音响监督",
    14: "总作画监督",
    15: "作画监督",
    4:  "分镜",
    5:  "演出",
    42: "製作",
    67: "动画制作",
    54: "制片人",
    58: "总制片人",
    87: "动画制片人",
}

SCHEMA_SQL = """
-- ═══════════════ 1. 作品（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS works (
    work_id        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    name_cn        TEXT,
    platform       INTEGER,
    date           TEXT,
    year           INTEGER,
    season         TEXT,
    episodes       INTEGER,
    nsfw           INTEGER NOT NULL DEFAULT 0,
    summary        TEXT,
    infobox        TEXT,
    data_version   TEXT,
    updated_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_works_name_cn ON works(name_cn);
CREATE INDEX IF NOT EXISTS idx_works_year    ON works(year);

-- ═══════════════ 1b. 作品统计（易变，整表重建） ═══════════════
CREATE TABLE IF NOT EXISTS work_stats (
    work_id       INTEGER PRIMARY KEY
                  REFERENCES works(work_id) ON DELETE CASCADE,
    score         REAL,
    score_count   INTEGER,
    rank          INTEGER,
    favorite_done INTEGER,
    source        TEXT,
    updated_at    INTEGER
);

-- ═══════════════ 2. 作品标签（派生表，整表重建） ═══════════════
CREATE TABLE IF NOT EXISTS work_tags (
    work_id INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    kind    TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (work_id, tag, kind)
);
CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON work_tags(tag);

-- ═══════════════ 3. 角色（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS characters (
    char_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_cn     TEXT,
    gender      TEXT,
    birthday    TEXT,
    blood_type  TEXT,
    height_cm   REAL,
    weight_kg   REAL,
    summary     TEXT,
    infobox     TEXT,
    collects    INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_characters_name_cn ON characters(name_cn);
CREATE INDEX IF NOT EXISTS idx_characters_gender  ON characters(gender);

-- ═══════════════ 4. 角色别名（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS character_aliases (
    char_id    INTEGER NOT NULL REFERENCES characters(char_id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    alias_type TEXT,
    source     TEXT,
    PRIMARY KEY (char_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON character_aliases(alias_norm);

-- ═══════════════ 5. 人物 / 声优（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS persons (
    person_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_cn     TEXT,
    type        INTEGER,
    career_json TEXT,
    summary     TEXT,
    infobox     TEXT,
    updated_at  INTEGER
);

-- ═══════════════ 6. 作品 <-> 角色（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS work_characters (
    work_id INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    char_id INTEGER NOT NULL REFERENCES characters(char_id) ON DELETE CASCADE,
    role    INTEGER NOT NULL,
    PRIMARY KEY (work_id, char_id)
);
CREATE INDEX IF NOT EXISTS idx_wc_char ON work_characters(char_id);

-- ═══════════════ 7. 角色 <-> 声优（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS character_cvs (
    char_id   INTEGER NOT NULL REFERENCES characters(char_id) ON DELETE CASCADE,
    work_id   INTEGER NOT NULL,
    person_id INTEGER NOT NULL REFERENCES persons(person_id),
    lang      TEXT NOT NULL,
    cv_type   INTEGER NOT NULL,
    PRIMARY KEY (char_id, work_id, person_id, lang)
);
CREATE INDEX IF NOT EXISTS idx_cv_char_lang ON character_cvs(char_id, lang);
CREATE INDEX IF NOT EXISTS idx_cv_person    ON character_cvs(person_id);

-- ═══════════════ 8. 作品 <-> 作品（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS work_episodes (
    ep_id        INTEGER PRIMARY KEY,
    work_id      INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    sort         REAL NOT NULL,     
    name         TEXT,              
    name_cn      TEXT,              
    synopsis     TEXT,              
    airdate      TEXT,              
    duration_sec INTEGER            
);
CREATE INDEX IF NOT EXISTS idx_ep_work ON work_episodes(work_id, sort);

CREATE TABLE IF NOT EXISTS work_relations (
    work_id    INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    related_id INTEGER NOT NULL,
    rel_type   INTEGER NOT NULL,
    rel_name   TEXT NOT NULL,
    PRIMARY KEY (work_id, related_id, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_wr_related ON work_relations(related_id);

-- ═══════════════ 8b. 作品 <-> 制作人员（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS work_staff (
    work_id       INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    person_id     INTEGER NOT NULL REFERENCES persons(person_id),
    position      INTEGER NOT NULL,
    position_name TEXT    NOT NULL,
    appear_eps    TEXT,
    PRIMARY KEY (work_id, person_id, position)
);
CREATE INDEX IF NOT EXISTS idx_staff_person   ON work_staff(person_id);
CREATE INDEX IF NOT EXISTS idx_staff_position ON work_staff(position);

-- ═══════════════ 9. 统一检索索引（稳定） ═══════════════
CREATE TABLE IF NOT EXISTS search_index (
    term_norm   TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    weight      INTEGER NOT NULL DEFAULT 50,
    PRIMARY KEY (term_norm, target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS idx_search_kind ON search_index(target_kind, target_id);

-- ═══════════════ 10. 元数据 ═══════════════
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def all_table_names(conn: sqlite3.Connection) -> list[str]:
    """库里实际存在的业务表名"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """打开数据库并设置 PRAGMA。"""
    p = Path(path)
    if read_only:
        conn = base.connect_readonly(p, timeout=5)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.executescript(VIEW_SQL)
    conn.commit()


# 派生视图：把「作品 -> 角色」这条链上的三张表拼成一行。
VIEW_SQL = """
DROP VIEW IF EXISTS v_work_characters;
CREATE VIEW v_work_characters AS
SELECT
    wc.work_id,
    wc.char_id,
    wc.role,
    CASE wc.role
        WHEN 1 THEN '主角' WHEN 2 THEN '配角' WHEN 3 THEN '客串'
        WHEN 4 THEN '闲角' WHEN 5 THEN '旁白' ELSE '?'
    END AS role_name,
    ch.name        AS char_name,
    ch.name_cn     AS char_name_cn,
    ch.collects,
    (SELECT GROUP_CONCAT(p.name, '、')
       FROM character_cvs cv JOIN persons p USING(person_id)
       WHERE cv.char_id = wc.char_id AND cv.work_id = wc.work_id) AS cvs
FROM work_characters wc
JOIN characters ch USING(char_id);
"""


class Database:
    """anime.db 的连接"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
