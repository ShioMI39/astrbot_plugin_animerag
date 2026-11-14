from __future__ import annotations

import json
import re
from pathlib import Path

from astrbot.api.event import AstrMessageEvent

from ..core.db import base as db_base
from ..core.db.bgm import episode as ep_mod
from ..core.db.bgm import queries
from ..core.db.moe import body_cache as bc
from ..core.db.translate import queries as tr_queries
from ..core.db.translate.schema import is_japanese

# work_characters.role 存的是 Bangumi 的 1/2/3
ROLE_NAME = {1: "主角", 2: "配角", 3: "客串"}

# 属性展示顺序，和 commands/fmt.py 保持一致
ATTR_ORDER = ["萌点", "发色", "瞳色", "身高", "生日", "星座", "血型", "体重", "所属团体"]

# Bangumi 的角色简介里有 [mask]剧透[/mask] 标记，还有 \r 和零散换行
_MASK_RE = re.compile(r"\[/?mask\]")
_WS_RE = re.compile(r"\s+")


def _plain(text: str | None) -> str:
    """压成单行：去剧透标记、去 \r、折叠空白。"""
    if not text:
        return ""
    return _WS_RE.sub(" ", _MASK_RE.sub("", str(text))).strip()


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(value: float) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else ("%g" % f)


MOE_PAGE_CHARS = 1500      # 萌百正文每页字数
CHAR_WITH_SUMMARY = 40     # 带简介的角色数上限
CHAR_LINE_CAP = 58         # 限制文本在 60 行
EPISODE_PER_CALL = 20      # 单次最多取 20 集


