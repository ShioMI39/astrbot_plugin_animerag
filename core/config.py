from __future__ import annotations

import json
import os
import winreg
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = ROOT / "data"
SCHEMA_PATH = ROOT / "_conf_schema.json"

PLUGIN_DIR_NAME = ROOT.name

# 插件运行时注入的 AstrBotConfig 实例
_LIVE_CONFIG = None

def astrbot_root() -> Path | None:
    root = os.environ.get("ASTRBOT_ROOT")
    if root:
        return Path(root)
    cand = ROOT.parent.parent
    if cand.name == "data":
        return cand.parent
    return None


def _astrbot_config_path() -> Path:
    root = astrbot_root()
    if root is None:
        return Path()
    return root / "data" / "config" / "{}_config.json".format(PLUGIN_DIR_NAME)


def set_astrbot_config(cfg) -> None:
    """注入 AstrBot 的活配置对象。"""
    global _LIVE_CONFIG
    _LIVE_CONFIG = cfg


@lru_cache(maxsize=1)
def _disk_config() -> dict:
    root = astrbot_root()
    if root is None:
        return {}
    p = root / "data" / "cmd_config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def astrbot_config() -> dict | None:
    """AstrBot 全局配置。"""
    if _LIVE_CONFIG and _LIVE_CONFIG.get("provider"):
        return _LIVE_CONFIG
    return _disk_config()


def chat_provider(provider_id: str) -> dict | None:
    """从 AstrBot 配置里解出一个 chat_completion provider 的连接参数。"""
    if not provider_id:
        return None
    cfg = astrbot_config()
    entry = next(
        (p for p in (cfg.get("provider") or []) if p.get("id") == provider_id), None
    )
    if entry is None:
        return None

    src = entry.get("provider_source_id")
    if src:
        source = next(
            (s for s in (cfg.get("provider_sources") or []) if s.get("id") == src),
            None,
        )
        if source is None:
            return None
        api_base = source.get("api_base") or ""
        key = source.get("key") or ""
    else:
        api_base = entry.get("api_base") or ""
        key = entry.get("key") or entry.get("api_key") or ""

    # key 可能是列表（多 key 轮询），取第一个
    if isinstance(key, list):
        key = key[0] if key else ""
    key = str(key or "")

    model = entry.get("model") or ""
    if not api_base or not model:
        return None
    return {"api_base": str(api_base), "key": str(key), "model": str(model)}


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(current, raw: str):
    """环境变量是字符串，按当前默认值的类型转。"""
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        return current
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    return raw


def flatten(cfg: dict) -> dict:
    """把 type=object 的分组配置展平"""
    out: dict = {}
    for k, v in (cfg or {}).items():
        if isinstance(v, dict):
            out.update(v)
        else:
            out[k] = v
    return out


def _schema_defaults() -> dict:
    """所有配置项的默认值"""
    if not SCHEMA_PATH.exists():
        return {}

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    out: dict = {}
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("type") == "object":
            for ik, im in (meta.get("items") or {}).items():
                out[ik] = im.get("default")
        else:
            out[key] = meta.get("default")
    return out


@lru_cache(maxsize=1)
def load() -> dict:
    """读全部配置，返回扁平 dict。"""
    out = dict(_schema_defaults())
    path = _astrbot_config_path()
    if path and path.is_file():
        out.update(flatten(json.loads(path.read_text(encoding="utf-8-sig"))))

    for key in list(out):
        env = os.environ.get("ANIMERAG_" + key.upper())
        if env is not None:
            out[key] = _coerce(out[key], env)
    return out


def get(key: str, default=None):
    return load().get(key, default)


def data_dir() -> Path:
    """数据目录。配置 data_dir 可以指向插件目录之外。"""
    raw = load().get("data_dir")
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _DEFAULT_DATA


def temp_dir() -> Path:
    return data_dir() / "temp"


# data_dir 是唯一可配置的路径；下面全部由它按固定顺序派生
def anime_db() -> Path:
    return data_dir() / "anime.db"


def moegirl_db() -> Path:
    return data_dir() / "moegirl.db"


def moegirl_bodies() -> Path:
    return data_dir() / "moegirl_bodies.json"


def translate_db() -> Path:
    return data_dir() / "translate.db"


def vec_dir() -> Path:
    return data_dir() / "vec"


def dump_cache_dir() -> Path:
    return temp_dir() / "animerag-dump"


def update_proxy() -> str | None:
    """Bangumi dump 下载代理。"""
    raw = str(get("update_proxy") or "").strip()
    if not raw:
        raw = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()
    if not raw and os.name == "nt":
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            try:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            finally:
                winreg.CloseKey(key)
            if enable and server:
                server = str(server)
                if "=" in server:
                    parts = dict(
                        p.split("=", 1) for p in server.split(";") if "=" in p
                    )
                    server = parts.get("https") or parts.get("http") or ""
                raw = str(server).strip()
        except Exception:  # noqa: BLE001
            raw = ""
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def _proxy_of(cfg: dict) -> str | None:
    v = cfg.get("proxy") or ""
    return str(v) if v else None


def embedding_provider(provider_id: str) -> dict | None:
    """从 AstrBot 配置解出 Embedding provider 的连接参数。"""
    if not provider_id:
        return None
    provs = astrbot_config().get("provider") or []
    # 先按 provider ID 匹配；没有就按模型名匹配
    entry = next((p for p in provs if p.get("id") == provider_id), None)
    if entry is None:
        entry = next(
            (p for p in provs if p.get("embedding_model") == provider_id), None
        )
    if entry is None:
        return None
    api_base = str(entry.get("embedding_api_base") or "").rstrip("/")
    key = entry.get("embedding_api_key") or ""
    if isinstance(key, list):
        key = key[0] if key else ""
    # 离线重跑/排障时可用环境变量临时覆盖，不写进配置文件
    api_base = os.environ.get("ANIMERAG_EMBEDDING_BASE") or api_base
    key = os.environ.get("ANIMERAG_EMBEDDING_KEY") or key
    model = str(entry.get("embedding_model") or "")
    if not api_base or not model:
        return None
    return {
        "api_base": api_base,
        "key": str(key or ""),
        "model": model,
        "dim": int(entry.get("embedding_dimensions") or 0),
        "proxy": _proxy_of(entry),
    }


def rerank_provider(provider_id: str) -> dict | None:
    """从 AstrBot 配置解出 Rerank provider 的连接参数。"""
    if not provider_id:
        return None
    provs = astrbot_config().get("provider") or []
    entry = next((p for p in provs if p.get("id") == provider_id), None)
    if entry is None:
        entry = next(
            (p for p in provs if p.get("rerank_model") == provider_id), None
        )
    if entry is None:
        return None
    api_base = str(entry.get("rerank_api_base") or "").rstrip("/")
    suffix = str(entry.get("rerank_api_suffix") or "/v1/rerank")
    key = entry.get("rerank_api_key") or ""
    if isinstance(key, list):
        key = key[0] if key else ""
    model = str(entry.get("rerank_model") or "")
    if not api_base or not model:
        return None
    return {
        "api_base": api_base,
        "suffix": suffix if suffix.startswith("/") else "/" + suffix,
        "key": str(key or ""),
        "model": model,
        "proxy": _proxy_of(entry),
    }


def moegirl_proxy() -> str | None:
    return os.environ.get("ANIMERAG_MOEGIRL_PROXY") or None
