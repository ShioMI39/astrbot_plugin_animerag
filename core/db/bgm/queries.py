from __future__ import annotations

import sqlite3
from typing import Any, Iterable
from ...normalize import normalize_alias

REL_DISPLAY_ORDER = (1, 2, 3, 4, 6, 8, 10, 9, 11, 12, 14, 7, 99)
_REL_PRIORITY_SQL = "CASE r.rel_type " + " ".join(
    "WHEN {} THEN {}".format(t, i) for i, t in enumerate(REL_DISPLAY_ORDER)
) + " ELSE 99 END"

_COUNT_TABLES = {
    "works", "work_stats", "work_tags", "characters", "character_aliases",
    "persons", "work_characters", "character_cvs", "work_relations",
    "work_staff", "search_index", "work_episodes",
}


def _normalize(text: str) -> str:
    return normalize_alias(text)


# ── search_index ─────────────────────────────────────────────────────────

def search(conn: sqlite3.Connection, text: str, kind: str | None = None,
           limit: int = 10) -> list[sqlite3.Row]:
    term = _normalize(text)
    if not term:
        return []
    sql = "SELECT * FROM search_index WHERE term_norm = ?"
    params: list[Any] = [term]
    if kind:
        sql += " AND target_kind = ?"
        params.append(kind)
    sql += " ORDER BY weight DESC, target_id ASC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def has_strong_name(conn: sqlite3.Connection, text: str) -> bool:
    term = _normalize(text)
    if not term:
        return False
    row = conn.execute(
        "SELECT 1 FROM search_index"
        " WHERE term_norm = ? AND weight >= 90 LIMIT 1", (term,)
    ).fetchone()
    return row is not None


def search_like(conn: sqlite3.Connection, text: str, kind: str | None = None,
                limit: int = 10) -> list[sqlite3.Row]:
    term = _normalize(text)
    if len(term) < 2:
        return []
    sql = "SELECT * FROM search_index WHERE term_norm LIKE ?"
    params: list[Any] = ["%{}%".format(term)]
    if kind:
        sql += " AND target_kind = ?"
        params.append(kind)
    sql += (
        " ORDER BY (term_norm LIKE ?) DESC, weight DESC,"
        " length(term_norm) ASC, target_id ASC LIMIT ?"
    )
    params.extend(["{}%".format(term), limit])
    return list(conn.execute(sql, params))


def search_terms(conn: sqlite3.Connection, max_len: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT term_norm, target_kind, target_id, weight FROM search_index"
        " WHERE length(term_norm) BETWEEN 2 AND ?", (max_len,)
    ))


# ── works / characters / persons ──────────────────────────────────────────

def get_work(conn: sqlite3.Connection, work_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM works WHERE work_id = ?", (work_id,)
    ).fetchone()
    return dict(row) if row else None


def get_work_stats(conn: sqlite3.Connection, work_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM work_stats WHERE work_id = ?", (work_id,)
    ).fetchone()
    return dict(row) if row else None


def get_work_tags(conn: sqlite3.Connection, work_id: int,
                  limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT tag, count, kind FROM work_tags WHERE work_id = ?"
        " ORDER BY count DESC LIMIT ?", (work_id, limit)
    )
    return [dict(r) for r in rows]



