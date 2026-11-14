from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterable

import numpy as np

from .. import config
from ..db.bgm import queries
from ..normalize import to_simplified

# 题材 / 主题 / 受众
GENRE_TAGS = (
    "搞笑", "喜剧", "剧情", "吐槽", "恶搞", "无厘头", "爆笑", "欢乐",
    "奇幻", "科幻", "魔幻", "玄幻", "超现实", "神话", "神魔",
    "仙侠", "修仙", "武侠", "和风", "赛博朋克", "末世", "异世界",
    "穿越", "转生",
    "战斗", "动作", "热血", "冒险", "机战", "机甲", "机器人", "萝卜",
    "超能力", "魔法", "魔法少女", "智斗",
    "竞技", "运动", "体育", "棒球", "足球", "赛车", "格斗", "卡牌",
    "游戏", "网游",
    "恋爱", "纯爱", "爱情", "后宫", "逆后宫", "乙女", "乙女向",
    "百合", "轻百合", "耽美", "BL", "GL", "腐", "腐向", "少女漫",
    "校园", "青春", "日常", "治愈", "家庭", "亲情", "友情",
    "职场", "美食", "偶像", "乐队", "音乐",
    "历史", "战争", "推理", "悬疑", "恐怖", "惊悚", "猎奇",
    "犯罪", "灵异", "复仇", "励志", "科普", "动物", "特摄", "实验动画",
    "子供向", "幼女向", "少女向", "少年向", "青年向", "女性向",
    "致郁", "催泪", "温馨", "平静",
)

# 地区
REGION_TAGS = (
    "日本", "中国", "欧美", "美国", "国产", "韩国", "法国", "英国",
    "台湾", "香港", "华语", "欧洲", "加拿大", "俄罗斯", "苏联",
)

# 改编来源
SOURCE_TAGS = (
    "原创", "漫画改", "小说改", "轻小说改", "游戏改", "GAL改",
    "网文改", "影视改", "动画改",
)

ALL_TAGS = GENRE_TAGS + REGION_TAGS + SOURCE_TAGS

ALIASES = {
    "漫改": "漫画改",
    "轻改": "轻小说改",
    "学园": "校园",
    "治愈系": "治愈",
    "国漫": "国产",
    "国创": "国产",
    "国动": "国产",
}

# canonical -> 数据库里实际可能出现的写法
CANON_RAW: dict[str, set[str]] = {t: {t} for t in ALL_TAGS}
for _alias, _canon in ALIASES.items():
    CANON_RAW.setdefault(_canon, {_canon}).add(_alias)
CANON_RAW["国产"].update({"国产动画"})
CANON_RAW["日本"].update({"日本动画"})
CANON_RAW["中国"].update({"中国动画"})
CANON_RAW["美国"].update({"美国动画"})
CANON_RAW["欧美"].update({"欧美动画"})
CANON_RAW["韩国"].update({"韩国动画"})
CANON_RAW["漫画改"].update({"漫画改编"})
CANON_RAW["游戏改"].update({"游戏改编"})
CANON_RAW["小说改"].update({"小说改编"})

RAW_CANON = {
    raw: canon
    for canon, raws in CANON_RAW.items()
    for raw in raws
}

# 扫描用：归一化后的 surface -> canonical
_TERMS: dict[str, str] = {}
for _canon in ALL_TAGS:
    for _raw in CANON_RAW.get(_canon, {_canon}):
        _TERMS[unicodedata.normalize("NFKC", to_simplified(_raw)).lower()] = _canon
for _alias, _canon in ALIASES.items():
    _TERMS[unicodedata.normalize("NFKC", to_simplified(_alias)).lower()] = _canon

_MAX_TERM = max((len(t) for t in _TERMS), default=2)

# ── tag 语义索引（tag gloss 向量） ──────────────────────────────
TAG_TEMPLATE = "{}：动画题材标签"