class DetailMixin:
    def _work_characters(self, work_id: int) -> list[tuple]:
        """(char_id, 展示名, role, collects, summary, name, name_cn)"""
        return [
            (r["char_id"], r["name_cn"] or r["name"] or "#{}".format(r["char_id"]),
             r["role"], r["collects"] or 0, (r["summary"] or "").strip(),
             r["name"], r["name_cn"])
            for r in queries.get_work_characters(self.db.conn, work_id)
        ]

    @staticmethod
    def _name_wall(chars, budget: int) -> str:
        """把角色名挤成紧凑的一段。"""
        out: list[str] = []
        used = 0
        for name, role in chars:
            piece = name + ("（{}）".format(ROLE_NAME.get(role, "")) if role in ROLE_NAME else "")
            if used + len(piece) > budget:
                return "、".join(out) + "…（还有 {} 个）".format(len(chars) - len(out))
            out.append(piece)
            used += len(piece) + 1
        return "、".join(out)

    def _char_id_wall(self, chars, budget: int = 900) -> str:
        out: list[str] = []
        used = 0
        for c in chars:
            piece = "[角色#{}] {}".format(c[0], c[1])
            if c[2] in ROLE_NAME:
                piece += "（{}）".format(ROLE_NAME[c[2]])
            if used + len(piece) > budget:
                return "、".join(out) + "…（还有 {} 个）".format(len(chars) - len(out))
            out.append(piece)
            used += len(piece) + 1
        return "、".join(out)

    def _render_overview(self, wid: int, row) -> str:
        lines = [self._fmt_work(row)]
        chars = self._work_characters(wid)
        if chars:
            counts: dict[int, int] = {}
            for c in chars:
                counts[c[2]] = counts.get(c[2], 0) + 1
            head = "、".join(
                "{} {}".format(ROLE_NAME.get(r, "其他"), n)
                for r, n in sorted(counts.items())
            )
            lines.append("")
            lines.append("角色 {} 个（{}）：".format(len(chars), head))
            lines.append(self._name_wall(
                [(c[1], c[2]) for c in chars], budget=1200))
        nep = queries.count_episodes(self.db.conn, wid)
        if nep:
            lines.append("")
            lines.append("共 {} 话。".format(nep))
            lines.append(
                "角色表用 get_entity(kind=\"work\", id={}, part=\"characters\")；"
                "单集用 get_entity(kind=\"work\", id={}, part=\"episodes\", "
                "start=1, end={})。".format(wid, wid, min(nep, EPISODE_PER_CALL))
            )
        return "\n".join(lines)

    def _render_characters(self, wid: int, row) -> str:
        chars = self._work_characters(wid)
        title = row["name_cn"] or row["name"]
        if not chars:
            return "《{}》没有收录角色。".format(title)
        # 主角优先，其次按收藏数
        chars.sort(key=lambda c: (c[2] or 9, -c[3], c[0]))
        lines = ["《{}》角色 {} 个".format(title, len(chars))]
        shown = 0
        for c in chars:
            if shown >= CHAR_WITH_SUMMARY or len(lines) >= CHAR_LINE_CAP:
                break
            summary = _plain(self.zh_char.get(c[0]) or c[4])
            tag = "（{}）".format(ROLE_NAME.get(c[2], "")) if c[2] in ROLE_NAME else ""
            if summary:
                lines.append("  [角色#{}] {}{} — {}".format(
                    c[0], c[1], tag,
                    summary[:60] + "…" if len(summary) > 60 else summary))
            else:
                lines.append("  [角色#{}] {}{}".format(c[0], c[1], tag))
            shown += 1
        rest = chars[shown:]
        if rest:
            lines.append("")
            lines.append("其余 {} 个：{}".format(
                len(rest), self._char_id_wall(rest, budget=900)))
        lines.append("")
        lines.append(
            "看某个角色的完整资料用 get_entity(kind=\"character\", id=#)。")
        return "\n".join(lines)

    def _render_episodes(self, wid: int, row, start: float = 1, end: float = 12) -> str:
        title = row["name_cn"] or row["name"]
        s = max(1.0, _as_float(start, 1.0))
        e = max(s, _as_float(end, s))
        rows = queries.list_episodes(
            self.db.conn, wid, s, e, EPISODE_PER_CALL + 1)
        total = queries.count_episodes(self.db.conn, wid)
        if not rows:
            if s == e:
                return "《{}》没有第 {} 话（共 {} 话）。".format(
                    title, _fmt_num(s), total)
            return "《{}》没有第 {}-{} 话（共 {} 话）。".format(
                title, _fmt_num(s), _fmt_num(e), total)

        rows = rows[:EPISODE_PER_CALL]
        if s == e and len(rows) == 1:
            head0 = "《{}》第 {} 话".format(title, _fmt_num(rows[0]["sort"]))
        else:
            head0 = "《{}》第 {}-{} 话".format(
                title, _fmt_num(rows[0]["sort"]), _fmt_num(rows[-1]["sort"]))
        out = [head0]
        for r in rows:
            head = "第 {} 话".format(_fmt_num(r["sort"]))
            if r["name_cn"]:
                head += "「{}」".format(_plain(r["name_cn"]))
            if r["airdate"]:
                head += "　播出 {}".format(r["airdate"])
            out.append("")
            out.append(head)
            # 剥掉「脚本：xx / 分镜：xx」这类制作信息，再压成多行简介
            body = ep_mod.split_description(r["synopsis"]) or ""
            body = body.strip()
            out.append("  " + body[:200] + "…" if len(body) > 200
                       else ("  " + body if body else "  （没有简介）"))

        last = rows[-1]["sort"]
        shown = queries.count_episodes_upto(self.db.conn, wid, last)
        if total > shown:
            nxt = queries.next_episode_sort(self.db.conn, wid, last)
            if nxt is not None:
                out.append("")
                out.append("还有 {} 话，用 start={} 继续。".format(
                    total - shown, _fmt_num(nxt)))
        return "\n".join(out)

    # ═══════════════ get_entity ═══════════════

    async def get_entity(
        self,
        event: AstrMessageEvent,
        kind: str,
        id: int,
        part: str = "overview",
        page: int = 1,
        start: float = 1,
        end: float = 12,
    ) -> str:
        """按类型 + id 取完整词条（search_anime 返回的 [角色#123] 直接用）。

        work: 简介 / 角色表（带 char_id）/ 单集列表；character: 角色卡 + 萌百正文分页；
        person: 个人资料 + 参与作品；org: 公司/组合 + 参与作品；episode: 单集详情。
        moegirl 会按 character 处理（ref_id 就是 char_id）。

        Args:
            kind(string): work / character / person / org / episode
            id(number): 对应实体的 id（search_anime 返回里的 # 后面那个数）
            part(string): work 专用。overview=简介（默认）；characters=角色表；episodes=单集列表
            page(number): character 专用。萌百正文第几页，默认 1
            start(number): work + part=episodes 时起始集，支持小数（9.5）
            end(number): work + part=episodes 时结束集
        """
        await self._ensure()
        if self.db is None:
            return self._missing_message()
        try:
            rid = int(id)
        except (TypeError, ValueError):
            return "id 必须是整数。"

        k = (kind or "").strip().lower()
        if k in ("moegirl", "萌百", "moe"):
            k = "character"
        if k in ("work", "番剧", "作品", "anime"):
            row = self._work_row(rid)
            if row is None:
                return "没有找到作品 #{}。先调 search_anime 确认 id。".format(rid)
            p = (part or "overview").strip().lower()
            if p == "characters":
                return self._render_characters(rid, row)
            if p == "episodes":
                return self._render_episodes(rid, row, start, end)
            return self._render_overview(rid, row)

        if k in ("character", "char", "角色"):
            d = self._char_row(rid)
            if d is None:
                return "没有找到角色 #{}。先调 search_anime 确认 id。".format(rid)
            pages = self._moe_pages(rid)
            try:
                pno = max(1, int(page or 1))
            except (TypeError, ValueError):
                pno = 1
            body = pages[pno - 1] if 0 < pno <= len(pages) else ""
            text = self._fmt_char(d, body="", works_limit=12)
            if pages:
                text += "\n\n【萌娘百科】（第 {}/{} 页）\n{}".format(
                    pno, len(pages), body)
                if pno < len(pages):
                    text += "\n\n（还有 {} 页，用 page={} 继续）".format(
                        len(pages) - pno, pno + 1)
            elif self.enable_moegirl:
                text += "\n\n（这个角色没有萌百正文）"
            return text

        if k in ("episode", "ep", "单集"):
            return self._render_episode(rid)

        if k in ("person", "人物", "真人", "org", "company", "公司", "组合"):
            row = queries.get_person(self.db.conn, rid)
            if row is None:
                return "没有找到人物/公司 #{}。先调 search_anime 确认 id。".format(rid)
            ptype = int(row["type"] or 1)
            want_org = k in ("org", "company", "公司", "组合")
            if want_org and ptype == 1:
                return "「{}」是个人，不是公司/组合。请用 kind=\"person\"".format(
                    row["name_cn"] or row["name"])
            if (not want_org) and ptype != 1:
                return "「{}」是公司/组合。请用 kind=\"org\"".format(
                    row["name_cn"] or row["name"])

            name = row["name_cn"] or row["name"]
            label = {1: "个人", 2: "公司", 3: "组合"}.get(ptype, "人物")
            lines = ["【{}】（{}）".format(name, label)]
            if row["name"] and row["name_cn"]:
                lines.append("原名：{}".format(row["name"]))
            career = row["career_json"] or ""
            if career:
                try:
                    arr = json.loads(career)
                    if isinstance(arr, list) and arr:
                        lines.append("职业：{}".format("、".join(str(x) for x in arr)))
                except (TypeError, ValueError):
                    pass
            summary = (row["summary"] or "").strip()
            if summary:
                lines.append("")
                lines.append(summary[:600])
            lines.append("")
            if ptype == 1:
                lines.extend(self._filmography(rid, name, 20))
            else:
                lines.append(self._org_works(rid, 20))
            return "\n".join(lines)

        return "kind 只支持 work / character / person / org / episode。"

    def _render_episode(self, ep_id: int) -> str:
        row = queries.get_episode(self.db.conn, ep_id)
        if row is None:
            return "没有找到单集 #{}。".format(ep_id)
        head = "《{}》第 {} 话".format(row["work_title"], _fmt_num(row["sort"]))
        if row["name_cn"]:
            head += "「{}」".format(_plain(row["name_cn"]))
        if row["name"] and row["name"] != row["name_cn"]:
            head += " / {}".format(_plain(row["name"]))
        if row["airdate"]:
            head += "　播出 {}".format(row["airdate"])
        body = (ep_mod.split_description(row["synopsis"]) or "").strip()
        return head + "\n\n" + (body if body else "（没有简介）")

    # ═══════════════ get_translation ═══════════════

    async def get_translation(
        self,
        event: AstrMessageEvent,
        kind: str,
        id: int,
    ) -> str:
        """按类型 + id 取中文译文。

        work / character / episode；没有译文时返回日文原文（并注明），
        原文本身是中文时直接返回原文。episode 目前译文覆盖很低。

        Args:
            kind(string): work / character / episode
            id(number): 对应实体的 id
        """
        await self._ensure()
        if self.db is None:
            return self._missing_message()
        try:
            rid = int(id)
        except (TypeError, ValueError):
            return "id 必须是整数。"

        k = (kind or "").strip().lower()
        if k in ("moegirl", "萌百", "moe", "character", "char", "角色"):
            k = "character"
        elif k in ("work", "番剧", "作品", "anime"):
            k = "work"
        elif k in ("episode", "ep", "单集"):
            k = "episode"
        else:
            return "kind 只支持 work / character / episode。"

        header = ""
        source = ""
        if k == "work":
            row = queries.get_work(self.db.conn, rid)
            if row is None:
                return "没有找到作品 #{}。".format(rid)
            header = "《{}》".format(row["name_cn"] or row["name"] or "")
            source = row["summary"] or ""
        elif k == "character":
            row = queries.get_character(self.db.conn, rid)
            if row is None:
                return "没有找到角色 #{}。".format(rid)
            header = "【{}】".format(row["name_cn"] or row["name"] or "")
            source = row["summary"] or ""
        else:
            row = queries.get_episode_source(self.db.conn, rid)
            if row is None:
                return "没有找到单集 #{}。".format(rid)
            header = "《{}》第 {} 话".format(
                row["work_title"] or "", _fmt_num(row["sort"]))
            source = row["synopsis"] or ""

        dst = None
        tr_path = Path(self.translate_db)
        if tr_path.is_file():
            conn = None
            try:
                conn = db_base.connect_readonly(tr_path, timeout=10)
                dst = tr_queries.get(conn, k, rid)
            except Exception:  # noqa: BLE001
                dst = None
            finally:
                if conn is not None:
                    conn.close()

        if dst and str(dst).strip():
            return "{}\n\n{}".format(header, str(dst).strip())
        source = str(source or "").strip()
        if not source:
            return "{}没有可翻译的文本。".format(header)
        if is_japanese(source):
            return "{}\n\n暂无译文，返回日文原文：\n{}".format(
                header, source[:2000])
        return "{}\n\n原文是中文，无需翻译：\n{}".format(header, source[:2000])

    # ═══════════════ 萌百正文分页 ═══════════════

    def _moe_bodies(self) -> dict:
        if self._moe_cache is not None:
            return self._moe_cache
        self._moe_cache = {}
        if self.enable_moegirl:
            path = Path(self.moegirl_bodies)
            if path.is_file():
                try:
                    self._moe_cache = bc.load(path)
                except Exception:  # noqa: BLE001
                    self._moe_cache = {}
        return self._moe_cache

    def _moe_pages(self, char_id: int) -> list[str]:
        """把萌百正文按章节切成每页约 1500 字。"""
        bodies = self._moe_bodies()
        entry = bc.get(bodies, char_id) if bodies else None
        if not entry:
            return []
        blocks: list[str] = []
        for title, text in bc.sections_of(entry).items():
            text = (text or "").strip()
            if text:
                blocks.append("【{}】\n{}".format(title, text))
        if not blocks:
            return []

        pages: list[str] = []
        cur: list[str] = []
        used = 0
        for block in blocks:
            # 单节就超长时先独成一页，再硬切
            while len(block) > MOE_PAGE_CHARS:
                if cur:
                    pages.append("\n\n".join(cur))
                    cur, used = [], 0
                pages.append(block[:MOE_PAGE_CHARS])
                block = block[MOE_PAGE_CHARS:]
            if cur and used + len(block) > MOE_PAGE_CHARS:
                pages.append("\n\n".join(cur))
                cur, used = [], 0
            cur.append(block)
            used += len(block) + 2
        if cur:
            pages.append("\n\n".join(cur))
        return pages
