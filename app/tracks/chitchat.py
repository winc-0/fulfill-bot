"""闲聊轨道。只在"确实没有业务意图"时接手，回答不超过两句，别抢业务的话。"""

from __future__ import annotations

from app.infra import llm

FALLBACK = {
    "你好": "你好，我是履约客服助手。查运单、看轨迹、改派送地址、催派送、报异常理赔都可以找我。",
    "谢谢": "不客气，还有别的运单要处理吗？",
    "在吗": "在的，需要我帮你查哪张运单？",
}

SYSTEM = (
    "你是仓配履约客服助手，语气自然、克制。只回一句到两句，不主动推销，不编造运单信息。"
    "你只负责闲聊与引导：**不要承诺执行任何操作**（改址、催派、提交理赔等都不行），"
    "也不要假装已经做了什么；需要办事时，引导用户说出运单号或直接提要求。"
)


async def reply(text: str) -> str:
    for key, value in FALLBACK.items():
        if key in text:
            return value
    if llm.available():
        answer = await llm.complete(text, system=SYSTEM)
        if answer:
            return answer
    return "我在。查运单、看轨迹、改派送地址、催派送、报异常理赔都可以直接跟我说。"