def get_worklist(
    conn: sqlite3.Connection,
    tags: list[str] | None = None,
    *,
    start_month: str | None = None,
    end_month: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """按 tag/月份/评分筛作品，按 tag 命中数、日期、评分排序。"""
    tags = list(dict.fromkeys(t.strip() for t in (tags or []) if t and t.strip()))
    where: list[str] = []
    where_params: list[Any] = []
    if start_month:
        where.append("substr(w.date, 1, 7) >= ?")
        where_params.append(start_month)
    if end_month:
        where.append("substr(w.date, 1, 7) <= ?")
        where_params.append(end_month)
    if score_min is not None:
        where.append("s.score IS NOT NULL AND s.score >= ?")
        where_params.append(float(score_min))
    if score_max is not None:
        where.append("s.score IS NOT NULL AND s.score <= ?")
        where_params.append(float(score_max))

    join_sql = ""
    join_params: list[Any] = []
    having = ""
    if tags:
        marks = ",".join("?" for _ in tags)
        join_sql = (
            " LEFT JOIN work_tags wt ON wt.work_id = w.work_id"
            " AND wt.tag IN ({})".format(marks)
        )
        join_params = tags
        having = " HAVING COUNT(DISTINCT wt.tag) > 0"

    sql = (
        "SELECT w.work_id, w.name, w.name_cn, w.date,"
        " COUNT(DISTINCT wt.tag) AS matched, MAX(s.score) AS score"
        " FROM works w"
        " LEFT JOIN work_stats s ON s.work_id = w.work_id"
        + join_sql
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY w.work_id"
        + having
        + " ORDER BY matched DESC, (w.date IS NULL), w.date DESC,"
        " (s.score IS NULL), s.score DESC, w.work_id"
        " LIMIT ?"
    )
    limit = max(1, min(int(limit), 100))
    return [
        dict(r)
        for r in conn.execute(sql, [*join_params, *where_params, limit])
    ]


def get_work_relations(conn: sqlite3.Connection,
                       work_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.related_id, r.rel_type, r.rel_name,"
        "       w.name, w.name_cn, w.date, w.year, w.platform"
        " FROM work_relations r LEFT JOIN works w ON w.work_id = r.related_id"
        " WHERE r.work_id = ?"
        " ORDER BY " + _REL_PRIORITY_SQL + ", (w.date IS NULL), w.date, r.related_id",
        (work_id,),
    )
    return [dict(r) for r in rows]


def get_character(conn: sqlite3.Connection,
                  char_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM characters WHERE char_id = ?", (char_id,)
    ).fetchone()
    return dict(row) if row else None


def get_character_aliases(conn: sqlite3.Connection,
                          char_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT alias, alias_norm, alias_type, source"
        " FROM character_aliases WHERE char_id = ?", (char_id,)
    )
    return [dict(r) for r in rows]


def get_character_cvs(conn: sqlite3.Connection, char_id: int, *,
                      with_work: bool = False) -> list[dict[str, Any]]:
    if with_work:
        sql = (
            "SELECT c.lang, c.cv_type, c.work_id, c.person_id,"
            "       p.name, p.name_cn AS person_name_cn, w.name_cn"
            " FROM character_cvs c"
            " JOIN persons p ON p.person_id = c.person_id"
            " LEFT JOIN works w ON w.work_id = c.work_id"
            " WHERE c.char_id = ? ORDER BY c.lang DESC, p.name"
        )
    else:
        sql = (
            "SELECT DISTINCT c.lang, c.cv_type, p.name"
            " FROM character_cvs c"
            " JOIN persons p ON p.person_id = c.person_id"
            " WHERE c.char_id = ? ORDER BY c.lang DESC, p.name"
        )
    return [dict(r) for r in conn.execute(sql, (char_id,))]


def get_work_staff(conn: sqlite3.Connection,
                   work_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.position, s.position_name, s.person_id, s.appear_eps,"
        "       p.name, p.name_cn AS person_name_cn, p.type AS person_type"
        " FROM work_staff s JOIN persons p ON p.person_id = s.person_id"
        " WHERE s.work_id = ? ORDER BY s.position, p.name",
        (work_id,),
    )
    return [dict(r) for r in rows]


def get_person(conn: sqlite3.Connection,
               person_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM persons WHERE person_id = ?", (person_id,)
    ).fetchone()
    return dict(row) if row else None


def get_person_works(conn: sqlite3.Connection, person_id: int,
                     positions: tuple[int, ...] | None = None
                     ) -> list[dict[str, Any]]:
    sql = (
        "SELECT s.position, s.position_name, s.appear_eps, w.work_id,"
        "       w.name, w.name_cn, w.date, w.year, w.platform"
        " FROM work_staff s JOIN works w ON w.work_id = s.work_id"
        " WHERE s.person_id = ?"
    )
    params: list[Any] = [person_id]
    if positions:
        sql += " AND s.position IN ({})".format(",".join("?" for _ in positions))
        params.extend(positions)
    sql += " ORDER BY w.year, w.work_id"
    return [dict(r) for r in conn.execute(sql, params)]


def get_work_characters(conn: sqlite3.Connection, work_id: int,
                        roles: tuple[int, ...] = (1, 2, 3)
                        ) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in roles)
    rows = conn.execute(
        "SELECT v.role, v.role_name, c.char_id, c.name, c.name_cn, c.gender,"
        "       c.birthday, c.blood_type, c.height_cm, c.weight_kg, c.collects,"
        "       c.summary"
        " FROM v_work_characters v JOIN characters c USING(char_id)"
        " WHERE v.work_id = ? AND v.role IN ({})"
        " ORDER BY v.role, c.char_id".format(placeholders),
        (work_id, *roles),
    )
    return [dict(r) for r in rows]