GLOSSES = {
    "搞笑": "以搞笑、吐槽、喜剧桥段为主要目的",
    "喜剧": "轻松幽默、以喜剧冲突为主",
    "剧情": "重视故事推进、人物关系和戏剧冲突",
    "奇幻": "魔法、架空世界、非现实设定",
    "科幻": "未来科技、宇宙、机器人、赛博空间",
    "魔幻": "魔法与幻想元素",
    "玄幻": "东方玄幻、修炼、神魔设定",
    "超现实": "梦境、超自然、非日常",
    "神话": "神话传说、神明",
    "神魔": "神与魔的对抗、神话设定",
    "仙侠": "东方仙侠、修真、门派",
    "修仙": "修炼成仙、宗门",
    "武侠": "江湖、武林、侠客",
    "和风": "日本传统文化、和风背景",
    "赛博朋克": "高科技低生活、义体、霓虹都市",
    "末世": "末日、废土、生存",
    "异世界": "穿越或转生到异世界",
    "穿越": "穿越时空或世界",
    "转生": "转生、重生、异世界",
    "战斗": "打斗、战斗、动作场面",
    "动作": "动作、打斗、冒险",
    "热血": "热血、奋斗、燃",
    "冒险": "探险、旅途、冒险",
    "机战": "机器人、机甲战斗",
    "机甲": "机甲、机器人",
    "机器人": "机器人、机械",
    "萝卜": "机器人动画（robot）",
    "超能力": "超能力、异能",
    "魔法": "魔法、法术",
    "魔法少女": "少女使用魔法的变身战斗",
    "智斗": "智力博弈、策略对决",
    "竞技": "竞技、比赛、对决",
    "运动": "体育运动、竞技",
    "体育": "体育竞技",
    "棒球": "棒球运动",
    "足球": "足球运动",
    "赛车": "赛车、竞速",
    "格斗": "格斗、武术对决",
    "卡牌": "卡牌、卡片对战",
    "游戏": "游戏、游戏世界",
    "网游": "网络游戏、虚拟世界",
    "恋爱": "以恋爱关系、告白、约会为主要内容",
    "纯爱": "专一、纯粹的恋爱",
    "爱情": "爱情、恋爱",
    "后宫": "多个角色围绕主角的恋爱喜剧",
    "逆后宫": "多个男性角色围绕女主角",
    "乙女": "面向女性的恋爱作品",
    "乙女向": "面向女性的恋爱作品",
    "百合": "女性之间的恋爱或深厚友情",
    "轻百合": "轻松的女性友情/暧昧",
    "耽美": "男性之间的恋爱",
    "BL": "男性之间的恋爱",
    "GL": "女性之间的恋爱",
    "腐": "男性之间的恋爱/同人",
    "腐向": "男性之间的恋爱/同人",
    "少女漫": "少女漫画风格的恋爱",
    "校园": "以学校生活、学生日常、社团活动为主要背景",
    "青春": "青春成长、校园、恋爱",
    "日常": "日常生活中的小故事",
    "治愈": "节奏舒缓、氛围温柔、让人放松",
    "家庭": "家庭生活、亲情",
    "亲情": "家人之间的感情",
    "友情": "朋友之间的感情",
    "职场": "工作、职场、社会人",
    "美食": "料理、美食",
    "偶像": "偶像活动、Live、粉丝",
    "乐队": "乐队、音乐活动",
    "音乐": "音乐、乐队、演奏",
    "历史": "历史背景、历史人物",
    "战争": "战争、军事冲突",
    "推理": "推理、侦探、解谜",
    "悬疑": "悬念、谜团、反转",
    "恐怖": "恐怖、惊悚、灵异",
    "惊悚": "紧张、惊悚",
    "猎奇": "猎奇、重口、异常",
    "犯罪": "犯罪、悬疑、警匪",
    "灵异": "灵异、鬼怪",
    "复仇": "复仇、恩怨",
    "励志": "励志、成长",
    "科普": "科普、知识",
    "动物": "动物、宠物",
    "特摄": "特摄、变身英雄",
    "实验动画": "实验性、非主流表现",
    "子供向": "面向儿童的动画",
    "幼女向": "面向幼女的动画",
    "少女向": "面向少女的动画",
    "少年向": "面向少年的动画",
    "青年向": "面向青年的动画",
    "女性向": "面向女性的动画",
    "致郁": "压抑、悲伤、黑暗",
    "催泪": "感人、催泪",
    "温馨": "温暖、治愈",
    "平静": "平静、舒缓",
}

