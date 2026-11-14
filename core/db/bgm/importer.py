from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema as dbmod
from .episode import parse_duration, split_description
from .infobox import (
    parse_alias_list,
    parse_birthday,
    parse_blood_type,
    parse_date,
    parse_gender,
    parse_infobox,
    parse_int,
    parse_length_cm,
    parse_weight_kg,
)
from ...normalize import normalize_alias

SUBJECT_TYPE_ANIME = 2
CHARACTER_ROLE_PERSON = 1
WORK_CHARACTER_ROLES = (1, 2, 3)  # 主角 / 配角 / 客串
EPISODE_TYPE_MAIN = 0

PLATFORM_ALLOW = (1, 2, 3, 5)             # 剧集只收本篇；SP/OP/ED/预告/MAD 一律不收
CV_TYPES = {0: "日语", 3: "中文"}

# 作品↔作品关系类型（由 16 部作品全样本反推）
REL_NAMES = {
    1: "原作", 2: "前传", 3: "续集", 4: "总集篇", 6: "OVA/OAD", 7: "其他",
    8: "衍生", 9: "恶搞", 10: "新作", 11: "特别篇", 12: "交叉", 14: "联动",
    99: "其他企划",
}

PLACEHOLDER_PERSONS = {"没有人"}
PLACEHOLDER_PREFIXES = ("[沙盒]",)

# search_index 权重
W_NAME = 100        # 主名（原名）
W_NAME_CN = 90      # 简体中文名
W_NAME_ZH = 85      # 其他中文名（第二中文名 / 中文别名 / 中国真人版名）
W_ALIAS = 70        # 其他语言名：日文名 / 罗马字 / 纯假名 / 英文名 / 各种译名
W_NICKNAME = 50     # 昵称类：外号 / 绰号 / 称号 / 网名

W_PERSON = 90       # 人物原名
W_PERSON_CN = 80    # 人物简体中文名
W_PERSON_ALIAS = 50 # 人物别名（第二中文名 / 昵称 / 旧艺名 / 马甲）

_NICKNAME_KEYS = ("昵称", "外号", "绰号", "诨号", "称号", "自称", "网名",
                  "Code Name", "识别名", "識別名")
_ZH_NAME_KEYS = ("中文名", "中文别名", "中国真人版名", "中文译名")


@dataclass
class ImportStats:
    """导入统计。"""

    skipped: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读 JSONL，跳过空行与坏行。"""
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _birthday_str(y: int | None, mo: int | None, d: int | None) -> str | None:
    """归一化成 YYYY-MM-DD（有年）或 MM-DD（无年）。"""
    if mo and d:
        return f"{y:04d}-{mo:02d}-{d:02d}" if y else f"{mo:02d}-{d:02d}"
    return None


def _txt(value: Any) -> str:
    """把 dump 里的字段值强制成文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_txt(x) for x in value)
    if isinstance(value, dict):
        return " ".join(_txt(x) for x in value.values())
    return str(value)


def _opt(value: Any) -> str | None:
    """同 _txt，但空串转 None。"""
    s = _txt(value).strip()
    return s or None


def _int(value: Any) -> int | None:
    """把字段值强制成整数。"""
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return parse_int(value)
    return None


