from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

KIND_LABEL = {
    "work": "作品",
    "character": "角色",
    "person": "人物",
    "org": "公司/组合",
    "episode": "单集",
    "moegirl": "萌百",
}


def _kind(kind: str) -> str:
    if kind == "moegirl":
        return "角色"
    return KIND_LABEL.get(kind, kind or "?")


class ToolMixin:
    async def search_anime(self, event: AstrMessageEvent, query: str) -> str:
        """查询 Anime 知识库：番剧、角色、角色属性、作品题材与剧情概要。

        查不到时返回「没有足够匹配的资料」，此时不要编造答案。
        返回里的 [角色#123] / [作品#123] / [单集#123] 用于 get_entity(kind, id) 取完整资料；
        问「第几集」可以直接用语义段里的 [单集#id]，或用
        get_entity(kind="work", id=work_id, part="episodes", start=N, end=N)。

        Args:
            query(string): 查询内容。可以是番剧名、角色名、属性（如「粉发」「傲娇」）、
                题材（如「校园」「治愈」），也可以是一句自然语言描述
        """
        await self._ensure()
        if self.db is None:
            return self._missing_message()
        q = (query or "").strip()
        if not q:
            return "查询为空。"

        result = await self._search(q)
        sections = result.get("sections") or {}
        names = sections.get("name") or []
        attrs = sections.get("attr") or []
        tag_hits = sections.get("tag") or []
        semantic = sections.get("semantic") or []

        if not (names or attrs or tag_hits or semantic):
            return (
                "没有足够匹配的资料。本知识库覆盖番剧名、角色名、角色属性、"
                "作品题材与作品/角色概要，不擅长「我记得有个情节」这类模糊回忆——"
                "请让用户补充角色名、番剧名、年份或制作公司。不要编造。"
            )

        out: list[str] = []

        if names:
            out.append("[名字命中]")
            for item in names:
                out.append("[{}#{}] {} · w={} · 热度{}".format(
                    _kind(item["kind"]), item["ref_id"], item["title"],
                    item["weight"], item["popularity"],
                ))

        if attrs:
            labels = result.get("attr") or []
            head = "、".join(a.get("label", "") for a in labels)
            out.append("")
            out.append("[属性命中] {}".format(head))
            for item in attrs:
                out.append("[角色#{}] {} · 命中{}: {}".format(
                    item["ref_id"], item["title"], item["matched"],
                    "、".join(item["labels"]),
                ))

        if tag_hits:
            tags = (result.get("debug") or {}).get("tag", {}).get("tags") or []
            out.append("")
            out.append("[标签命中] {}".format("、".join(tags)))
            for item in tag_hits:
                out.append("[作品#{}] {} · {}/{}: {} · 看过{}".format(
                    item["ref_id"], item["title"], item["matched"], len(tags),
                    "、".join(item["tags"]), item["favorite_done"],
                ))

        if semantic:
            out.append("")
            out.append("[语义片段]")
            for item in semantic:
                score = item.get("rerank_score")
                if not isinstance(score, (int, float)):
                    score = item.get("score")
                tail = "（{:.3f}）".format(float(score)) if isinstance(score, (int, float)) else ""
                out.append("[{}#{}] {} {}".format(
                    _kind(item["kind"]), item["ref_id"], item["title"], tail,
                ).rstrip())
                text = (item.get("text") or "").strip()
                if text:
                    out.append("  " + text[:240].replace("\n", " "))

        out.append("")
        out.append("取完整资料用 get_entity(kind, id)；单集用 "
                   'get_entity(kind="episode", id=...) 或 '
                   'get_entity(kind="work", id=..., part="episodes", start=N, end=N)。')
        return "\n".join(out)
