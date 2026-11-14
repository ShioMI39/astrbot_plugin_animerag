from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.star.star import star_map

from ..core import config as _cfg
from ..core.db import base as db_base
from ..core.db.bgm import queries as bgm_queries
from ..core.db.moe import queries as moe_queries
from ..core.db.translate import queries as tr_queries
from ..core.db.bgm import archive
from ..core.db.bgm.schema import Database
from ..core.db.bgm.importer import import_dump
from ..core.db.translate import schema as tr
from ..core.search import embedder as _emb
from ..core.search import rerank as _rr


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

_DUMP_VERSION_RE = re.compile(r"^dump-(\d{4}-\d{2}-\d{2})\.(\d{6})Z$")


def _dump_version_ts(version: str) -> float:
    """dump-2026-11-10.210405Z -> UTC timestamp。"""
    m = _DUMP_VERSION_RE.match((version or "").strip())
    if not m:
        return 0.0
    try:
        return datetime.strptime(
            m.group(1) + " " + m.group(2), "%Y-%m-%d %H%M%S"
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


class WebUIMixin:
    def _register_web_apis(self) -> None:
        """注册 WebUI 视图用的接口。"""
        name = _PLUGIN_ROOT.name
        routes = (
            ("/status", self.api_status, ["GET"], "AnimeRAG 状态"),
            ("/search", self.api_search, ["GET"], "试检索"),
            ("/check-update", self.api_check_update, ["GET"], "检查 Bangumi 新版本"),
            ("/update", self.api_update, ["POST"], "启动数据更新"),
            ("/update/status", self.api_update_status, ["GET"], "更新进度"),
            ("/vector/check", self.api_vector_check, ["GET"], "检查向量是否需要更新"),
            ("/vector/update", self.api_vector_update, ["POST"], "增量重建向量"),
            ("/vector/status", self.api_vector_status, ["GET"], "向量任务进度"),
            ("/translation/check", self.api_translation_check, ["GET"], "检查待译条目"),
            ("/translation/update", self.api_translation_update, ["POST"], "增量同步翻译"),
            ("/translation/status", self.api_translation_status, ["GET"], "翻译任务进度"),
            ("/config", self.api_config, ["GET"], "读取可编辑配置"),
            ("/config/save", self.api_config_save, ["POST"], "保存配置"),
            ("/restart", self.api_restart, ["POST"], "重载插件"),
        )
        ok = 0
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(name + route, handler, methods, desc)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[animerag] 注册 %s 失败：%s", route, exc)
        logger.info("[animerag] WebUI 接口 %d/%d 已注册（前缀 /%s）", ok, len(routes), name)

    async def api_status(self):
        """数据版本、各级条数、模型绑定状态。"""

        await self._ensure()
        counts = {}
        try:
            for k, table in (
                ("works", "works"),
                ("characters", "characters"),
                ("episodes", "work_episodes"),
            ):
                counts[k] = bgm_queries.count_rows(self.db.conn, table)
        except Exception:
            pass
        if self.mg is not None:
            try:
                counts["moegirl_pages"] = moe_queries.count_ok_pages(self.mg)
            except Exception:
                pass
        try:
            tr = Path(self.translate_db)
            if tr.exists():
                c = db_base.connect_readonly(tr)
                counts["translations"] = tr_queries.count(c)
                c.close()
        except Exception:
            pass

        ep = _emb.get_provider()
        rp = _rr.get_provider()
        return json_response({
            "ready": self.retriever is not None,
            "error": self._error,
            "blocks": getattr(getattr(self.retriever, "idx", None), "n", 0),
            "data_version": self._data_version(),
            "data_dir": self.data_dir,
            "embedding": (
                "{}（已绑定实例）".format(self.embedding_provider_id) if ep
                else ("{}（直调）".format(self.embedding_provider_id)
                      if _emb.resolve_conn() else "")
            ),
            "rerank": "{}{}".format(self.rerank_provider_id, " · 已启用" if rp else "")
                      if self.rerank_provider_id else "",
            "translate": str(self.cfg.get("translate_provider_id") or ""),
            "moegirl": self.mg is not None,
            "embed_dim": getattr(ep, "get_dim", lambda: None)() if ep else None,
            **counts,
        })

    async def api_search(self):
        q = (request.query.get("q", "", type=str) or "").strip()
        if not q:
            return error_response("缺少 q 参数")
        kind = request.query.get("kind", "", type=str) or ""
        kinds = {kind} if kind else None

        r = await self._search(q, kinds=kinds)
        r["hits"] = [{
            **h,
            "ref_id": _num(h["ref_id"]),
            "score": _num(h["score"]),
            "rerank_score": _num(h.get("rerank_score")),
        } for h in r["hits"]]
        return json_response(r)

    async def api_check_update(self):
        """比对 Bangumi Archive 的最新 dump 和本地版本。"""
        current = self._data_version()
        try:
            meta = await archive.fetch_latest(proxy=_cfg.update_proxy())
        except Exception as exc:
            return json_response({"error": "{}: {}".format(type(exc).__name__, exc)})
        latest = archive.dump_name(meta)
        return json_response({
            "current": current,
            "latest": latest,
            "up_to_date": current == latest,
            "size_mb": round(int(meta.get("size") or 0) / 1024 / 1024, 1),
        })

    # ── 数据更新 ────────────────────────────────────────────────

    def _job_set(self, **kw) -> None:
        self._job.update(kw)
        if len(self._job["log"]) > 200:
            del self._job["log"][:-200]

    def _job_log(self, msg: str) -> None:
        self._job["log"].append(msg)

    async def api_update_status(self):
        """更新任务的进度。前端每秒轮询一次。"""
        return json_response(dict(self._job))

    async def api_update(self):
        """启动后台更新。已经在跑就直接返回当前状态。"""
        if self._job["state"] == "running":
            return json_response(dict(self._job))
        self._job = {"state": "running", "message": "准备中…", "percent": 0, "log": []}
        asyncio.create_task(self._run_update())
        return json_response(dict(self._job))

    async def _run_update(self) -> None:
        """下载最新 dump -> 解压 -> 导入。"""
        cache = Path(self.data_dir) / "temp" / "animerag-dump"
        db_path = Path(self.anime_db)
        keep = bool(self.cfg.get("keep_dump"))
        proxy = _cfg.update_proxy()
        if proxy:
            self._job_log("下载代理 {}".format(proxy))
        try:
            self._job_set(message="读取最新版本…", percent=2)
            meta = await archive.fetch_latest(proxy=proxy)
            name = archive.dump_name(meta)
            current = self._data_version()
            if current == name:
                self._job_set(state="done", message="已是最新：{}".format(name), percent=100)
                return
            self._job_log("当前 {} → 最新 {}".format(current or "(无)", name))

            zip_path = cache / (name + ".zip")
            self._job_set(message="下载 {}（{}）".format(name, archive.human(int(meta.get("size") or 0))), percent=5)
            loop = asyncio.get_running_loop()
            last = [0.0]

            def on_progress(done: int, total: int) -> None:
                # 5% ~ 70% 给下载
                if total and loop.time() - last[0] > 1:
                    last[0] = loop.time()
                    self._job_set(percent=5 + int(done * 65 / total))

            await archive.download(
                str(meta.get("browser_download_url") or ""),
                zip_path, int(meta.get("size") or 0),
                str(meta.get("digest") or ""), on_progress, proxy=proxy,
            )
            self._job_log("下载完成 {}".format(archive.human(zip_path.stat().st_size)))

            self._job_set(message="解压…", percent=72)
            dump_dir = await asyncio.to_thread(archive.extract, zip_path, cache / name)

            self._job_set(message="导入数据库（约 1 分钟）…", percent=78)
            stats = await asyncio.to_thread(
                import_dump, dump_dir=dump_dir, db_path=db_path, limit=None,
            )
            self._job_log("导入完成 {}".format(stats))

            if not keep:
                self._job_set(message="清理…", percent=95)
                await asyncio.to_thread(shutil.rmtree, cache / name, ignore_errors=True)
                zip_path.unlink(missing_ok=True)

            self._job_set(
                state="done", percent=100,
                message="完成，数据版本 {}。重启插件后索引生效".format(self._data_version()),
            )
        except Exception as exc:  # noqa: BLE001
            self._job_set(
                state="error", percent=100,
                message="{}: {}".format(type(exc).__name__, exc),
            )

    # ── 向量 / 翻译维护 ─────────────────────────────────────────

    def _maint_job(self, name: str) -> dict:
        if not hasattr(self, "_maint_jobs"):
            self._maint_jobs = {}
        return self._maint_jobs.setdefault(
            name, {"state": "idle", "message": "", "percent": 0, "log": []})

    @staticmethod
    def _maint_log(job: dict, line: str) -> None:
        job["log"].append(line)
        if len(job["log"]) > 200:
            del job["log"][:-200]
        job["message"] = line
        m = re.search(r"(\d{1,3})%", line)
        if m:
            job["percent"] = min(100, max(0, int(m.group(1))))

    @staticmethod
    def _read_job_log(path: Path, pos: int, job: dict) -> int:
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
        except OSError:
            return pos
        pos += len(data)
        for line in data.decode("utf-8", "replace").splitlines():
            if line.strip():
                job["log"].append(line)
                if len(job["log"]) > 200:
                    del job["log"][:-200]
                job["message"] = line
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    job["percent"] = min(100, max(0, int(m.group(1))))
        return pos

    async def _run_script_job(self, name: str, script: Path, args: list[str]) -> None:
        job = self._maint_job(name)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        log_path = Path(self.data_dir) / "temp" / ("maint-{}.log".format(name))
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def spawn():
            fh = open(log_path, "wb", buffering=0)
            try:
                return subprocess.Popen(
                    [sys.executable, "-u", str(script), *args],
                    cwd=str(_PLUGIN_ROOT), env=env,
                    stdout=fh, stderr=subprocess.STDOUT,
                )
            finally:
                fh.close()

        try:
            proc = await asyncio.to_thread(spawn)
            pos = 0
            while proc.poll() is None:
                await asyncio.sleep(0.8)
                pos = self._read_job_log(log_path, pos, job)
            code = proc.wait()
            self._read_job_log(log_path, pos, job)
            if code == 0:
                job["state"] = "done"
                job["percent"] = 100
                job["message"] = "完成。重启插件后生效。"
            else:
                job["state"] = "error"
                job["percent"] = 100
                job["message"] = "失败（exit {}），请看日志。".format(code)
        except Exception as exc:
            job["state"] = "error"
            job["percent"] = 100
            job["message"] = "{}: {}".format(type(exc).__name__, exc)

    async def api_vector_check(self):
        """快速检查：文件是否齐全、行数是否一致、源文件是否比向量新。"""
        vec = Path(self.vec_dir)
        chunks = vec / "chunks.jsonl"
        emb = vec / "embeddings.npy"
        manifest = vec / "emb_hashes.json"
        meta_path = vec / "meta.json"
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        tags_path = vec / "tags.json"
        tag_emb = vec / "tag_embeddings.npy"
        tag_meta_path = vec / "tag_meta.json"
        reasons: list[str] = []
        for path in (chunks, emb, manifest, meta_path):
            if not path.is_file():
                reasons.append("缺少 {}".format(path.name))
        rows = 0
        dim = 0
        if emb.is_file():
            try:
                arr = np.load(emb, mmap_mode="r")
                rows, dim = int(arr.shape[0]), int(arr.shape[1])
            except Exception as exc:  # noqa: BLE001
                reasons.append("{} 读取失败：{}".format(emb.name, exc))
        hashes = []
        if manifest.is_file():
            try:
                hashes = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                reasons.append("emb_hashes.json 读取失败")
        total = int(meta.get("total") or 0)
        if total and rows != total:
            reasons.append("embeddings={} 与 meta.total={} 不一致".format(rows, total))
        if len(hashes) != rows:
            reasons.append("manifest={} 与 embeddings={} 不一致".format(len(hashes), rows))
        meta_dim = int(meta.get("dim") or 0)
        if meta_dim and dim and meta_dim != dim:
            reasons.append("embeddings 维度 {} 与 meta.dim={} 不一致".format(dim, meta_dim))

        # 当前配置的模型是否与向量库记录一致
        provider = _cfg.embedding_provider(
            str(getattr(self, "embedding_provider_id", "") or ""))
        if provider:
            cur_model = str(provider.get("model") or "")
            cur_dim = int(provider.get("dim") or 0)
            if meta.get("embed_model") and cur_model and \
                    meta["embed_model"] != cur_model:
                reasons.append("向量模型 {} 与当前配置模型 {} 不一致".format(
                    meta["embed_model"], cur_model))
            if cur_dim and dim and cur_dim != dim:
                reasons.append("当前模型维度 {} 与向量库 {} 不一致".format(
                    cur_dim, dim))

        # tag 语义索引：行数、维度、模型都要和主向量匹配
        tag_rows = 0
        tag_dim = 0
        tags = []
        if not tag_emb.is_file():
            reasons.append("缺少 tag_embeddings.npy")
        if not tags_path.is_file():
            reasons.append("缺少 tags.json")
        if tags_path.is_file():
            try:
                tags = json.loads(tags_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                reasons.append("tags.json 读取失败")
                tags = []
        if tag_emb.is_file():
            try:
                tag_arr = np.load(tag_emb, mmap_mode="r")
                tag_rows, tag_dim = int(tag_arr.shape[0]), int(tag_arr.shape[1])
            except Exception as exc:  # noqa: BLE001
                reasons.append("tag_embeddings.npy 读取失败：{}".format(exc))
        if tags and tag_rows != len(tags):
            reasons.append("tag 向量 {} 与 tags={} 不一致".format(tag_rows, len(tags)))
        if dim and tag_dim and tag_dim != dim:
            reasons.append("tag 维度 {} 与主向量 {} 不一致".format(tag_dim, dim))
        tag_meta = {}
        if tag_meta_path.is_file():
            try:
                tag_meta = json.loads(tag_meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                reasons.append("tag_meta.json 读取失败")
        if tag_meta:
            if meta.get("embed_model") and tag_meta.get("embed_model") and \
                    tag_meta["embed_model"] != meta["embed_model"]:
                reasons.append("tag 模型 {} 与主向量模型 {} 不一致".format(
                    tag_meta["embed_model"], meta["embed_model"]))
            tm_dim = int(tag_meta.get("dim") or 0)
            if tm_dim and dim and tm_dim != dim:
                reasons.append("tag_meta 维度 {} 与主向量 {} 不一致".format(tm_dim, dim))

        built_raw = str(meta.get("embeddings_built_at") or meta.get("built_at") or "")
        built_ts = 0.0
        if built_raw:
            try:
                built_ts = datetime.strptime(built_raw, "%Y-%m-%dT%H:%M:%S").timestamp()
            except ValueError:
                built_ts = 0.0

        # anime.db 用 data_version 判断；版本时间晚于向量构建时间才需要更新
        version = self._data_version()
        version_ts = _dump_version_ts(version)
        if version_ts:
            if built_ts and built_ts + 1 < version_ts:
                reasons.append("向量构建时间 {} 早于 anime.db 版本 {}".format(
                    built_raw, version))
        else:
            # 旧库没有可解析版本时退回 mtime
            try:
                if Path(self.anime_db).is_file() and \
                        Path(self.anime_db).stat().st_mtime > built_ts + 1:
                    reasons.append("anime.db 比向量 新")
            except OSError:
                pass

        sources = []
        if getattr(self, "enable_moegirl", False):
            sources.extend([
                Path(self.moegirl_db),
                Path(self.moegirl_bodies),
            ])
        for src in sources:
            try:
                if src.is_file() and src.stat().st_mtime > built_ts + 1:
                    reasons.append("{} 比向量 新".format(src.name))
            except OSError:
                pass
        try:
            if chunks.is_file() and chunks.stat().st_mtime > built_ts + 1:
                reasons.append("chunks.jsonl 比向量 新")
        except OSError:
            pass

        return json_response({
            "needs_update": bool(reasons),
            "reasons": reasons,
            "total": total or rows,
            "embeddings": rows,
            "manifest": len(hashes),
            "dim": dim,
            "tags": len(tags),
            "tag_embeddings": tag_rows,
            "tag_dim": tag_dim,
            "built_at": built_raw,
            "data_version": version,
            "size_mb": round(emb.stat().st_size / 2**20, 1) if emb.is_file() else 0,
            "embed_model": meta.get("embed_model") or "",
            "tag_model": tag_meta.get("embed_model") or "",
        })

    async def api_vector_status(self):
        return json_response(dict(self._maint_job("vector")))

    async def api_vector_update(self):
        if not Path(self.anime_db).is_file():
            return error_response(self._missing_message())
        job = self._maint_job("vector")
        if job.get("state") == "running":
            return json_response(dict(job))
        job.update({"state": "running", "message": "准备重建…",
                    "percent": 0, "log": []})
        args = [
            "--anime-db", str(self.anime_db),
            "--moegirl-db", str(self.moegirl_db),
            "--bodies", str(self.moegirl_bodies),
            "--vec-dir", str(self.vec_dir),
        ]
        if getattr(self, "enable_moegirl", False):
            args.append("--with-moegirl")
        pid = str(getattr(self, "embedding_provider_id", "") or "")
        if pid:
            args.extend(["--provider-id", pid])
        asyncio.create_task(self._run_script_job(
            "vector", _PLUGIN_ROOT / "scripts" / "build" / "sync_vectors.py", args))
        return json_response(dict(job))

    async def api_translation_check(self):
        """按 src_hash 统计待译条数。"""
        if not Path(self.anime_db).is_file():
            return error_response(self._missing_message())

        db = Database(self.anime_db)
        conn = tr.open_db(self.translate_db)
        out: dict[str, dict] = {}
        total = 0
        chars = 0
        try:
            for kind in tr.KINDS:
                rows = [
                    {"ref_id": r["ref_id"], "text": r["text"], "label": r["label"] or ""}
                    for r in bgm_queries.get_translation_sources(db.conn, kind)
                ]
                todo = tr_queries.pending(conn, kind, rows)
                n = len(todo)
                c = sum(len(r["text"]) for r in todo)
                out[kind] = {"pending": n, "chars": c, "source": len(rows)}
                total += n
                chars += c
        finally:
            conn.close()
            db.close()
        return json_response({
            "pending": out,
            "total": total,
            "chars": chars,
            "has_provider": bool(self.cfg.get("translate_provider_id")),
        })

    async def api_translation_status(self):
        return json_response(dict(self._maint_job("translation")))

    async def api_translation_update(self):
        if not Path(self.anime_db).is_file():
            return error_response(self._missing_message())
        job = self._maint_job("translation")
        if job.get("state") == "running":
            return json_response(dict(job))
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        raw = payload.get("kinds")
        if isinstance(raw, str):
            raw = [raw]
        wanted = set(raw) if isinstance(raw, list) else set(tr.KINDS)
        kinds = [k for k in tr.KINDS if k in wanted]
        if not kinds:
            return error_response("至少选择一层：character / work / episode")
        job.update({"state": "running", "message": "准备同步翻译…",
                    "percent": 0, "log": [], "kinds": kinds})
        args = [
            "--anime-db", str(self.anime_db),
            "--translate-db", str(self.translate_db),
        ]
        for kind in kinds:
            args.extend(["--kind", kind])
        pid = str(self.cfg.get("translate_provider_id") or "")
        if pid:
            args.extend(["--provider-id", pid])
        conc = max(1, min(20, int(self.cfg.get("translate_concurrency") or 10)))
        args.extend(["--concurrency", str(conc)])
        asyncio.create_task(self._run_script_job(
            "translation", _PLUGIN_ROOT / "scripts" / "build" / "translate.py", args))
        return json_response(dict(job))

    # ── 配置 ────────────────────────────────────────────────────

    def _plugin_conf(self):
        """拿到本插件的 AstrBotConfig 实例"""
        conf = getattr(self, "conf", None)
        if conf is not None:
            return conf
        root = (__package__ or "").rsplit(".", 1)[0]
        md = star_map.get(root + ".main") if root else None
        return getattr(md, "config", None)

    async def api_config(self):
        conf = self._plugin_conf()
        values = {}
        if conf is not None:
            values = {k: dict(v) for k, v in conf.items() if isinstance(v, dict)}
        flat = _cfg.flatten(values) if values else dict(self.cfg)
        return json_response({
            "flat": flat,
            "pending": sorted(self._pending_changes),
            "values": values,
            "schema": {
                group: [
                    {
                        "key": key,
                        "type": meta.get("type"),
                        "description": meta.get("description"),
                        "hint": meta.get("hint"),
                        "default": meta.get("default"),
                    }
                    for key, meta in (node.get("items") or {}).items()
                ]
                for group, node in json.loads(
                    (_PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig")
                ).items()
            },
        })

    async def api_config_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload:
            return error_response("请求体为空")
        conf = self._plugin_conf()
        if conf is None:
            return error_response("拿不到插件配置对象，请在 AstrBot 面板里改")
        changed = []
        for group, items in payload.items():
            if group not in conf or not isinstance(conf[group], dict):
                continue
            for key, value in (items or {}).items():
                if key not in conf[group]:
                    continue
                if conf[group][key] == value:
                    continue
                conf[group][key] = value
                changed.append("{}.{}".format(group, key))
                if key in self._runtime_cfg and value == self._runtime_cfg.get(key):
                    self._pending_changes.discard(key)
                else:
                    self._pending_changes.add(key)

        if not changed:
            return json_response({
                "saved": False,
                "message": "没有变化",
                "pending": sorted(self._pending_changes),
            })
        save = getattr(conf, "save_config", None)
        if callable(save):
            save()
        # 同步内存快照
        self.cfg = _cfg.flatten({k: dict(v) for k, v in conf.items()
                                 if isinstance(v, dict)})
        return json_response({
            "saved": True,
            "changed": changed,
            "pending": sorted(self._pending_changes),
            "message": "已保存 {} 项".format(len(changed)),
        })

    # ── 重启 ────────────────────────────────────────────────────

    async def api_restart(self):
        """重载插件"""
        asyncio.create_task(self._restart_soon())
        return json_response({"restarting": True, "name": _PLUGIN_ROOT.name})

    async def _restart_soon(self) -> None:
        await asyncio.sleep(0.4)
        sm = getattr(self.context, "_star_manager", None)
        if sm is None:
            logger.error(
                "[animerag] 拿不到 star_manager，无法自动重载 —— 请在面板里手动重载"
            )
            return
        try:
            await sm.reload(_PLUGIN_ROOT.name)
            logger.info("[animerag] 已重载")
        except Exception as exc:  # noqa: BLE001
            logger.error("[animerag] 重载异常：%s: %s", type(exc).__name__, exc)
