from __future__ import annotations

import re


_DROP_LINE_MARKS = (
    "以下内容含有剧透成分",
    "宽屏模式显示视频",
    "本页面在移动端",
    "折叠",
    "展开",
    '" lang=',
    'dir="ltr"',
    "欢迎正在阅读这个条目的您协助编辑本条目",
    "编辑前请阅读Wiki入门",
    "查找相关资料。萌娘百科祝您",
    "萌娘百科祝您在本站度过愉快的时光",
    "可以从以下几个方面加以改进",
    "欢迎您参与完善本条目",
)

_INFOBOX_LINES = frozenset({
    "基本资料", "本名", "别号", "名字", "姓名", "译名", "中文名", "日文名",
    "发色", "瞳色", "年龄", "生日", "星座", "血型", "身高", "体重",
    "声优", "多位声优", "萌点", "所属团体", "出身地区", "活动范围",
    "个人状态", "亲属或相关人", "宠物别名", "宠物名称", "宠物种类",
    "主人或同伴", "种族", "职业", "国籍", "籍贯", "活跃年代", "出生",
    "分类", "初次登场", "最新登场", "地区图鉴", "图鉴编号", "属性",
    "品质", "阵营", "身长", "翼展", "乐器", "主武器", "武器",
})

# 图注特征：短、无句末标点、带这些词
_CAPTION_HINTS = ("截图", "截屏", "设定图", "立绘", "模型图", "封面", "出处下同", "（原画）")

_DROP_LINE_EXACT = frozenset({
    "相关音乐", "创作相关", "动画版音乐", "音乐", "其他",
    "登场机体", "相关机体", "其他机体", "相关角色", "其他角色",
    "相关作品", "其他人物", "登场人物", "主要人物", "其他势力",
    "人型自在战斗装甲骑", "外部链接", "参考资料", "导航", "模板",
    "展开", "折叠", "目录",
})

_DECO_RE = re.compile(r"^[+=\-_*~·#<>─━]{3,}$")
_SYMBOLS = set("+=-_*~·#<>─━")


def _mostly_symbols(line: str) -> bool:
    """符号占比 >= 60% 且长度 >= 6 —— 分隔线。
    例：'+++++++++++++++++DANGER+++++++++++++++++'（符号占 94%）
    """
    if len(line) < 6:
        return False
    sym = sum(1 for ch in line if ch in _SYMBOLS)
    return sym / len(line) >= 0.6
_BULLET_RE = re.compile(r"[•·]{1,}s*S")            # 导航模板
_SENT_RE = re.compile(r"[。！？…!?]$")


def clean_body_text(text: str) -> str:
    """行级清洗正文。返回清洗后的文本（可能为空串）。"""
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    prev_was_field = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # ① 整行标记
        if any(m in line for m in _DROP_LINE_MARKS):
            prev_was_field = False
            continue
        if _DECO_RE.match(line) or _mostly_symbols(line):
            prev_was_field = False
            continue
        # ③ 导航模板（一行里塞了一串 • 分隔的名字）
        if line.count("•") >= 2 or _BULLET_RE.search(line) and line.count("·") >= 3:
            prev_was_field = False
            continue
        if line in _DROP_LINE_EXACT:
            prev_was_field = False
            continue
        if line in _INFOBOX_LINES:
            prev_was_field = True
            continue
        if prev_was_field:
            prev_was_field = False
            if len(line) <= 60 and not _SENT_RE.search(line):
                continue
        # ⑤ 图注
        if (len(line) <= 24 and not _SENT_RE.search(line)
                and any(h in line for h in _CAPTION_HINTS)):
            continue
        out.append(line)
    return "\n".join(out)
