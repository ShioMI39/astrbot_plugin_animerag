from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.filter.command import GreedyStr

from ..core import config as _cfg
from ..core.db.bgm import archive

KIND_LABEL = {
    "work": "番剧",
    "character": "角色",
    "person": "人物",
    "org": "公司/组合",
    "episode": "单集",
    "moegirl": "萌百",
}


class CommandMixin:
    async def cmd_check_update(self, event: AstrMessageEvent) -> None:
        """检查 Bangumi 存档有没有新版本"""
        await self._ensure()
        current = self._data_version() or "(未知)"
        try:
            meta = await archive.fetch_latest(proxy=_cfg.update_proxy())
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result("查询失败：{}: {}".format(type(exc).__name__, exc))
            return
        latest = archive.dump_name(meta)
        size = archive.human(int(meta.get("size") or 0))
        if latest == current:
            yield event.plain_result(
                "已是最新：{}\n向量块 {:,} · 数据目录 {}".format(
                    current,
                    getattr(getattr(self.retriever, "idx", None), "n", 0),
                    self.data_dir,
                )
            )
            return
        yield event.plain_result(
            "有新版：\n  当前 {}\n  最新 {}（{}）\n\n"
            "可在WebUI 里「更新数据」".format(
                current, latest, size
            )
        )

    async def cmd_recall(self, event: AstrMessageEvent, query: GreedyStr = "") -> None:
        """看看检索层对某个查询返回什么"""
        await self._ensure()
        if self.db is None:
            yield event.plain_result(self._missing_message())
            return
        q = str(query).strip()
        if not q:
            yield event.plain_result(
                "用法：/召回测试 <查询>\n例如：/召回测试 粉毛、/召回测试 孤独摇滚"
            )
            return

        r = await self._search(q)
        hits = r.get("hits") or []

        if r.get("mode") == "attr":
            attrs = " · ".join("{}={}".format(a["key"], a["label"]) for a in r["attr"])
            lines = ["属性命中：{}   共 {} 个角色".format(attrs, r["total"])]
            lines += ["  {}".format(h["title"]) for h in hits]
        else:
            sections = r.get("sections") or {}
            head = "分层检索：名字 {} / 属性 {} / 标签 {} / 语义 {} 条".format(
                len(sections.get("name") or []),
                len(sections.get("attr") or []),
                len(sections.get("tag") or []),
                len(sections.get("semantic") or []),
            )
            if r.get("reranked"):
                head += "（语义段已重排）"
            if r.get("error"):
                head += "（{}）".format(r["error"])
            lines = [head]
            for i, h in enumerate(hits, 1):
                # 只显示排序用的那个分
                main = h.get("rerank_score")
                if not isinstance(main, (int, float)):
                    main = h.get("score")
                tail = "  {:.3f}".format(main) if isinstance(main, (int, float)) else ""
                lines.append(
                    "  {}. [{}] {}{}".format(
                        i,
                        KIND_LABEL.get(h.get("kind"), h.get("kind")),
                        h.get("title"),
                        tail,
                    )
                )
            if not hits:
                lines.append("  没有命中。min_score={}".format(self.min_score))

        yield event.plain_result("\n".join(lines))