def _score(value: Any) -> float | None:
    """番剧评分：保留一位小数；缺失返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(_txt(value).strip() or 0), 1)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    """把字段值强制成浮点。"""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(_txt(value).strip() or 0)
    except ValueError:
        return 0.0


def _is_placeholder(name: str) -> bool:
    return name in PLACEHOLDER_PERSONS or name.startswith(PLACEHOLDER_PREFIXES)


def _alias_weight(alias_type: str | None) -> int:
    """按类型名里的关键词定权重。顺序敏感：先判昵称，再判中文名。"""
    if alias_type == "主名":
        return W_NAME
    if not alias_type:
        return W_ALIAS
    if alias_type == "简体中文名":
        return W_NAME_CN
    if any(k in alias_type for k in _NICKNAME_KEYS):
        return W_NICKNAME
    if any(k in alias_type for k in _ZH_NAME_KEYS):
        return W_NAME_ZH
    return W_ALIAS


def import_dump(
    dump_dir: str | Path,
    db_path: str | Path,
    *,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> ImportStats:
    """把 dump 导入 SQLite。"""
    started = time.time()
    dump = Path(dump_dir)
    stats = ImportStats()

    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)

    conn.commit()

    # 整个导入必须是一个事务：中途被杀/报错时 SQLite 回滚，旧库仍然完整。
    conn.execute("PRAGMA foreign_keys = OFF")
    for _table in dbmod.all_table_names(conn):
        conn.execute("DELETE FROM " + _table)
    conn.execute("PRAGMA defer_foreign_keys = ON")

    log("[1/5] 扫描 subject.jsonlines…")
    anime: dict[int, dict[str, Any]] = {}
    total_subjects = 0
    sandbox = 0
    off_platform = 0
    for rec in _iter_jsonl(dump / "subject.jsonlines"):
        total_subjects += 1
        if rec.get("type") != SUBJECT_TYPE_ANIME:
            continue
        if _is_placeholder(_txt(rec.get("name"))):
            sandbox += 1
            continue
        if rec.get("platform") not in PLATFORM_ALLOW:
            off_platform += 1
            continue
        anime[rec["id"]] = rec
    log(f"      总条目 {total_subjects:,}，动画 {len(anime):,}"
        + (f"（过滤沙盒 {sandbox}、非收录类型 {off_platform}）"
           if sandbox or off_platform else ""))

    if limit is not None and limit < len(anime):
        ranked = sorted(
            anime.values(),
            key=lambda r: (r.get("favorite") or {}).get("done", 0),
            reverse=True,
        )[:limit]
        selected = {r["id"]: r for r in ranked}
        log(f"      按「看过人数」取 Top {len(selected)}")
    else:
        selected = anime
    work_ids = set(selected)
    log(f"      入选 {len(work_ids):,} 部")

    log("[2/5] 读取关联表…")
    work_chars: list[dict[str, Any]] = []
    for rec in _iter_jsonl(dump / "subject-characters.jsonlines"):
        if rec["subject_id"] in work_ids and rec.get("type") in WORK_CHARACTER_ROLES:
            work_chars.append(rec)
    char_ids = {r["character_id"] for r in work_chars}
    log(f"      作品↔角色 {len(work_chars):,} 条，涉及角色 {len(char_ids):,} 个")

    cv_records: list[dict[str, Any]] = []
    for rec in _iter_jsonl(dump / "person-characters.jsonlines"):
        if rec["subject_id"] in work_ids and rec.get("type") in CV_TYPES:
            cv_records.append(rec)
    cv_person_ids = {r["person_id"] for r in cv_records}
    log(f"      角色↔声优 {len(cv_records):,} 条，涉及声优 {len(cv_person_ids):,} 个")

    staff: dict[tuple[int, int, int], set[str]] = {}
    for rec in _iter_jsonl(dump / "subject-persons.jsonlines"):
        sid = rec.get("subject_id")
        if sid not in work_ids:
            continue
        pos = rec.get("position")
        if pos not in dbmod.STAFF_POSITIONS:
            continue
        eps = staff.setdefault((sid, rec["person_id"], pos), set())
        for e in (rec.get("appear_eps") or "").split(","):
            e = e.strip()
            if e:
                eps.add(e)
    staff_person_ids = {k[1] for k in staff}
    person_ids = cv_person_ids | staff_person_ids
    log(f"      作品↔制作人员 {len(staff):,} 条，涉及 {len(staff_person_ids):,} 人"
        f"（共 {len(person_ids):,} 个人物待导入）")

    raw_relations: list[dict[str, Any]] = []
    for rec in _iter_jsonl(dump / "subject-relations.jsonlines"):
        if rec["subject_id"] in work_ids and rec.get("relation_type") in REL_NAMES:
            raw_relations.append(rec)
    relations = [r for r in raw_relations if r["related_subject_id"] in work_ids]
    log(f"      作品↔作品 {len(raw_relations):,} 条 -> 两端均在库 {len(relations):,} 条")

    ep_rows: list[tuple[Any, ...]] = []
    ep_skipped_type = 0
    for rec in _iter_jsonl(dump / "episode.jsonlines"):
        if rec.get("subject_id") not in work_ids:
            continue
        if _int(rec.get("type")) != EPISODE_TYPE_MAIN:
            ep_skipped_type += 1
            continue
        ep_rows.append((
            rec["id"],
            rec["subject_id"],
            _float(rec.get("sort")),
            _opt(rec.get("name")),
            _opt(rec.get("name_cn")),
            split_description(_opt(rec.get("description"))),
            parse_date(rec.get("airdate")),
            parse_duration(rec.get("duration")),
        ))
    log(f"      剧集 {len(ep_rows):,} 条（跳过非本篇 {ep_skipped_type:,} 条），"
        f"有剧情简介 {sum(1 for r in ep_rows if r[5]):,} 条")

    log("[3/5] 读取 character.jsonlines / person.jsonlines…")
    characters: list[dict[str, Any]] = []
    for rec in _iter_jsonl(dump / "character.jsonlines"):
        if rec["id"] in char_ids and rec.get("role") == CHARACTER_ROLE_PERSON:
            if _is_placeholder(_txt(rec.get("name"))):
                continue
            characters.append(rec)
    kept_char_ids = {c["id"] for c in characters}
    stats.skipped["character_not_role1"] = len(char_ids) - len(characters)
    log(f"      角色 {len(characters):,} 个（滤掉非「角色」类型 {stats.skipped['character_not_role1']:,} 个）")

    persons: list[dict[str, Any]] = []
    for rec in _iter_jsonl(dump / "person.jsonlines"):
        if rec["id"] in person_ids and not _is_placeholder(rec.get("name", "")):
            persons.append(rec)
    kept_person_ids = {p["id"] for p in persons}

    log("[4/5] 写入稳定层（works / characters / aliases / cvs / relations）…")
    now = int(time.time())
    data_version = dump.name

    # 4.1 作品
    work_rows = []
    work_stats_rows = []
    tag_rows = []
    search_rows: list[tuple[str, str, int, int]] = []
    for rec in selected.values():
        infobox = parse_infobox(rec.get("infobox"))
        date_str = _opt(rec.get("date")) or parse_date(infobox.get("放送开始"))
        year = int(date_str[:4]) if date_str and date_str[:4].isdigit() else None
        season = f"{year}-{int(date_str[5:7]):02d}" if year and len(date_str) >= 7 and date_str[5:7].isdigit() else None
        work_rows.append((
            rec["id"], _txt(rec["name"]), _opt(rec.get("name_cn")),
            _opt(rec.get("platform")),
            date_str, year, season, parse_int(infobox.get("话数")),
            1 if rec.get("nsfw") else 0, _opt(rec.get("summary")),
            _opt(rec.get("infobox")), data_version, now,
        ))

        details = rec.get("score_details") or {}
        work_stats_rows.append((
            rec["id"],
            _score(rec.get("score")),
            sum(details.values()) if details else None,       # 评分人数
            _int(rec.get("rank")),
            _int((rec.get("favorite") or {}).get("done")),    # 看过人数
            data_version, now,
        ))

        for tag in rec.get("tags") or []:
            tag_rows.append((rec["id"], _txt(tag.get("name")),
                             _int(tag.get("count")) or 0, "user"))
        for tag in rec.get("meta_tags") or []:
            tag_rows.append((rec["id"], _txt(tag), 0, "meta"))

        # 检索索引：主名 / 中文名 / 别名
        for text, weight in (
            (_txt(rec["name"]), W_NAME),
            (_opt(rec.get("name_cn")), W_NAME_CN),
        ):
            norm = normalize_alias(text)
            if norm:
                search_rows.append((norm, "work", rec["id"], weight))
        for _kind, val in parse_alias_list(infobox):
            norm = normalize_alias(val)
            if norm:
                search_rows.append((norm, "work", rec["id"], W_ALIAS))

    conn.executemany(
        "INSERT OR REPLACE INTO works(work_id,name,name_cn,platform,date,year,season,"
        "episodes,nsfw,summary,infobox,data_version,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        work_rows,
    )

    # 4.2 角色 + 别名
    char_rows = []
    alias_rows = []
    for rec in characters:
        infobox = parse_infobox(rec.get("infobox"))
        y, mo, d = parse_birthday(infobox.get("生日"))
        name_cn = _opt(infobox.get("简体中文名"))
        char_rows.append((
            rec["id"], _txt(rec["name"]), name_cn,
            parse_gender(infobox.get("性别")), _birthday_str(y, mo, d),
            parse_blood_type(infobox.get("血型")),
            parse_length_cm(infobox.get("身高")), parse_weight_kg(infobox.get("体重")),
            _opt(rec.get("summary")), _opt(rec.get("infobox")),
            _int(rec.get("collects")) or 0, now,
        ))

        # (别名, 类型) —— 去重
        items: list[tuple[str, str]] = []
        if name_cn:
            items.append((name_cn, "简体中文名"))
        items.append((_txt(rec["name"]), "主名"))
        for kind, val in parse_alias_list(infobox):
            items.append((val, kind or "别名"))

        seen: set[str] = set()
        for alias, atype in items:
            alias = _txt(alias).strip()
            if not alias or alias in seen:
                continue
            seen.add(alias)
            norm = normalize_alias(alias)
            if not norm:
                continue
            alias_rows.append((rec["id"], alias, norm, atype, "bangumi"))
            search_rows.append((norm, "character", rec["id"], _alias_weight(atype)))

    conn.executemany(
        "INSERT OR REPLACE INTO characters(char_id,name,name_cn,gender,birthday,blood_type,"
        "height_cm,weight_kg,summary,infobox,collects,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        char_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO character_aliases(char_id,alias,alias_norm,alias_type,source)"
        " VALUES(?,?,?,?,?)",
        alias_rows,
    )

    person_rows = []
    for p in persons:
        ib = parse_infobox(p.get("infobox"))
        person_rows.append((
            p["id"], _txt(p.get("name")), _opt(ib.get("简体中文名")),
            _int(p.get("type")),
            json.dumps(p.get("career"), ensure_ascii=False) if p.get("career") else None,
            _opt(p.get("summary")), _opt(p.get("infobox")), now,
        ))
        # Bangumi person.type：1=个人，2=公司，3=组合（CLAMP / Daft Punk /
        # 藤子不二雄这类集体笔名）。后两种不是「真实人物」，索引里单独成 org，
        kind = "person" if _int(p.get("type")) == 1 else "org"
        names = [(p["name"], W_PERSON), (ib.get("简体中文名") or None, W_PERSON_CN)]
        names.extend((val, W_PERSON_ALIAS) for _kind, val in parse_alias_list(ib))
        seen: set[str] = set()
        for text, weight in names:
            if not text:
                continue
            norm = normalize_alias(text)
            if norm and norm not in seen:
                seen.add(norm)
                search_rows.append((norm, kind, p["id"], weight))
    conn.executemany(
        "INSERT OR REPLACE INTO persons(person_id,name,name_cn,type,career_json,"
        "summary,infobox,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        person_rows,
    )

    def _eps_key(token: str) -> tuple[int, str]:
        """集数排序：取区间起点做数值序，'6-8' 排在 4 和 10 之间。"""
        m = re.match(r"(\d+)", token)
        return (int(m.group(1)), token) if m else (10**9, token)

    staff_rows = [
        (wid, pid, pos, dbmod.STAFF_POSITIONS[pos],
         ",".join(sorted(eps, key=_eps_key)) or None)
        for (wid, pid, pos), eps in staff.items()
        if pid in kept_person_ids
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO work_staff(work_id,person_id,position,position_name,appear_eps)"
        " VALUES(?,?,?,?,?)",
        staff_rows,
    )
    dropped_staff = len(staff) - len(staff_rows)
    stats.skipped["staff_not_aligned"] = dropped_staff

    cv_rows = []
    dropped_cv = 0
    for rec in cv_records:
        if rec["character_id"] not in kept_char_ids or rec["person_id"] not in kept_person_ids:
            dropped_cv += 1
            continue
        cv_rows.append((
            rec["character_id"], rec["subject_id"], rec["person_id"],
            CV_TYPES[rec["type"]], rec["type"],
        ))
    stats.skipped["cv_not_aligned"] = dropped_cv
    conn.executemany(
        "INSERT OR REPLACE INTO character_cvs(char_id,work_id,person_id,lang,cv_type)"
        " VALUES(?,?,?,?,?)",
        cv_rows,
    )

    # 4.4 作品↔角色 与 作品↔作品
    wc_rows = [
        (r["subject_id"], r["character_id"], r["type"])
        for r in work_chars
        if r["character_id"] in kept_char_ids
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO work_characters(work_id,char_id,role) VALUES(?,?,?)",
        wc_rows,
    )
    rel_rows = [
        (r["subject_id"], r["related_subject_id"], r["relation_type"],
         REL_NAMES[r["relation_type"]])
        for r in relations
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO work_relations(work_id,related_id,rel_type,rel_name)"
        " VALUES(?,?,?,?)",
        rel_rows,
    )

    # 4.5 检索索引（去重，同 weight 取高者）
    best: dict[tuple[str, str, int], int] = {}
    for term, kind, tid, weight in search_rows:
        key = (term, kind, tid)
        if weight > best.get(key, -1):
            best[key] = weight
    conn.executemany(
        "INSERT OR REPLACE INTO work_episodes(ep_id,work_id,sort,name,name_cn,"
        "synopsis,airdate,duration_sec) VALUES(?,?,?,?,?,?,?,?)",
        ep_rows,
    )

    conn.executemany(
        "INSERT OR REPLACE INTO search_index(term_norm,target_kind,target_id,weight)"
        " VALUES(?,?,?,?)",
        [(t, k, i, w) for (t, k, i), w in best.items()],
    )

    log("[5/5] 写入易变层（work_stats / work_tags）…")
    conn.execute("DELETE FROM work_stats")
    conn.execute("DELETE FROM work_tags")
    conn.executemany(
        "INSERT OR REPLACE INTO work_stats(work_id,score,score_count,rank,"
        "favorite_done,source,updated_at) VALUES(?,?,?,?,?,?,?)",
        work_stats_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO work_tags(work_id,tag,count,kind) VALUES(?,?,?,?)",
        tag_rows,
    )
    for key, value in (
        ("data_version", data_version),
        ("last_import_at", now),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value))
        )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    stats.elapsed = time.time() - started
    conn.close()
    log(f"完成，耗时 {stats.elapsed:.1f}s")
    return stats
