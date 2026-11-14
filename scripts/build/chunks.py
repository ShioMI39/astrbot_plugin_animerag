from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import config
from core.db.moe import body_cache as bc
from core.search.body_clean import clean_body_text
from core.search.chunking import (
    ATTR_KEYS,
    attr_block,
    make_chunk,
    name_line,
    split_text,
)
from core.db.bgm.schema import Database

REL_SEQUEL = (2, 3)
REL_NOISY = (7, 9, 14, 99)
LCS_MIN = 6

PLATFORM_NAMES = {1: "TV", 2: "OVA", 3: "剧场版", 5: "Web"}

# 萌百章节里要和 bgm 简介去重的
MOE_SKIP_SECTIONS = {"简介", "角色简介", "人物简介", "概述"}

_KANA_RE = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u30fc\u30fb\s]+$")
_LATIN_RE = re.compile(r"^[A-Za-z0-9 .\-''&]+$")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_NAME_SEP_RE = re.compile(r"[，,、/;；()（）]")

_NICKNAME_TYPES = (
    "昵称", "外号", "绰号", "称号", "别号", "别称", "网名", "自称",
    "代号", "code name", "通称", "爱称", "诨号", "俗称", "小名", "艺名",
)
_CHINESE_TYPES = (
    "中文", "简体", "繁体", "正體", "译名", "譯名", "台译", "港译", "台譯", "港譯",
)
_JAPANESE_TYPES = (
    "日文", "本名", "真名", "原名", "全名", "旧名", "曾用名",
)


def _alias_rank(alias_type: str, text: str) -> int | None:
    """别名优先级：昵称 0、中文 1、日文汉字 2；纯假名/罗马音/英文返回 None。"""
    t = (alias_type or "").lower()
    if not text or _KANA_RE.fullmatch(text) or _LATIN_RE.fullmatch(text):
        return None
    if not _HAN_RE.search(text):
        return None
    if any(k in t for k in _NICKNAME_TYPES):
        return 0
    if any(k in t for k in _CHINESE_TYPES):
        return 1
    if any(k in t for k in _JAPANESE_TYPES):
        return 2
    if t in ("别名", "别称", "又名", "译名", "舊名", "旧名"):
        return 1
    return None


