"""知识轨道：回答"政策/规则"类问题。

不做向量检索是有意的：语料就几页，字符二元组 + 标题加权已经够用，
而且**可解释**——能直接说清"命中了哪一段、得分多少"，排查错答比向量库容易得多。
真到了几百篇再换向量库，接口不用动。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.log import get_logger
from app.infra import llm

logger = get_logger("knowledge")

MIN_SCORE = 0.12

# 口语问法 → 文档用词。不做这一步，"能赔吗"永远匹配不上"赔付标准"。
EXPANSIONS: tuple[tuple[str, str], ...] = (
    ("赔", "赔付 理赔 赔偿 保价"),
    ("破损", "破损 内件 凭证"),
    ("丢失", "丢失 整件 查找"),
    ("延误", "延误 超时 承诺时效"),
    ("保价", "保价 声明价值 保费"),
    ("多久", "时间 时效 工作日"),
    ("什么时候", "时间 时效 派送"),
    ("出库", "出库 时效 起算"),
    ("地址", "派送地址 改址"),
    ("拦截", "拦截 拒收 退回"),
    ("禁运", "禁运 锂电池 查扣"),
)


def expand(query: str) -> str:
    extra = [words for key, words in EXPANSIONS if key in query]
    return f"{query} {' '.join(extra)}".strip()


def expansion_only(query: str) -> str:
    """只取扩展词。扩展是"敲门砖"：帮口语问法撞上文档用词，
    但它本身很泛（"理赔 赔付 破损"），权重必须低于用户原话。"""
    extra = [words for key, words in EXPANSIONS if key in query]
    return " ".join(extra)


@dataclass
class Passage:
    title: str
    text: str
    source: str
    score: float = 0.0


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def load_passages(kb_dir: Path) -> list[Passage]:
    passages: list[Passage] = []
    if not kb_dir.is_dir():
        return passages
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        # 按二级标题切段，标题本身也参与打分
        chunks = re.split(r"^##\s+", text, flags=re.MULTILINE)
        for chunk in chunks[1:]:
            lines = chunk.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            if body:
                passages.append(Passage(title=title, text=body, source=path.name))
    return passages


# 词表外的二元组（"怎么""可以"这类跨句子的组合）按最高区分度计权
_UNSEEN_IDF = 2.4


def _idf(passages: list[Passage]) -> dict[str, float]:
    """按"出现在几段里"折算出区分度。

    没有这一步，"理赔""赔付"这种每段都有的高频词会把真正的关键词（"破损""到账"）
    淹掉——实测"包裹破损怎么赔"会被"保价与理赔材料"抢走，就因为那段也提了破损。
    """
    df: dict[str, int] = {}
    for passage in passages:
        for gram in _bigrams(passage.title + passage.text):
            df[gram] = df.get(gram, 0) + 1
    total = len(passages) or 1
    return {gram: math.log(1 + total / count) for gram, count in df.items()}


def _ratio(query_grams: set[str], target: set[str], idf: dict[str, float]) -> float:
    """查询词在目标文本里的加权覆盖率，取值 0~1。"""
    if not query_grams:
        return 0.0
    total = sum(idf.get(g, _UNSEEN_IDF) for g in query_grams)
    if not total:
        return 0.0
    hit = sum(idf.get(g, _UNSEEN_IDF) for g in query_grams & target)
    return hit / total


def retrieve(
    query: str, passages: list[Passage], top_k: int = 3, extra: str = ""
) -> list[Passage]:
    """正文覆盖率 0.6 + 扩展词 0.15 + 标题覆盖率 0.45。

    标题权重给得重，是因为这一批语料的标题就是"这段在讲什么"的浓缩，
    用户问法和标题撞上时几乎一定是想问那段。
    """
    idf = _idf(passages)
    query_grams = _bigrams(query)
    if not query_grams:
        return []
    extra_grams = _bigrams(extra) - query_grams
    scored: list[Passage] = []
    for passage in passages:
        body_grams = _bigrams(passage.text)
        title_grams = _bigrams(passage.title)
        score = (
            0.60 * _ratio(query_grams, body_grams, idf)
            + 0.15 * _ratio(extra_grams, body_grams, idf)
            + 0.45 * _ratio(query_grams | extra_grams, title_grams, idf)
        )
        scored.append(
            Passage(
                title=passage.title,
                text=passage.text,
                source=passage.source,
                score=round(score, 3),
            )
        )
    scored.sort(key=lambda p: -p.score)
    return [p for p in scored[:top_k] if p.score > 0]


ANSWER_PROMPT = """你是仓配履约客服。只根据下面给出的服务规则片段回答，不要编造片段里没有的内容。
如果片段不足以回答，就直说"这个我没查到明确说法"。
回答控制在 3 句话内，语气自然，不要罗列条款编号。

政策片段：
{passages}

用户问题：{question}

客服回答："""


async def answer(
    question: str, passages: list[Passage], context_text: str = ""
) -> tuple[str, list[Passage], bool]:
    """返回 (回答文本, 命中的片段, 是否查到了依据)。

    context_text 是"这句话本身没信息量、但上下文有"时的补充：
    比如用户只说"那这个能赔吗"，光看这句检索必然落空，
    把当前运单信息拼进去才有戏——这就是多轮里的指代消解。
    """
    raw = f"{question} {context_text}".strip()
    query = expand(raw)  # 只用于日志，让人能看出扩展了哪些词
    hits = retrieve(raw, passages, extra=expansion_only(raw))
    if not hits or hits[0].score < MIN_SCORE:
        logger.info(f"[knowledge] 未命中（最高分 {hits[0].score if hits else 0}）query={query[:40]}")
        return "", hits, False

    context = "\n\n".join(f"## {p.title}\n{p.text}" for p in hits)
    if llm.available():
        text = await llm.complete(
            ANSWER_PROMPT.format(passages=context, question=question),
            system="你是仓配履约客服，回答必须严格基于给定服务规则片段。",
        )
        if text:
            return text, hits, True

    # 没有模型就摘录最相关的一段，保证不编
    head = hits[0]
    return f"{head.title}：{head.text.split('。')[0]}。", hits, True
