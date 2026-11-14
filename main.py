from __future__ import annotations

import asyncio
from pathlib import Path
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from .commands import (
    CommandMixin,
    DetailMixin,
    FormatMixin,
    RelationMixin,
    ToolMixin,
)
from .core import config as _cfg
from .core.db import base as _db_base
from .core.db.bgm import queries as _bgm_q
from .core.db.bgm.schema import Database
from .core.db.moe import schema as _moe_schema
from .core.db.translate import queries as _tr_q
from .core.search import candidates as _cand
from .core.search import embedder as _emb
from .core.search import keywords as _kw
from .core.search import rerank as _rr
from .core.search import tags as _tag
from .core.search.retriever import Retriever
from .core.search.vector_index import VectorIndex
from .webui import WebUIMixin

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


class AnimeRAG(
    WebUIMixin, CommandMixin, DetailMixin, RelationMixin, ToolMixin, FormatMixin, Star
):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)

        self.conf = config
        self.cfg = _cfg.flatten(config or {})

        try:
            _cfg.set_astrbot_config(self.context.get_config())
        except Exception:
            pass

        self.data_dir = str(_cfg.data_dir())

        self.anime_db = str(Path(self.data_dir) / "anime.db")
        self.enable_moegirl = bool(self.cfg.get("enable_moegirl", True))
        self.moegirl_db = str(Path(self.data_dir) / "moegirl.db")
        self.moegirl_bodies = str(Path(self.data_dir) / "moegirl_bodies.json")
        self.translate_db = str(Path(self.data_dir) / "translate.db")
        self.vec_dir = str(Path(self.data_dir) / "vec")
        self.embedding_provider_id = str(self.cfg.get("embedding_provider_id") or "").strip()
        self.rerank_provider_id = str(self.cfg.get("rerank_provider_id") or "").strip()
        self.top_k = max(5, min(12, int(self.cfg.get("top_k") or 8)))
        self.top_n = max(18, min(25, int(self.cfg.get("top_n") or 20)))
        self.min_score = float(self.cfg.get("min_score") or 0.55)

        self.zh_char: dict[int, str] = {}
        self.zh_work: dict[int, str] = {}
        self.db: Database | None = None
        self.mg = None     # moegirl.db 连接
        self.retriever: Retriever | None = None
        self._lock = asyncio.Lock()
        self._error: str = ""
        # 「更新数据」任务的状态，WebUI 轮询 /update/status
        self._job: dict = {"state": "idle", "message": "", "percent": 0, "log": []}
        # 向量 / 翻译维护任务，分别轮询 /vector/status、/translation/status
        self._maint_jobs: dict[str, dict] = {
            "vector": {"state": "idle", "message": "", "percent": 0, "log": []},
            "translation": {"state": "idle", "message": "", "percent": 0, "log": []},
        }

        # 运行时的配置快照
        self._runtime_cfg = dict(self.cfg)
        # 已保存但还没重启生效的配置项（key 集合），重启后新实例自然清空
        self._pending_changes: set[str] = set()

        # 萌百正文缓存
        self._moe_cache: dict | None = None
        self._register_web_apis()



    # ── Commands ────────────────────────────────────────────────
    # 指令 handler 也必须定义在插件主模块，WebUI 才能通过 module_path
    # 找到所属插件并修改权限；实现仍留在 CommandMixin。
    @filter.command("检查更新")
    async def cmd_check_update(self, event: AstrMessageEvent):
        """检查 Bangumi 存档有没有新版本"""
        async for result in CommandMixin.cmd_check_update(self, event):
            yield result

    @filter.command("召回测试")
    async def cmd_recall(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """看看检索层对某个查询返回什么"""
        async for result in CommandMixin.cmd_recall(self, event, query):
            yield result

    # ── LLM tools ────────────────────────────────────────────────
    @filter.llm_tool(name="search_anime")
    async def search_anime(self, event: AstrMessageEvent, query: str) -> str:
        """查询 Anime 知识库：番剧、角色、角色属性、作品题材与剧情概要。

        Args:
            query(string): 查询内容。可以是番剧名、角色名、属性（如「粉发」「傲娇」）、
                题材（如「校园」「治愈」），也可以是一句自然语言描述
        """
        return await ToolMixin.search_anime(self, event, query)

    @filter.llm_tool(name="get_entity")
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

        Args:
            kind(string): work / character / person / org / episode
            id(number): 对应实体的 id
            part(string): work 专用。overview=简介；characters=角色表；episodes=单集列表
            page(number): character 专用。萌百正文第几页，默认 1
            start(number): work + part=episodes 时的起始集
            end(number): work + part=episodes 时的结束集
        """
        return await DetailMixin.get_entity(
            self, event, kind, id, part=part, page=page, start=start, end=end)

    @filter.llm_tool(name="get_translation")
    async def get_translation(self, event: AstrMessageEvent, kind: str, id: int) -> str:
        """按类型 + id 取中文译文。

        Args:
            kind(string): work / character / episode
            id(number): 对应实体的 id
        """
        return await DetailMixin.get_translation(self, event, kind, id)

    @filter.llm_tool(name="get_relations")
    async def get_relations(
        self,
        event: AstrMessageEvent,
        kind: str,
        id: int,
        relation: str = "auto",
        limit: int = 20,
    ) -> str:
        """按 kind + id 查实体关系：续作、制作公司、声优、参与作品。

        Args:
            kind(string): work / character / person / org
            id(number): search_anime 或 get_entity 返回的 # 后面的数字
            relation(string): auto=按 kind 选默认；work: sequel/studio；
                character: cvs/works；person: works；org: studio/works
            limit(number): 最多列多少部/多少人，默认 20，上限 60
        """
        return await RelationMixin.get_relations(
            self, event, kind, id, relation=relation, limit=limit)


    @filter.llm_tool(name="get_worklist")
    async def get_worklist(
        self,
        event: AstrMessageEvent,
        tag: list[str] | None = None,
        start_date: str = "",
        end_date: str = "",
        score_min: float | None = None,
        score_max: float | None = None,
    ) -> str:
        """按标签、播出时间和评分筛选作品，返回作品 id、名称、命中 tag 数、日期、评分。

        Args:
            tag(array[string]): 标签列表，例如 ["恋爱", "治愈", "青春"]；
                多个 tag 按命中数量排序，命中越多越靠前
            start_date(string): 起始月份，格式 YYYY-MM，例如 2020-01；留空不限制
            end_date(string): 结束月份，格式 YYYY-MM，例如 2024-12；留空不限制
            score_min(number): 最低评分，例如 7.5；留空不限制
            score_max(number): 最高评分，例如 9.0；留空不限制
        """
        return await ToolMixin.get_worklist(
            self, event,
            tag=tag,
            start_date=start_date,
            end_date=end_date,
            score_min=score_min,
            score_max=score_max,
        )


    async def initialize(self) -> None:
        """加载索引。"""
        logger.info(
            "[animerag] 配置 data_dir=%s top_k=%s min_score=%s "
            "萌百=%s embedding=%s rerank=%s 代理=%s",
            self.data_dir, self.top_k, self.min_score,
            "开" if self.enable_moegirl else "关",
            self.embedding_provider_id or "(未配，语义检索关闭)",
            self.rerank_provider_id or "(未配，不重排)",
            _cfg.moegirl_proxy() or "(直连)",
        )
        missing = self._missing_data()
        if missing:
            logger.warning(
                "[animerag] 数据未安装（缺少 %s），插件进入未就绪模式。"
                "请先在 WebUI「数据维护 → 资源包」下载，并解压到 %s。",
                "、".join(missing), self.data_dir,
            )
            return
        try:
            await self._ensure()
            if self.retriever is None:
                logger.warning(
                    "[animerag] 数据未就绪：%s", self._error or "未知原因")
                return
            logger.info(
                "[animerag] 就绪：%s 块", getattr(self.retriever.idx, "n", "?")
            )
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            logger.error("[animerag] 初始化失败，将退回纯 SQL 层：%s", self._error)


    def _missing_data(self) -> list[str]:
        required = [
            (Path(self.anime_db), "anime.db"),
            (Path(self.vec_dir) / "chunks.jsonl", "vec/chunks.jsonl"),
            (Path(self.vec_dir) / "embeddings.npy", "vec/embeddings.npy"),
        ]
        missing = [label for p, label in required if not p.is_file()]
        if missing:
            self._error = "数据未安装：缺少 " + "、".join(missing)
        return missing


    @staticmethod
    def _missing_message() -> str:
        return (
            "数据包还没有安装，当前无法查询。\n\n"
            "请打开 WebUI「数据维护 → 资源包」，下载并解压到：\n"
            "<AstrBot>/data/plugin_data/astrbot_plugin_animerag/\n"
            "确保 anime.db、vec/、translate.db 等直接位于该目录下，然后重启插件。"
        )


    async def terminate(self) -> None:
        if self.retriever:
            self.retriever.close()
        if self.db:
            self.db.close()
        if self.mg:
            self.mg.close()

        try:
            await _rr.close()
        except Exception:
            pass


    def _find_embedding_provider(self, key: str):
        """按 provider ID 或模型名找 EmbeddingProvider 实例。找不到返回 None。"""
        ep = self.context.get_provider_by_id(key)
        if ep is not None and hasattr(ep, "get_embedding"):
            return ep
        for p in self.context.get_all_embedding_providers():
            conf = getattr(p, "provider_config", None) or {}
            if key in (conf.get("id"), conf.get("embedding_model")):
                return p
        return None


    async def _bind_providers(self) -> None:
        """把 AstrBot 里配好的 Embedding / Rerank Provider 注入检索层。"""
        if not self.embedding_provider_id:
            logger.error(
                "[animerag] 没配 embedding_provider_id（必填）。"
                "语义检索已禁用，只剩词法与 SQL —— 请在插件配置里填一个 Embedding Provider ID"
            )
        else:
            expected = _emb.get_dim()
            ep = self._find_embedding_provider(self.embedding_provider_id)
            if ep is None:
                conn = _cfg.embedding_provider(self.embedding_provider_id)
                if conn is None:
                    logger.error(
                        "[animerag] 找不到 Embedding Provider %r，语义检索已禁用。"
                        "去「服务提供商」页面确认 ID（如 openai_embedding）或模型名",
                        self.embedding_provider_id,
                    )
                else:
                    dim = int(conn.get("dim") or 0)
                    if dim and dim != expected:
                        logger.error(
                            "[animerag] Embedding Provider 维度 %s 与向量库 %s 不一致，"
                            "语义检索已禁用。换模型必须重新构建向量",
                            dim, expected,
                        )
                    else:
                        _emb.use_conn(conn)
                        logger.info(
                            "[animerag] Embedding 走直调 %s（模型 %s，维度 %s）",
                            conn["api_base"], conn["model"], dim or "待首次请求确认",
                        )
            else:
                dim = getattr(ep, "get_dim", lambda: None)()
                if dim and dim != expected:
                    logger.error(
                        "[animerag] Embedding Provider 维度 %s 与向量库 %s 不一致，"
                        "语义检索已禁用。换模型必须重新构建向量",
                        dim, expected,
                    )
                else:
                    _emb.set_provider(ep)
                    logger.info(
                        "[animerag] Embedding 用 AstrBot provider %s（维度 %s）",
                        self.embedding_provider_id, dim or expected,
                    )

        if self.rerank_provider_id:
            rp = self.context.get_provider_by_id(self.rerank_provider_id)
            if rp is None or not hasattr(rp, "rerank"):
                rp = None
                for p in getattr(self.context.provider_manager, "rerank_provider_insts", []):
                    conf = getattr(p, "provider_config", None) or {}
                    if self.rerank_provider_id in (
                        conf.get("id"), conf.get("rerank_model")
                    ):
                        rp = p
                        break
            if rp is not None:
                _rr.set_provider(rp)
                logger.info("[animerag] Rerank 用 AstrBot provider（%s）", self.rerank_provider_id)
            else:
                conn = _cfg.rerank_provider(self.rerank_provider_id)
                if conn is None:
                    logger.warning(
                        "[animerag] 找不到 Rerank Provider %r，重排序已关闭",
                        self.rerank_provider_id,
                    )
                else:
                    _rr.use_conn(conn)
                    logger.info(
                        "[animerag] Rerank 走直调 %s（模型 %s）",
                        conn["api_base"], conn["model"],
                    )
        else:
            logger.info("[animerag] 没配 rerank_provider_id，不启用重排序")


    async def _ensure(self) -> None:
        """懒加载"""
        if self.retriever is not None:
            return
        if self._missing_data():
            return
        async with self._lock:
            if self.retriever is not None:
                return
            if self._missing_data():
                return
            self.db = Database(self.anime_db)

            # 两条路径降级互不影响 —— 属性查询消失，其余照常。
            if self.enable_moegirl and Path(self.moegirl_db).is_file():
                self.mg = _moe_schema.open_db(self.moegirl_db)
            else:
                self.mg = None
                if self.enable_moegirl:
                    logger.warning(
                        "[animerag] 萌百层已启用但找不到 %s —— 属性查询不可用。"
                        "跑 scripts/data/install.py 下载数据包，或关掉 enable_moegirl",
                        self.moegirl_db,
                    )

            tr_path = Path(self.translate_db)
            if tr_path.exists():
                tdb = _db_base.connect_readonly(tr_path)
                for row in _tr_q.all_translations(tdb):
                    kind, ref, dst = row["kind"], row["ref_id"], row["dst_text"]
                    if kind == "character":
                        self.zh_char[ref] = dst
                    elif kind == "work":
                        self.zh_work[ref] = dst
                tdb.close()
            # 向量库维度是运行时契约；先探测，再绑定 provider
            try:
                _emb.set_dim(VectorIndex.dim_of(self.vec_dir))
            except Exception as exc:  # noqa: BLE001
                self._error = "索引维度读取失败：{}: {}".format(
                    type(exc).__name__, exc)
                logger.warning("[animerag] %s", self._error)
                return
            await self._bind_providers()

            # 绑定可能拿到实例，也可能只拿到直调参数 —— 两种都算启用。
            rr_on = _rr.get_provider() is not None or _rr.resolve_conn() is not None
            try:
                self.retriever = Retriever(
                    self.anime_db, self.vec_dir,
                    rerank_enabled=rr_on,
                    rerank_top_n=int(self.cfg.get("rerank_top_n") or 20),
                    rerank_min_score=float(self.cfg.get("rerank_min_score") or 0.0),
                )
            except Exception as exc:  # noqa: BLE001
                self._error = "索引加载失败：{}: {}".format(
                    type(exc).__name__, exc)
                logger.warning("[animerag] %s", self._error)
                self.retriever = None
                return
            self.retriever.close()          # Retriever 只要 idx，自己的 Database 关掉
            self.retriever.db = self.db     # 复用同一个连接


    def _data_version(self) -> str:
        """anime.db 里记的 dump 版本号。"""
        try:
            return _bgm_q.get_data_version(self.db.conn)
        except Exception:
            return ""



    async def _search(self, q: str, k: int | None = None,
                      kinds: set[str] | None = None) -> dict:
        """分层检索统一入口。

        第 1 层  名字：search_index 精确候选（带热度）
        第 2a 层 属性：角色萌点/发色/瞳色/所属团体
        第 2b 层 标签：作品题材/地区/来源
        第 3 层  语义：chunk 向量 + rerank

        返回 sections（name / attr / tag / semantic）和兼容旧界面的 hits。
        """
        q = (q or "").strip()
        if not q:
            return {
                "mode": "semantic", "hits": [], "total": 0, "error": "空查询",
                "sections": {"name": [], "attr": [], "tag": [], "semantic": []},
                "attr": [], "reranked": False, "debug": {},
            }

        await self._ensure()
        if self.db is None:
            return {
                "mode": "semantic", "hits": [], "total": 0,
                "error": self._missing_message(),
                "sections": {"name": [], "attr": [], "tag": [], "semantic": []},
                "attr": [], "reranked": False, "debug": {},
            }
        # k 仅用于 /召回测试 这类显式覆盖；search_anime / WebUI 默认用 top_n
        cap = self.top_n if k is None else max(1, min(int(k), 50))
        semantic_k = min(self.top_k, cap)
        debug: dict = {"budget": {"top_n": cap, "semantic_k": semantic_k}}

        sections: dict[str, list[dict]] = {
            "name": [], "attr": [], "tag": [], "semantic": [],
        }
        attr_labels: list[dict] = []

        # ── 第 1 层：精确名字候选 ──────────────────────────────
        name_rows, name_confident = _cand.find(self.db, q, kinds=kinds, limit=5)
        debug["name"] = {"total": len(name_rows), "confident": name_confident}

        # ── 第 2a 层：角色属性 ─────────────────────────────────
        kws: list[dict] = []
        raw_attr: list[dict] = []
        attr_total = 0
        kw_count = 0
        attr_ok = False
        if self.mg is not None and (kinds is None or "character" in kinds):
            kws, raw_attr = _kw.search(self.mg, q, ignore=_kw.GENERIC_TRAITS)
            kw_count = len({x["kid"] for x in kws})
            attr_total = len(raw_attr)
            attr_ok = (
                (kw_count >= 2 and attr_total >= 3)
                or (kw_count == 1 and attr_total >= 20)
            )
            seen_labels: set[tuple[str, str]] = set()
            for kw in kws:
                pair = (kw["key"], kw["target"])
                if pair in seen_labels:
                    continue
                seen_labels.add(pair)
                attr_labels.append({
                    "key": kw["key"], "target": kw["target"], "label": kw["label"],
                })

        debug["attr"] = {
            "keywords": kw_count, "total": attr_total, "satisfied": attr_ok,
        }

        attr_items: list[dict] = []
        if raw_attr:
            meta = _bgm_q.get_character_meta_many(
                self.db.conn, (int(r["char_id"]) for r in raw_attr))
            for row in raw_attr:
                cid = int(row["char_id"])
                title, collects = meta.get(cid, ("#{}".format(cid), 0))
                attr_items.append({
                    "kind": "character",
                    "ref_id": cid,
                    "title": title,
                    "matched": row["matched"],
                    "labels": row["labels"],
                    "collects": collects,
                })
            attr_items.sort(
                key=lambda x: (-x["matched"], -x["collects"], x["ref_id"])
            )

        # ── 第 2b 层：作品标签 ─────────────────────────────────
        tag_names: list[str] = []
        tag_rows: list[dict] = []
        tag_ok = False
        qv = None
        if kinds is None or "work" in kinds:
            tag_names = _tag.scan(q)
            if not tag_names and (not name_confident) and (not attr_ok):
                try:
                    qv = await _emb.embed_one(q)
                    tag_names = _tag.semantic_tags(
                        qv,
                        top_k=max(1, int(self.cfg.get("tag_top_k") or 3)),
                        tau=float(self.cfg.get("tag_tau") or 0.62),
                        delta=float(self.cfg.get("tag_delta") or 0.02),
                        high=float(self.cfg.get("tag_high") or 0.68),
                        floor_min=float(self.cfg.get("tag_floor") or 0.55),
                    )
                except Exception as exc:  # noqa: BLE001
                    debug["tag_semantic_error"] = "{}: {}".format(
                        type(exc).__name__, exc)
                    qv = None
            if tag_names:
                tag_rows = _tag.search_works(
                    self.db.conn, tag_names, limit=5,
                    min_match=int(self.cfg.get("tag_match_min") or 2),
                )
                tag_ok = len(tag_rows) >= int(self.cfg.get("tag_hit_min") or 3)

        debug["tag"] = {
            "tags": tag_names, "total": len(tag_rows), "hit": tag_ok,
            "semantic": qv is not None,
        }

        # ── 第 3 层：只有结构化层都不够用时才跑 ────────────────
        need3 = (not name_confident) and (not attr_ok) and (not tag_ok)
        debug["need3"] = need3
        sem_rows: list[dict] = []
        # 长查询先扩大稠密候选池，再重排。
        deep = (
            len(q) >= 40 and self.retriever is not None
            and getattr(self.retriever, "rerank_enabled", False)
        )
        debug["semantic_deep"] = deep
        if need3 and self.retriever is not None:
            try:
                hold = await self.retriever.retrieve(
                    q, kinds=kinds,
                    top_k=semantic_k,
                    min_score=self.min_score,
                    force_dense=True, qv=qv,
                    candidate_k=400 if deep else None,
                    rerank_k=400 if deep else None,
                )
            except Exception as exc:  # noqa: BLE001
                debug["semantic_error"] = "{}: {}".format(
                    type(exc).__name__, exc)
                hold = []
            for h in hold:
                sem_rows.append({
                    "kind": h.get("kind"),
                    "ref_id": h.get("ref_id"),
                    "title": str(h.get("title") or ""),
                    "text": str(h.get("text") or "")[:400],
                    "score": h.get("score"),
                    "rerank_score": h.get("rerank_score"),
                })
        debug["semantic"] = {"total": len(sem_rows)}

        # ── 总装：去重 + 配额 ──────────────────────────────────
        seen: set[tuple[str, int]] = set()

        def add(section: str, item: dict) -> bool:
            key = (item.get("kind"), int(item.get("ref_id") or 0))
            if key in seen:
                return False
            seen.add(key)
            sections[section].append(item)
            return True

        name_cap = min(5, cap)
        for item in name_rows[:name_cap]:
            add("name", item)

        if need3:
            # 语义段先占 top_k 个名额（按 chunk 实体去重），2a 再把剩余名额补满
            sem_cap = min(semantic_k, max(0, cap - len(sections["name"])))
            sem_seen: set[tuple[str, int]] = set()
            for row in sem_rows[:sem_cap]:
                key = (row.get("kind"), int(row.get("ref_id") or 0))
                if key in sem_seen:
                    continue
                sem_seen.add(key)
                sections["semantic"].append(row)
            attr_cap = max(
                0, cap - len(sections["name"]) - len(sections["semantic"]))
            for item in attr_items[:attr_cap]:
                add("attr", item)
        else:
            tag_cap = min(5, max(0, cap - len(sections["name"])))
            for row in tag_rows[:tag_cap]:
                add("tag", {
                    "kind": "work",
                    "ref_id": int(row["work_id"]),
                    "title": row["title"],
                    "matched": row["matched"],
                    "tags": row["tags"],
                    "favorite_done": row["favorite_done"],
                })
            room = max(0, cap - len(sections["name"]) - len(sections["tag"]))
            for item in attr_items[:room]:
                add("attr", item)

        # 预算裁剪（按 section 顺序）
        order = ("name", "tag", "attr", "semantic") if not need3 else (
            "name", "attr", "semantic",
        )
        left = cap
        for section in order:
            sections[section] = sections[section][:left]
            left -= len(sections[section])

        hits: list[dict] = []
        for item in sections["name"]:
            hits.append({
                **item, "score": None, "rerank_score": None, "source": "name",
                "note": "w={} · 热度{}".format(item["weight"], item["popularity"]),
            })
        for item in sections["tag"]:
            hits.append({
                **item, "score": None, "rerank_score": None, "source": "tag",
                "note": "命中{}/{}: {}".format(
                    item["matched"], len(tag_names), "、".join(item["tags"])),
            })
        for item in sections["attr"]:
            hits.append({
                **item, "score": None, "rerank_score": None, "source": "attr",
                "note": "命中{}: {}".format(item["matched"], "、".join(item["labels"])),
            })
        for item in sections["semantic"]:
            hits.append({
                **item, "source": "semantic", "note": "",
            })

        mode = "attr" if (
            sections["attr"] and not sections["name"]
            and not sections["tag"] and not sections["semantic"]
        ) else "semantic"
        return {
            "mode": mode,
            "attr": attr_labels,
            "total": len(hits),
            "reranked": any(h.get("rerank_score") is not None for h in hits),
            "hits": hits,
            "sections": sections,
            "debug": debug,
            "error": self._error if (not hits and self.retriever is None) else "",
        }