def longest_common(a: str, b: str) -> int:
    """最长公共子串长度。识别「同一系列但名字不是包含关系」的作品。

    Code Geass 反叛的鲁路修 / Code Geass 复活的鲁路修   LCS=11
    化物语 / 猫物语（黑）                              LCS=2   真不同
    魔法禁书目录 / 某科学的超电磁炮                       LCS=1   平行外传
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def fmt_sort(s: float | None) -> str:
    """1.0 -> 第1集  11.5 -> 第11.5集  None -> 空"""
    if s is None:
        return ""
    return "第{}集".format(int(s) if float(s).is_integer() else s)


class Builder:
    def __init__(self, anime_db: str, moegirl_db: str, bodies: str) -> None:
        self.db = Database(anime_db)
        self.conn = self.db.conn

        # 萌百是可选的：只建 Bangumi 部分没有这两个文件。
        self.mg = None
        if Path(moegirl_db).is_file():
            self.mg = sqlite3.connect(
                "file:{}?mode=ro".format(moegirl_db), uri=True, timeout=30
            )
            self.mg.row_factory = sqlite3.Row
        self.bodies = bc.load(bodies) if Path(bodies).is_file() else {}
        self._preload()

    def _preload(self) -> None:
        t0 = time.time()
        c = self.conn

        # 作品元信息
        self.work_meta: dict[int, tuple[str, str, str, int]] = {
            r["work_id"]: (r["name"] or "", r["name_cn"] or "", r["date"] or "", r["platform"])
            for r in c.execute("SELECT work_id, name, name_cn, date, platform FROM works")
        }

        # 角色 → 作品列表
        self.char_works: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for r in c.execute("SELECT char_id, work_id, role FROM work_characters"):
            self.char_works[r["char_id"]].append((r["work_id"], r["role"]))

        # 续集/前传邻接
        self.adj: dict[int, set[int]] = collections.defaultdict(set)
        for r in c.execute(
            "SELECT work_id, related_id FROM work_relations WHERE rel_type IN (?,?)",
            REL_SEQUEL,
        ):
            self.adj[r["work_id"]].add(r["related_id"])
            self.adj[r["related_id"]].add(r["work_id"])

        # 联动/恶搞关系对（只需判断存在性）
        self.noisy: set[tuple[int, int]] = set()
        for r in c.execute(
            "SELECT work_id, related_id FROM work_relations WHERE rel_type IN ({})".format(
                ",".join("?" * len(REL_NOISY))),
            REL_NOISY,
        ):
            self.noisy.add((r["work_id"], r["related_id"]))
            self.noisy.add((r["related_id"], r["work_id"]))

        self.char_names: dict[int, list[tuple[int, str]]] = collections.defaultdict(list)
        for r in c.execute("SELECT char_id, name, name_cn FROM characters"):
            if r["name_cn"]:
                self.char_names[r["char_id"]].append((0, r["name_cn"]))
            if r["name"]:
                self.char_names[r["char_id"]].append((1, r["name"]))
        n_alias = 0
        prio_map = {"罗马字": 2, "英文名": 2, "简体中文名": 3, "第二中文名": 3,
                    "别名": 4, "昵称": 4}
        for r in c.execute(
            "SELECT char_id, alias, alias_type FROM character_aliases"
        ):
            p = prio_map.get(r["alias_type"])
            if p is not None:
                self.char_names[r["char_id"]].append((p, r["alias"]))
            n_alias += 1

        # 角色头用的主名 + 原始别名（带类型），用来做「昵称优先、去假名/罗马音」
        self.char_main: dict[int, tuple[str, str]] = {}
        for r in c.execute("SELECT char_id, name, name_cn FROM characters"):
            self.char_main[int(r["char_id"])] = (r["name"] or "", r["name_cn"] or "")
        self.char_alias_meta: dict[int, list[tuple[str, str]]] = collections.defaultdict(list)
        for r in c.execute("SELECT char_id, alias, alias_type FROM character_aliases"):
            self.char_alias_meta[int(r["char_id"])].append(
                (r["alias_type"] or "", r["alias"] or ""))

        self.zh: dict[int, str] = {}          # 角色简介译文
        self.work_zh: dict[int, str] = {}     # 番剧简介译文
        tr_path = Path("data/translate.db")
        if tr_path.exists():
            tdb = sqlite3.connect("file:{}?mode=ro".format(tr_path), uri=True, timeout=30)
            for r in tdb.execute(
                "SELECT kind, ref_id, dst_text FROM translations"
            ):
                if r[0] == "character":
                    self.zh[r[1]] = r[2]
                elif r[0] == "work":
                    self.work_zh[r[1]] = r[2]
            tdb.close()

        # 萌百属性
        self.attrs: dict[int, dict[str, list[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(list))
        n_attr = 0
        if self.mg is not None:
            for r in self.mg.execute(
                "SELECT char_id, attr_key, attr_value FROM character_attributes"
            ):
                if r["attr_key"] in ATTR_KEYS:
                    self.attrs[r["char_id"]][r["attr_key"]].append(r["attr_value"])
                    n_attr += 1

        print("  预载完成 {:.1f}s：作品 {:,} / 角色-作品 {:,} 条 / 别名 {:,} / 属性 {:,}"
        " / 角色译文 {:,} / 番剧译文 {:,}".format(
            time.time() - t0, len(self.work_meta), sum(len(v) for v in self.char_works.values()),
            n_alias, n_attr, len(self.zh), len(self.work_zh)), flush=True)

    def pick_names(self, char_id: int, limit: int = 6) -> list[str]:
        """挑最多 limit 个名字，过滤掉昵称串和超长别名。"""
        out: list[str] = []
        for _, n in sorted(self.char_names.get(char_id, [])):
            n = (n or "").strip()
            if not n or n in out:
                continue
            if len(n) > 24:                          # 「天才少女，THE ZOMBIE/复苏者，…」
                continue
            if any(ch in n for ch in "，,、/;"):        # 昵称列表
                continue
            out.append(n)
            if len(out) >= limit:
                break
        return out

    def head_names(self, char_id: int, limit: int = 3) -> list[str]:
        """角色块头：主名 + 最多 3 个高权重别名。

        昵称优先，其次中文别名，再次日文汉字名
        """
        name, name_cn = self.char_main.get(char_id, ("", ""))
        main = (name_cn or name).strip()
        out = [main] if main else []
        seen = {main} if main else set()
        ranked: list[tuple[int, int, str]] = []

        for text in (name, name_cn):
            text = (text or "").strip()
            if not text or text in seen:
                continue
            if len(text) > 24 or _NAME_SEP_RE.search(text):
                continue
            if _KANA_RE.fullmatch(text) or _LATIN_RE.fullmatch(text):
                continue
            if not _HAN_RE.search(text):
                continue
            ranked.append((1, len(text), text))

        for alias_type, alias in self.char_alias_meta.get(char_id, []):
            text = (alias or "").strip()
            if not text or text in seen:
                continue
            if len(text) > 24 or _NAME_SEP_RE.search(text):
                continue
            rank = _alias_rank(alias_type, text)
            if rank is None:
                continue
            ranked.append((rank, len(text), text))

        ranked.sort()
        for _, _, text in ranked:
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) > limit:
                break
        return out[:limit + 1]

    def display_name(self, char_id: int) -> str:
        """展示用名字：中文名优先。"""
        ns = self.pick_names(char_id, 1)
        return ns[0] if ns else "角色{}".format(char_id)

    def label(self, work_id: int) -> str:
        m = self.work_meta.get(work_id)
        if not m:
            return ""
        return m[1] or m[0]      # 中文名优先

    def pick_works(self, char_id: int, k: int = 2) -> list[int]:
        """来源番剧：role=1 主角优先 → TV 优先 → 日期升序，取前 k 部不同系列。"""
        ws = self.char_works.get(char_id) or []
        if not ws:
            return []

        def key(item: tuple[int, int]) -> tuple:
            wid, role = item
            name, name_cn, date, platform = self.work_meta.get(wid, ("", "", "", 0))
            return (0 if role == 1 else 1,
                    0 if platform == 1 else 1,
                    1 if not date else 0,
                    date, wid)

        ws = sorted(ws, key=key)
        first = ws[0][0]
        same = set(self.adj.get(first, ())) | {first}
        base = self.label(first)

        out = [first]
        for wid, role in ws[1:]:
            if len(out) >= k:
                break
            if wid in same or role != 1:
                continue
            if self.work_meta.get(wid, ("", "", "", 0))[3] != 1:
                continue
            if (first, wid) in self.noisy:
                continue
            nm = self.label(wid)
            if base and nm and (base in nm or nm in base or longest_common(base, nm) >= LCS_MIN):
                continue
            out.append(wid)
        return out


def build_work_chunks(b: Builder, limit: int | None) -> Iterator[dict[str, Any]]:
    rows = b.conn.execute(
        "SELECT work_id, name, name_cn, summary, platform FROM works"
        " WHERE summary IS NOT NULL AND summary != ''"
        + (" LIMIT {}".format(limit) if limit else "")
    ).fetchall()
    for r in rows:
        wid = r["work_id"]
        name, name_cn, date, platform = b.work_meta.get(wid, ("", "", "", r["platform"]))
        title = name_cn or name or "作品{}".format(wid)
        head = name_line([name_cn, name])
        head = "{} {}".format(head, PLATFORM_NAMES.get(platform, "")).strip()
        head += " 作品简介"
        summary = (b.work_zh.get(wid) or r["summary"] or "").strip()
        if not summary:
            continue
        for i, piece in enumerate(split_text(summary)):
            yield make_chunk(
                cid="work:{}:{}".format(wid, i), kind="work", ref_id=wid,
                title=title, text=piece, embed_head=head,
                work_id=wid, platform=platform,
            )


def build_character_chunks(b: Builder, limit: int | None) -> Iterator[dict[str, Any]]:
    rows = b.conn.execute(
        "SELECT char_id, name, name_cn, summary FROM characters"
        + (" ORDER BY collects DESC LIMIT {}".format(limit) if limit else "")
    ).fetchall()
    for r in rows:
        cid = r["char_id"]
        if cid not in b.char_works:
            continue
        attrs = b.attrs.get(cid) or {}
        # 优先用中文译文；没有译文才退回日文原文
        summary = (b.zh.get(cid) or r["summary"] or "").strip()
        if not summary and not attrs:
            continue

        works = b.pick_works(cid)
        work_names = [b.label(w) for w in works]
        work_names = [x for x in work_names if x]
        display = b.display_name(cid)
        title = display + (" · " + work_names[0] if work_names else "")

        ablock = attr_block(attrs)
        if not ablock and not summary:
            continue

        pieces = split_text(summary) if summary else []
        if ablock:
            if pieces:
                pieces[0] = ablock + "\n" + pieces[0]
                # 拼完可能超长，再切一次
                if len(pieces[0]) > 600:
                    head_pieces = split_text(pieces[0])
                    pieces = head_pieces + pieces[1:]
            else:
                pieces = [ablock]

        base = "{} {} 角色卡".format(name_line(b.head_names(cid)), " ".join(work_names))
        base = " ".join(base.split())
        attr_flat = " ".join(ablock.replace("：", " ").split())
        platform = b.work_meta.get(works[0], ("", "", "", 0))[3] if works else 0

        for i, piece in enumerate(pieces):
            head = base + (" " + attr_flat + " 简介" if i > 0 else "")
            if len(pieces) > 1:
                head += " ({}/{})".format(i + 1, len(pieces))
            yield make_chunk(
                cid="character:{}:{}".format(cid, i), kind="character", ref_id=cid,
                title=title, text=piece, embed_head=head,
                work_id=works[0] if works else None, platform=platform,
            )


def build_episode_chunks(b: Builder, limit: int | None) -> Iterator[dict[str, Any]]:
    rows = b.conn.execute(
        "SELECT ep_id, work_id, sort, name, name_cn, synopsis FROM work_episodes"
        " WHERE synopsis IS NOT NULL AND synopsis != ''"
        + (" LIMIT {}".format(limit) if limit else "")
    ).fetchall()
    for r in rows:
        wid = r["work_id"]
        name, name_cn, date, platform = b.work_meta.get(wid, ("", "", "", 0))
        wlabel = name_cn or name or "作品{}".format(wid)
        ep = fmt_sort(r["sort"])
        ep_cn = (r["name_cn"] or "").strip()
        ep_jp = (r["name"] or "").strip()
        title = "{} {} {}".format(wlabel, ep, ep_cn or ep_jp).strip()
        head = name_line([wlabel, name, ep, ep_cn, ep_jp]) + " 剧情"
        for i, piece in enumerate(split_text(r["synopsis"])):
            yield make_chunk(
                cid="episode:{}:{}".format(r["ep_id"], i), kind="episode",
                ref_id=r["ep_id"], title=title, text=piece, embed_head=head,
                work_id=wid, platform=platform, sort=r["sort"],
            )


def merge_moe_sections(secs: dict[str, str]) -> list[tuple[str, str]]:
    """合并过短的章节（<120 字符），并跳过与 bgm 简介重复的章节。"""
    items = []
    for t_, x_ in secs.items():
        cleaned = clean_body_text(x_ or "")
        if cleaned.strip():
            items.append((t_, cleaned))
    kept = [(t, x) for t, x in items if t not in MOE_SKIP_SECTIONS]
    items = kept if kept else items
    out: list[tuple[str, str]] = []
    for t, x in items:
        if out and len(out[-1][1]) < 120:
            pt, px = out[-1]
            out[-1] = ("{}·{}".format(pt, t), px + "\n" + x)
        else:
            out.append((t, x))
    return out


def build_moegirl_chunks(b: Builder, limit: int | None) -> Iterator[dict[str, Any]]:
    pages = list(b.bodies.get("pages", {}).items())
    if not pages:
        return
    if limit:
        pages = pages[:limit]
    for key, entry in pages:
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        secs = bc.sections_of(entry)
        if not secs:
            continue
        works = b.pick_works(cid)
        work_names = [x for x in (b.label(w) for w in works) if x]
        platform = b.work_meta.get(works[0], ("", "", "", 0))[3] if works else 0
        names = name_line(b.head_names(cid))
        display = b.display_name(cid)

        for seq, (sec, text) in enumerate(merge_moe_sections(secs)):
            title = "{} · {}".format(display, sec)
            head = "{} {} 页面 {}".format(names, " ".join(work_names), sec)
            head = " ".join(head.split())
            pieces = split_text(text)
            for j, piece in enumerate(pieces):
                h = head if len(pieces) == 1 else "{} ({}/{})".format(head, j + 1, len(pieces))
                yield make_chunk(
                    cid="moegirl:{}:{}{}".format(cid, seq, "" if j == 0 else "-{}".format(j)),
                    kind="moegirl", ref_id=cid, title=title, text=piece,
                    embed_head=h, work_id=works[0] if works else None,
                    platform=platform, section=sec,
                )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anime-db", default=str(config.anime_db()))
    ap.add_argument("--moegirl-db", default=str(config.moegirl_db()))
    ap.add_argument("--bodies", default=str(config.data_dir() / "moegirl_bodies.json"))
    ap.add_argument("--out", default=str(config.vec_dir() / "chunks.jsonl"))
    ap.add_argument("--limit", type=int, default=None, help="每种块各取前 N 条（调试验证用）")
    ap.add_argument("--only", default=None, help="只生成这几种，逗号分隔：work,character,episode,moegirl")
    ap.add_argument(
        "--with-moegirl", action="store_true",
        help="即使插件配置里关掉了 enable_moegirl 也切萌百块",
    )
    args = ap.parse_args()

    default_kinds = {"work", "character", "episode"}
    # 萌百块只在开关打开时才切 —— 只建 Bangumi 部分时不用准备 bodies 文件
    if config.get("enable_moegirl", True) or args.with_moegirl:
        default_kinds.add("moegirl")
    only = set(args.only.split(",")) if args.only else default_kinds
    t0 = time.time()
    print("载入数据…", flush=True)
    b = Builder(args.anime_db, args.moegirl_db, args.bodies)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    n = 0
    tmp = out_path.with_suffix(".tmp")

    gens = []
    if "work" in only:
        gens.append(("work", build_work_chunks(b, args.limit)))
    if "character" in only:
        gens.append(("character", build_character_chunks(b, args.limit)))
    if "episode" in only:
        gens.append(("episode", build_episode_chunks(b, args.limit)))
    if "moegirl" in only:
        gens.append(("moegirl", build_moegirl_chunks(b, args.limit)))

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for kind, gen in gens:
            k = 0
            for ch in gen:
                if ch["id"] in seen_ids:
                    continue
                seen_ids.add(ch["id"])
                ch["hash"] = hashlib.sha256(ch["embed"].encode("utf-8")).hexdigest()[:16]
                f.write(json.dumps(ch, ensure_ascii=False) + "\n")
                k += 1
                n += 1
                if n % 20000 == 0:
                    print("  {:,} 块…".format(n), flush=True)
            counts[kind] = k
            print("  {:<10}{:>9,} 块".format(kind, k), flush=True)

    tmp.replace(out_path)

    meta = {
        "version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "embed_model": "BAAI/bge-m3",
        "dim": 1024,
        "counts": counts,
        "total": n,
        "anime_db": {"works": len(b.work_meta)},
        "moegirl_db": {"pages": len(b.bodies.get("pages", {}))},
    }
    (out_path.parent / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    size = out_path.stat().st_size / 2**20
    print("\n完成 {:.1f}s   共 {:,} 块   {:.1f} MB".format(time.time() - t0, n, size))
    print("  " + "  ".join("{} {:,}".format(k, v) for k, v in counts.items()))
    b.db.close()
    if b.mg is not None:
        b.mg.close()


if __name__ == "__main__":
    main()