def get_character_works(conn: sqlite3.Connection,
                        char_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.work_id, v.role, v.role_name, v.cvs,"
        "       w.name AS work_name, w.name_cn AS work_name_cn,"
        "       w.date, w.platform"
        " FROM v_work_characters v JOIN works w USING(work_id)"
        " WHERE v.char_id = ?"
        " ORDER BY w.date IS NULL, w.date, v.work_id",
        (char_id,),
    )
    return [dict(r) for r in rows]


def get_person_title(conn: sqlite3.Connection, person_id: int) -> str:
    row = conn.execute(
        "SELECT name, name_cn FROM persons WHERE person_id = ?", (person_id,)
    ).fetchone()
    return (row["name_cn"] or row["name"] or "") if row else ""


# ── 计数 / meta ───────────────────────────────────────────────────────────

def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in _COUNT_TABLES:
        raise ValueError("unknown table: {}".format(table))
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM {}".format(table)
    ).fetchone()["n"])


def get_data_version(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='data_version'"
    ).fetchone()
    return row[0] if row else ""


# ── 名字定位 ─────────────────────────────────────────────────────────────

# ── 单集 ─────────────────────────────────────────────────────────────────

def count_episodes(conn: sqlite3.Connection, work_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM work_episodes WHERE work_id = ?", (work_id,)
    ).fetchone()[0])


def list_episodes(conn: sqlite3.Connection, work_id: int,
                  start: float, end: float, limit: int
                  ) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT sort, name, name_cn, airdate, synopsis"
        "  FROM work_episodes"
        " WHERE work_id = ? AND sort >= ? AND sort <= ?"
        " ORDER BY sort LIMIT ?",
        (work_id, start, end, limit),
    ))


def count_episodes_upto(conn: sqlite3.Connection, work_id: int,
                        sort: float) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM work_episodes WHERE work_id = ? AND sort <= ?",
        (work_id, sort),
    ).fetchone()[0])


