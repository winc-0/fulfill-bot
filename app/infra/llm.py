"""模型调用封装。只有一处，方便统一超时、日志与降级。"""

from __future__ import annotations

from openai import AsyncOpenAI

from app.conf.settings import settings
from app.core.log import get_logger

logger = get_logger("llm")

_client: AsyncOpenAI | None = None


def available() -> bool:
    return settings.llm.enabled


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
            timeout=settings.llm.timeout,
        )
    return _client


async def complete(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    temperature: float | None = None,
) -> str:
    """一次朴素补全。失败返回空串——调用方必须准备兜底文案。"""
    if not available():
        return ""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = await client().chat.completions.create(
            model=model or settings.llm.writer_model,
            messages=messages,
            temperature=settings.llm.temperature if temperature is None else temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning(f"[llm] 调用失败：{type(exc).__name__} {str(exc)[:80]}")
        return ""
