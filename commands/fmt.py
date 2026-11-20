from __future__ import annotations

from ..core.db.bgm import queries
from ..core.db.moe import queries as moe_queries

PLATFORM_NAMES = {1: "TV", 2: "OVA", 3: "剧场版", 5: "Web"}

# 属性展示顺序
ATTR_ORDER = ["萌点", "发色", "瞳色", "身高", "生日", "星座", "血型", "体重", "所属团体"]


class FormatMixin:
    def _fmt_work(self, row) -> str:
        lines = ["《{}》".format(row["name_cn"] or row["name"])]
        if row["name_cn"] and row["name"]:
            lines.append("原名：{}".format(row["name"]))
        bits = []
        if row["platform"] in PLATFORM_NAMES:
            bits.append(PLATFORM_NAMES[row["platform"]])
        if row["date"]:
            bits.append(str(row["date"]))
        if row["episodes"]:
            bits.append("{} 话".format(row["episodes"]))
        if row["score"]:
            bits.append("评分 {:.1f}".format(row["score"]))
        if bits:
            lines.append(" / ".join(bits))
        if row["tags"]:
            lines.append("标签：{}".format(row["tags"]))
        text = self.zh_work.get(row["work_id"]) or row["summary"]
        if text:
            lines.append("")
            lines.append(str(text).strip())
        return "\n".join(lines)

    def _work_row(self, wid: int):
        d = queries.get_work(self.db.conn, wid)
        if not d:
            return None
        st = queries.get_work_stats(self.db.conn, wid) or {}
        d["score"] = st.get("score")
        d["rank"] = st.get("rank")
        d["tags"] = "、".join(
            t["tag"] for t in queries.get_work_tags(self.db.conn, wid, limit=8)
        )
        return d

    def _char_row(self, cid: int):
        d = queries.get_character(self.db.conn, cid)
        if not d:
            return None
        # 每部作品一条：主角在前，同 role 内按播出日期正序。
        works = queries.get_character_works(self.db.conn, cid)
        works.sort(key=lambda r: (r["role"], r["date"] is None, r["date"] or ""))
        d["works"] = [
            (r["work_id"], r["work_name_cn"] or r["work_name"]) for r in works
        ]
        seen_cv: set[str] = set()
        cvs = []
        for r in queries.get_character_cvs(self.db.conn, cid, with_work=True):
            label = r["person_name_cn"] or r["name"]
            if not label or label in seen_cv:
                continue
            seen_cv.add(label)
            cvs.append((r["name"], r["person_name_cn"], r["cv_type"]))
        cvs.sort(key=lambda x: (x[2], x[1] or x[0] or ""))
        d["cvs"] = cvs[:8]
        d["attrs"] = self._attrs(cid)
        return d

    def _attrs(self, cid: int) -> dict[str, list[str]]:
        """按 attr_key 聚合，保留多值（萌点/所属团体经常有多行）。"""
        if not self.mg:
            return {}
        try:
            return moe_queries.character_attrs_grouped(self.mg, cid)
        except Exception:
            return {}

    def _fmt_char(self, d: dict, body: str = "", works_limit: int = 3) -> str:
        lines = ["【{}】".format(d.get("name_cn") or d.get("name"))]
        if d.get("name") and d.get("name_cn"):
            lines.append("原名：{}".format(d["name"]))
        works = d.get("works") or []
        if works:
            shown = works[:works_limit]
            line = "出自：" + "、".join("《{}》".format(w[1]) for w in shown)
            if len(works) > len(shown):
                line += "等 {} 部".format(len(works))
            lines.append(line)
        if d.get("cvs"):
            jp = [c[1] or c[0] for c in d["cvs"] if c[2] == 0][:2]
            cn = [c[1] or c[0] for c in d["cvs"] if c[2] == 3][:1]
            bits = []
            if jp:
                bits.append(" / ".join(jp))
            if cn:
                bits.append("中配 " + " / ".join(cn))
            if bits:
                lines.append("声优：" + "；".join(bits))
        if d.get("attrs"):
            parts = []
            for k in ATTR_ORDER:
                vals = d["attrs"].get(k)
                if vals:
                    parts.append("{}：{}".format(k, "、".join(vals)))
            parts += [
                "{}：{}".format(k, "、".join(vals))
                for k, vals in d["attrs"].items()
                if k not in ATTR_ORDER and vals
            ]
            if parts:
                lines.append("　".join(parts))
        text = body or self.zh_char.get(d.get("char_id")) or (d.get("summary") or "")
        if text:
            lines.append("")
            lines.append(str(text).strip()[:600])
        return "\n".join(lines)
