"""说话类动作：念模板、等输入。

`mode: rephrase` 是"模板打底 + 模型润色"：模板保证内容正确（数字、单号、政策口径），
模型只负责让它像人话。模型挂了就退回模板原文，用户顶多觉得机械，不会看到错信息。
"""

from __future__ import annotations

from typing import Any

from app.actions.registry import ActionContext, ActionResult, register
from app.core.text import render
from app.infra import llm


def _args(ctx: ActionContext) -> Any:
    """args 支持两种写法：内联 dict，或 "context.response" 这种引用。"""
    args = ctx.step.args
    if isinstance(args, str):
        key = args.split(".")[-1]
        value = (ctx.context or {}).get(key)
        return value if isinstance(value, dict) else {}
    return args or {}


def _history(ctx: ActionContext) -> str:
    lines = []
    for message in ctx.state.messages[-6:]:
        role = "用户" if message.get("role") == "user" else "客服"
        text = message.get("text") or (message.get("object") or {}).get("title") or ""
        if text:
            lines.append(f"{role}：{text}")
    return "\n".join(lines) or "（无）"


@register("action_response")
async def action_response(ctx: ActionContext) -> ActionResult:
    args = _args(ctx)
    slots = {**ctx.state.known_slots(), **ctx.slots}
    text = render(str(args.get("text") or ""), slots, ctx.context)
    suggestions = args.get("suggestions") or None

    if args.get("mode") == "rephrase" and llm.available():
        prompt = str(args.get("prompt") or "").format(
            history=_history(ctx),
            user_message=ctx.user_text,
            current_response=text,
        )
        polished = await llm.complete(prompt)
        # 润色只许"换说法"，不许变长：超过原文 2 倍就当它跑偏了，用原文
        if polished and len(polished) <= max(len(text) * 2, 60):
            text = polished

    payload: dict[str, Any] = {"text": text}
    if suggestions:
        payload["suggestions"] = suggestions
    return ActionResult(messages=[payload], slots=slots)


@register("action_listen")
async def action_listen(ctx: ActionContext) -> ActionResult:
    """纯等待：collect 步骤已经念过追问，这里不需要再说什么。"""
    return ActionResult(messages=[])