_INDEX: tuple[list[str], np.ndarray] | None = None


def _load_semantic() -> tuple[list[str], np.ndarray] | None:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    vec_path = config.vec_dir() / "tag_embeddings.npy"
    tags_path = config.vec_dir() / "tags.json"
    if not (vec_path.is_file() and tags_path.is_file()):
        return None
    try:
        tags = json.loads(tags_path.read_text(encoding="utf-8"))
        vec = np.load(vec_path).astype(np.float32)
    except Exception:
        return None
    if not isinstance(tags, list) or vec.ndim != 2 or vec.shape[0] != len(tags):
        return None
    _INDEX = (tags, vec)
    return _INDEX


def semantic_tags(
    qvec,
    top_k: int = 3,
    tau: float = 0.62,
    delta: float = 0.02,
    high: float = 0.68,
    floor_min: float = 0.55,
) -> list[str]:
    """query 向量 -> tag 词表。

    high 以上视为高置信，不再要求与第二名拉开差距（避免近义标签因为分差太小被误杀）
    tau~high 之间要求 >= delta。
    """
    index = _load_semantic()
    if index is None or qvec is None:
        return []
    tags, mat = index
    q = np.asarray(qvec, dtype=np.float32).reshape(-1)
    if q.size != mat.shape[1]:
        return []
    norm = float(np.linalg.norm(q))
    if norm == 0:
        return []
    q = q / norm
    scores = mat @ q
    order = np.argsort(-scores)
    first = float(scores[order[0]])
    second = float(scores[order[1]]) if len(order) > 1 else 0.0
    if first < tau:
        return []
    if first < high and first - second < delta:
        return []
    floor = max(floor_min, first - 0.10)
    picked = [tags[int(i)] for i in order[:top_k] if float(scores[int(i)]) >= floor]
    return picked


def scan(query: str) -> list[str]:
    """从 query 里扫出白名单 tag，返回 canonical 列表（按出现顺序去重）。"""
    q = unicodedata.normalize("NFKC", to_simplified(query or "")).lower()
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(q):
        hit = None
        for length in range(min(_MAX_TERM, len(q) - i), 1, -1):
            term = q[i:i + length]
            if term in _TERMS:
                hit = (_TERMS[term], length)
                break
        if hit is None:
            i += 1
            continue
        canon, length = hit
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
        i += length
    return out


def search_works(
    conn,
    tags: Iterable[str],
    limit: int = 5,
    min_match: int | None = None,
) -> list[dict[str, Any]]:
    """按 canonical tags 查作品。

    多 tag 时默认要求 matched >= 2，单 tag 时 >= 1；
    min_match 可覆盖多 tag 的阈值（单 tag 仍不会要求 2）。
    排序 matched 降序 -> favorite_done 降序。
    """
    canon = list(dict.fromkeys(t for t in tags if t))
    if not canon:
        return []
    if len(canon) == 1:
        threshold = 1
    else:
        threshold = max(2, int(min_match or 2))
    raw: set[str] = set()
    for tag in canon:
        raw.update(CANON_RAW.get(tag, {tag}))

    rows = queries.get_tag_rows(conn, raw)

    works: dict[int, dict[str, Any]] = {}
    for work_id, tag, favorite in rows:
        work = works.setdefault(int(work_id), {"favorite": int(favorite or 0), "tags": set()})
        work["tags"].add(RAW_CANON.get(tag, tag))

    picked = [
        (wid, data) for wid, data in works.items()
        if len(data["tags"]) >= threshold
    ]
    picked.sort(key=lambda x: (-len(x[1]["tags"]), -x[1]["favorite"], x[0]))
    picked = picked[:limit]

    out: list[dict[str, Any]] = []
    if not picked:
        return out
    ids = [wid for wid, _ in picked]
    titles = queries.get_work_titles(conn, ids)
    for wid, data in picked:
        out.append({
            "work_id": wid,
            "title": titles.get(wid, "#{}".format(wid)),
            "matched": len(data["tags"]),
            "tags": sorted(data["tags"]),
            "favorite_done": data["favorite"],
        })
    return out