def next_episode_sort(conn: sqlite3.Connection, work_id: int,
                      sort: float) -> float | None:
    row = conn.execute(
        "SELECT MIN(sort) FROM work_episodes WHERE work_id = ? AND sort > ?",
        (work_id, sort),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def get_episode(conn: sqlite3.Connection, ep_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT e.ep_id, e.sort, e.name, e.name_cn, e.airdate, e.synopsis,"
        "       COALESCE(w.name_cn, w.name) AS work_title"
        "  FROM work_episodes e JOIN works w USING(work_id)"
        " WHERE e.ep_id = ?", (ep_id,)
    ).fetchone()
    return dict(row) if row else None


def get_episode_source(conn: sqlite3.Connection,
                       ep_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT e.sort, e.synopsis, COALESCE(w.name_cn, w.name) AS work_title"
        "  FROM work_episodes e JOIN works w USING(work_id)"
        " WHERE e.ep_id = ?", (ep_id,)
    ).fetchone()
    return dict(row) if row else None


# ── 搜索层批量查询 ───────────────────────────────────────────────────────

_TRANSLATION_SOURCES = {
    "character": (
        "SELECT char_id AS ref_id, summary AS text,"
        " COALESCE(name_cn, name) AS label FROM characters"
        " WHERE summary IS NOT NULL AND summary != ''"
    ),
    "work": (
        "SELECT work_id AS ref_id, summary AS text,"
        " COALESCE(name_cn, name) AS label FROM works"
        " WHERE summary IS NOT NULL AND summary != ''"
    ),
    "episode": (
        "SELECT ep_id AS ref_id, synopsis AS text,"
        " COALESCE(name_cn, name, '') AS label FROM work_episodes"
        " WHERE synopsis IS NOT NULL AND synopsis != ''"
    ),
}


def get_translation_sources(conn: sqlite3.Connection, kind: str
                            ) -> list[sqlite3.Row]:
    sql = _TRANSLATION_SOURCES.get(kind)
    if sql is None:
        raise ValueError("unknown kind: {}".format(kind))
    return list(conn.execute(sql))


def get_character_meta_many(conn: sqlite3.Connection,
                            ids: Iterable[int]) -> dict[int, tuple[str, int]]:
    ids = list(ids)
    out: dict[int, tuple[str, int]] = {}
    for start in range(0, len(ids), 400):
        part = ids[start:start + 400]
        ph = ",".join("?" * len(part))
        for cid, name, collects in conn.execute(
            "SELECT char_id, COALESCE(name_cn, name), collects"
            "  FROM characters WHERE char_id IN ({})".format(ph),
            part,
        ):
            out[int(cid)] = (name or "", int(collects or 0))
    return out


def get_character_popularity(conn: sqlite3.Connection, char_id: int) -> int:
    row = conn.execute(
        "SELECT collects FROM characters WHERE char_id = ?", (char_id,)
    ).fetchone()
    return int(row[0] or 0) if row else 0


def get_work_popularity(conn: sqlite3.Connection, work_id: int) -> int:
    row = conn.execute(
        "SELECT favorite_done FROM work_stats WHERE work_id = ?", (work_id,)
    ).fetchone()
    return int(row[0] or 0) if row else 0


def get_person_popularity(conn: sqlite3.Connection, person_id: int) -> int:
    staff = conn.execute(
        "SELECT COUNT(*) FROM work_staff WHERE person_id = ?", (person_id,)
    ).fetchone()[0]
    cvs = conn.execute(
        "SELECT COUNT(DISTINCT work_id) FROM character_cvs WHERE person_id = ?",
        (person_id,),
    ).fetchone()[0]
    return int(staff or 0) + int(cvs or 0)


def get_tag_rows(conn: sqlite3.Connection,
                 tags: Iterable[str]) -> list[sqlite3.Row]:
    tags = list(tags)
    if not tags:
        return []
    ph = ",".join("?" for _ in tags)
    return list(conn.execute(
        "SELECT wt.work_id, wt.tag, COALESCE(s.favorite_done, 0)"
        "  FROM work_tags wt LEFT JOIN work_stats s USING(work_id)"
        " WHERE wt.kind IN ('user','meta') AND wt.tag IN ({})".format(ph),
        tags,
    ))


def get_work_titles(conn: sqlite3.Connection,
                    ids: Iterable[int]) -> dict[int, str]:
    ids = list(ids)
    if not ids:
        return {}
    return {
        int(r[0]): (r[1] or r[2] or "")
        for r in conn.execute(
            "SELECT work_id, name_cn, name FROM works WHERE work_id IN ({})".format(
                ",".join("?" * len(ids))
            ),
            ids,
        )
    }


def get_person_cv_works(conn: sqlite3.Connection,
                        person_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT w.work_id, COALESCE(w.name_cn, w.name) AS title, w.date,"
        "       ch.char_id, COALESCE(ch.name_cn, ch.name) AS cname"
        "  FROM character_cvs cv"
        "  JOIN works w ON w.work_id = cv.work_id"
        "  JOIN characters ch ON ch.char_id = cv.char_id"
        " WHERE cv.person_id = ?", (person_id,)
    )
    return [dict(r) for r in rows]
