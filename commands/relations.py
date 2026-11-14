from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..core.db.bgm import queries


# 作品关系的展示顺序：先看剧情连续，再看衍生
SEQUEL_TYPES = ("续集", "前传", "新作", "衍生", "OVA/OAD", "特别篇", "总集篇", "交叉", "其他")
STUDIO_LABEL = {67: "动画制作", 42: "製作"}

MAX_LIMIT = 60

MODE_DESC = {
    "sequel": "续作 / 前传 / 衍生 / 总集篇",
    "studio": "制作公司；公司参与的作品",
    "cvs": "角色的声优",
    "works": "角色登场作品 / 人物参与作品",
}


class RelationMixin:
    async def get_relations(
        self,
        event: AstrMessageEvent,
        kind: str,
        id: int,
        relation: str = "auto",
        limit: int = 20,
    ) -> str:
        """按 kind + id 查实体关系：续作、制作公司、声优、参与作品。

        kind=work:      relation=sequel（默认）看相关作品；studio 看制作公司
        kind=character: relation=cvs（默认）看声优；works 看登场作品
        kind=person:    relation=works，列他参与的作品，并标明配音角色/制作职位
        kind=org:       relation=studio（默认）或 works，列公司参与的作品

        Args:
            kind(string): work / character / person / org
            id(number): search_anime 或 get_entity 返回的 # 后面的数字
            relation(string): auto=按 kind 选默认；work: sequel/studio；
                character: cvs/works；person: works；org: studio/works
            limit(number): 最多列多少部/多少人，默认 20，上限 60
        """
        await self._ensure()
        if self.db is None:
            return self._missing_message()
        try:
            eid = int(id)
        except (TypeError, ValueError):
            return "id 必须是整数。"

        k = (kind or "").strip().lower()
        if k in ("moegirl", "萌百", "moe"):
            k = "character"
        elif k in ("work", "番剧", "作品", "anime"):
            k = "work"
        elif k in ("character", "char", "角色"):
            k = "character"
        elif k in ("person", "人物", "真人"):
            k = "person"
        elif k in ("org", "company", "公司", "组合"):
            k = "org"
        else:
            return "kind 只支持 work / character / person / org。"

        try:
            n = max(1, min(int(limit or 20), MAX_LIMIT))
        except (TypeError, ValueError):
            n = 20
        mode = (relation or "auto").strip().lower()

        if k == "work":
            row = self._work_row(eid)
            if row is None:
                return "没有找到作品 #{}。先调 search_anime 确认 id。".format(eid)
            modes = ("sequel", "studio")
            if mode == "auto":
                mode = "sequel"
            if mode not in modes:
                return self._hint(k, eid, mode, modes)
            if mode == "sequel":
                return self._rel_sequel(eid, n)
            return self._rel_studio_work(eid, n)

        if k == "character":
            row = self._char_row(eid)
            if row is None:
                return "没有找到角色 #{}。先调 search_anime 确认 id。".format(eid)
            modes = ("cvs", "works")
            if mode == "auto":
                mode = "cvs"
            if mode not in modes:
                return self._hint(k, eid, mode, modes)
            if mode == "cvs":
                return self._rel_cvs(eid, n)
            return self._rel_character_works(eid, n)

        # person / org 共用 persons 表，靠 type 区分
        row = queries.get_person(self.db.conn, eid)
        if row is None:
            return "没有找到人物/公司 #{}。先调 search_anime 确认 id。".format(eid)
        ptype = int(row["type"] or 1)
        if k == "person" and ptype != 1:
            return "「{}」是公司/组合，请用 kind=\"org\"。".format(
                row["name_cn"] or row["name"])
        if k == "org" and ptype == 1:
            return "「{}」是个人，请用 kind=\"person\"。".format(
                row["name_cn"] or row["name"])

        if ptype == 1:
            modes = ("works",)
            if mode == "auto":
                mode = "works"
            if mode not in modes:
                return self._hint(k, eid, mode, modes)
            return "\n".join(self._filmography(eid, n))

        modes = ("studio", "works")
        if mode == "auto":
            mode = "studio"
        if mode not in modes:
            return self._hint(k, eid, mode, modes)
        return self._org_works(eid, n)

    @staticmethod
    def _hint(kind: str, eid: int, mode: str, modes: tuple[str, ...]) -> str:
        lines = [
            "kind={} id={} 不支持 relation=\"{}\"。可选：".format(kind, eid, mode)
        ]
        for m in modes:
            lines.append("  relation=\"{}\"    {}".format(m, MODE_DESC.get(m, "")))
        return "\n".join(lines)

    # ═══════════════ sequel ═══════════════

    def _rel_sequel(self, wid: int, n: int) -> str:
        work = queries.get_work(self.db.conn, wid)
        title = (work["name_cn"] or work["name"]) if work else "#{}".format(wid)
        rows = queries.get_work_relations(self.db.conn, wid)
        if not rows:
            return "《{}》在 Bangumi 里没有登记关联作品。".format(title)

        buckets: dict[str, list] = {}
        missing = 0
        for r in rows:
            r["t"] = r["name_cn"] or r["name"]
            if r["t"] is None:
                missing += 1
                continue
            buckets.setdefault(r["rel_name"] or "其他", []).append(r)
        if not buckets:
            return "《{}》有 {} 个关联条目，但都不在动画库里。".format(title, missing)

        out = ["《{}》的相关作品".format(title)]
        shown = 0
        for rel in SEQUEL_TYPES + tuple(k for k in buckets if k not in SEQUEL_TYPES):
            items = buckets.get(rel)
            if not items:
                continue
            out.append("")
            out.append("{}（{}）".format(rel, len(items)))
            for r in items:
                if shown >= n:
                    break
                out.append("[作品#{}] {}  {}".format(
                    r["related_id"], str(r["t"])[:40], r["date"] or ""))
                shown += 1
            if shown >= n:
                break
        if shown >= n and sum(len(v) for v in buckets.values()) > n:
            out.append("")
            out.append("（共 {} 部，这里列了前 {} 部）".format(
                sum(len(v) for v in buckets.values()), n))
        if missing:
            out.append("")
            out.append("另有 {} 个关联条目不在动画库里（漫画/小说/游戏等）".format(missing))
        return "\n".join(out)

    # ═══════════════ studio ═══════════════

    def _rel_studio_work(self, wid: int, n: int) -> str:
        work = queries.get_work(self.db.conn, wid)
        title = (work["name_cn"] or work["name"]) if work else "#{}".format(wid)
        rows = [
            r for r in queries.get_work_staff(self.db.conn, wid)
            if r["person_type"] in (2, 3) and r["position"] in (67, 42)
        ]
        if not rows:
            return (
                "《{}》没有登记制作公司。用 get_entity(kind=\"work\", id={}) 看简介。".format(
                    title, wid)
            )

        out = ["《{}》的制作".format(title)]
        order = {67: 0, 42: 1}
        rows.sort(key=lambda r: (order.get(r["position"], 9), r["person_id"]))
        seen: set[int] = set()
        for r in rows:
            if int(r["person_id"]) in seen:
                continue
            seen.add(int(r["person_id"]))
            out.append("[公司#{}] {}（{}）".format(
                r["person_id"], r["person_name_cn"] or r["name"],
                STUDIO_LABEL.get(r["position"], r["position"])))

        primary = []
        seen_primary: set[int] = set()
        for r in rows:
            pid = int(r["person_id"])
            if r["position"] == 67 and pid not in seen_primary:
                seen_primary.add(pid)
                primary.append(r)
        if not primary:
            out.append("")
            out.append("这家没有单独的「动画制作」，可以用 kind=\"org\" 查公司参与作品。")
            return "\n".join(out)
        for r in primary[:2]:
            out.append("")
            out.append(self._org_works(int(r["person_id"]), n))
        return "\n".join(out)

    def _org_works(self, pid: int, n: int) -> str:
        """公司/组合参与的作品，列出职位。"""
        row = queries.get_person(self.db.conn, pid)
        title = (row["name_cn"] or row["name"]) if row else "#{}".format(pid)
        works: dict[int, dict] = {}
        for r in queries.get_person_works(self.db.conn, pid):
            e = works.setdefault(r["work_id"], {
                "work_id": r["work_id"],
                "title": r["name_cn"] or r["name"] or "",
                "date": r["date"] or "", "jobs": []})
            job = r["position_name"] or ""
            if job and job not in e["jobs"]:
                e["jobs"].append(job)
        if not works:
            return "[公司#{}] {}：没有查到参与的作品。".format(pid, title)
        rows = sorted(works.values(), key=lambda e: e["date"], reverse=True)
        out = ["[公司#{}] {} 的参与作品（共 {} 部，按日期倒序）".format(
            pid, title, len(rows))]
        for e in rows[:n]:
            out.append("[作品#{}] {}  {}".format(
                e["work_id"], str(e["title"])[:40], e["date"]))
            if e["jobs"]:
                out.append("  担任 " + "、".join(e["jobs"][:5]) + (
                    " 等" if len(e["jobs"]) > 5 else ""))
        if len(rows) > n:
            out.append("  …还有 {} 部".format(len(rows) - n))
        return "\n".join(out)

    # ═══════════════ cvs ═══════════════

    def _rel_cvs(self, cid: int, n: int) -> str:
        row = queries.get_character(self.db.conn, cid)
        cname = (row["name_cn"] or row["name"]) if row else "#{}".format(cid)
        cvs: list[dict] = []
        seen: set[int] = set()
        for r in queries.get_character_cvs(self.db.conn, cid, with_work=True):
            pid = int(r["person_id"])
            if pid in seen:
                continue
            seen.add(pid)
            cvs.append({
                "person_id": pid, "name": r["name"],
                "name_cn": r["person_name_cn"] or r["name"],
                "cv_type": r["cv_type"],
            })
        cvs.sort(key=lambda x: (x["cv_type"], x["person_id"]))
        if not cvs:
            return "[角色#{}] {}：没有收录声优。".format(cid, cname)

        out = ["[角色#{}] {} 的声优".format(cid, cname)]
        for cv in cvs:
            label = "日语" if cv["cv_type"] == 0 else "中文"
            pid = int(cv["person_id"])
            pname = cv["name_cn"] or cv["name"]
            out.append("")
            out.append("[人物#{}] {}（{}）".format(pid, pname, label))
            out.extend("  " + line for line in self._filmography(pid, n))
        return "\n".join(out)

    # ═══════════════ works ═══════════════

    def _rel_character_works(self, cid: int, n: int) -> str:
        row = queries.get_character(self.db.conn, cid)
        cname = (row["name_cn"] or row["name"]) if row else "#{}".format(cid)
        rows = [
            {
                "work_id": r["work_id"],
                "t": r["work_name_cn"] or r["work_name"],
                "date": r["date"],
            }
            for r in queries.get_character_works(self.db.conn, cid)
        ]
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        if not rows:
            return "[角色#{}] {}：没有查到登场作品。".format(cid, cname)
        out = ["[角色#{}] {} 的登场作品（共 {} 部，按日期倒序）".format(
            cid, cname, len(rows))]
        for r in rows[:n]:
            out.append("[作品#{}] {}  {}".format(
                r["work_id"], str(r["t"])[:40], r["date"] or ""))
        if len(rows) > n:
            out.append("  …还有 {} 部".format(len(rows) - n))
        return "\n".join(out)

    def _filmography(self, pid: int, n: int) -> list[str]:
        """人物参与作品：配音角色 + 制作职位，同一作品混排。"""
        c = self.db.conn
        row = queries.get_person(self.db.conn, pid)
        title = (row["name_cn"] or row["name"]) if row else "#{}".format(pid)
        works: dict[int, dict] = {}

        def slot(r) -> dict:
            e = works.get(r["work_id"])
            if e is None:
                e = {"work_id": r["work_id"], "title": r["t"] or "",
                     "date": r["date"] or "", "roles": [], "jobs": []}
                works[r["work_id"]] = e
            return e

        for r in queries.get_person_cv_works(c, pid):
            r["t"] = r["title"]
            e = slot(r)
            cid = int(r["char_id"])
            if all(x[0] != cid for x in e["roles"]):
                e["roles"].append((cid, r["cname"] or ""))

        for r in queries.get_person_works(self.db.conn, pid):
            r = dict(r)
            r["t"] = r["name_cn"] or r["name"]
            e = slot(r)
            job = r["position_name"] or ""
            if job and job not in e["jobs"]:
                e["jobs"].append(job)

        if not works:
            return ["[人物#{}] {}：没有收录配音或制作记录。".format(pid, title)]
        rows = sorted(works.values(), key=lambda e: e["date"], reverse=True)
        out = ["[人物#{}] {} 的参与作品（共 {} 部，按日期倒序）".format(
            pid, title, len(rows))]
        for e in rows[:n]:
            out.append("[作品#{}] {}  {}".format(
                e["work_id"], str(e["title"])[:40], e["date"]))
            if e["roles"]:
                parts = ["[角色#{}] {}".format(cid, name)
                         for cid, name in e["roles"][:5]]
                if len(e["roles"]) > 5:
                    parts.append("…")
                out.append("  参演 " + " / ".join(parts))
            if e["jobs"]:
                out.append("  担任 " + "、".join(e["jobs"][:5]) + (
                    " 等" if len(e["jobs"]) > 5 else ""))
        if len(rows) > n:
            out.append("  …还有 {} 部".format(len(rows) - n))
        cv_works = sum(1 for e in rows if e["roles"])
        job_works = sum(1 for e in rows if e["jobs"])
        if cv_works and job_works:
            out.append("")
            out.append("（其中配音 {} 部、制作 {} 部）".format(cv_works, job_works))
        elif job_works and not cv_works:
            out.append("")
            out.append("（这位是制作人员，没有配音记录）")
        return out
