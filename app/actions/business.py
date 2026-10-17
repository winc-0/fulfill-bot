"""业务动作：调履约中台、把结果写回槽位、顺手产出一张运单卡片。

失败处理原则：**动作失败就终止这个流程，把失败原因说成人话，绝不继续念后面的模板。**
否则会出现"理赔没提交成功，却跟用户说已提交"这种最致命的错误。
"""

from __future__ import annotations

from typing import Any

from app.actions.registry import ActionContext, ActionResult, register
from app.core.log import get_logger
from app.infra.commerce_client import commerce_client

logger = get_logger("actions")

# 口语里说的异常类型 → 中台枚举
CLAIM_TYPES = {
    "破损": "破损",
    "坏了": "破损",
    "碎了": "破损",
    "压坏": "破损",
    "漏了": "破损",
    "丢了": "丢失",
    "丢失": "丢失",
    "少了": "丢失",
    "没收到": "丢失",
    "延误": "延误",
    "超时": "延误",
    "太慢": "延误",
    "没按时": "延误",
}


def _guess_claim_type(reason: str) -> str:
    for word, ctype in CLAIM_TYPES.items():
        if word in reason:
            return ctype
    return "破损"


def _shipment_object(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "shipment",
        "tracking_number": detail.get("tracking_number", ""),
        "goods_desc": detail.get("goods_desc", ""),
        "status": detail.get("status", ""),
        "carrier": detail.get("carrier", ""),
        "promise_time": detail.get("promise_time", ""),
    }


def _summary(detail: dict[str, Any]) -> str:
    insured = detail.get("insured_amount") or 0
    insured_text = f"保价 {insured} 元" if float(insured) > 0 else "未保价"
    return (
        f"托寄物 {detail.get('goods_desc', '')}，{detail.get('weight_kg', '')} 公斤，"
        f"{detail.get('carrier', '')}承运，{insured_text}，"
        f"承诺时效 {detail.get('promise_time', '')}。"
    )


@register("action_lookup_shipment_status")
async def action_lookup_shipment_status(ctx: ActionContext) -> ActionResult:
    known = ctx.state.known_slots()
    tracking = known.get("tracking_number") or ctx.slots.get("tracking_number", "")
    if not tracking:
        return ActionResult(messages=[{"text": "我还需要运单号才能查，麻烦发一下。"}], abort=True)

    result = await commerce_client.shipment_detail(tracking)
    if not result.ok:
        return ActionResult(messages=[{"text": result.error}], abort=True)

    detail = result.data
    slots = {
        "tracking_number": detail.get("tracking_number", tracking),
        "shipment_status": detail.get("status", ""),
        "shipment_summary": _summary(detail),
        "carrier": detail.get("carrier", ""),
        "promise_time": detail.get("promise_time", ""),
    }
    ctx.state.focused_object = {
        "type": "shipment",
        "tracking_number": detail.get("tracking_number", tracking),
        "status": detail.get("status", ""),
    }
    return ActionResult(
        messages=[{"object": _shipment_object(detail)}],
        slots=slots,
    )


@register("action_lookup_tracking")
async def action_lookup_tracking(ctx: ActionContext) -> ActionResult:
    known = ctx.state.known_slots()
    tracking = known.get("tracking_number") or ctx.slots.get("tracking_number", "")
    if not tracking:
        return ActionResult(messages=[{"text": "要查轨迹得先有运单号，麻烦发一下。"}], abort=True)

    result = await commerce_client.shipment_tracking(tracking)
    if not result.ok:
        return ActionResult(messages=[{"text": result.error}], abort=True)

    data = result.data
    traces = data.get("traces") or []
    latest = traces[0]["desc"] if traces else data.get("status_desc", "")
    slots = {
        "tracking_number": data.get("tracking_number", tracking),
        "carrier": data.get("carrier", ""),
        "shipment_status": data.get("status", ""),
        "latest_trace": f"{traces[0]['node']}：{latest}" if traces else data.get("status_desc", ""),
    }
    return ActionResult(messages=[], slots=slots)


@register("action_submit_claim")
async def action_submit_claim(ctx: ActionContext) -> ActionResult:
    known = ctx.state.known_slots()
    tracking = known.get("tracking_number") or ctx.slots.get("tracking_number", "")
    reason = known.get("claim_reason") or ctx.slots.get("claim_reason", "")
    claim_type = known.get("claim_type") or ctx.slots.get("claim_type", "") or _guess_claim_type(reason)

    if not tracking or not reason:
        missing = "运单号" if not tracking else "异常情况说明"
        return ActionResult(messages=[{"text": f"还差{missing}，补齐我就能提交。"}], abort=True)

    result = await commerce_client.submit_claim(
        tracking, claim_type, reason, operator=ctx.sender_id
    )
    if result.ok:
        data = result.data
        return ActionResult(
            messages=[],
            slots={
                "claim_id": data.get("request_id", ""),
                "claim_type": claim_type,
                "tracking_number": tracking,
            },
        )

    # 409 是业务规则而非系统故障，给一句更具体的解释
    if result.status == 409:
        text = f"运单 {tracking} 已经有一笔理赔在审核里了，不用重复提交。定责结果会短信通知你。"
    else:
        text = result.error or "理赔提交失败，稍后再试。"
    logger.info(f"[claim] 失败 status={result.status} tracking={tracking}")
    return ActionResult(messages=[{"text": text}], abort=True)


@register("action_change_address")
async def action_change_address(ctx: ActionContext) -> ActionResult:
    known = ctx.state.known_slots()
    tracking = known.get("tracking_number") or ctx.slots.get("tracking_number", "")
    new_address = known.get("new_address") or ctx.slots.get("new_address", "")

    if not tracking or not new_address:
        missing = "运单号" if not tracking else "新的派送地址"
        return ActionResult(messages=[{"text": f"还差{missing}，补齐我就能提交改址。"}], abort=True)

    result = await commerce_client.change_address(tracking, new_address, operator=ctx.sender_id)
    if result.ok:
        return ActionResult(
            messages=[],
            slots={
                "change_id": result.data.get("request_id", ""),
                "tracking_number": tracking,
                "new_address": new_address,
            },
        )

    if result.status == 409:
        text = f"运单 {tracking} 已经在派送路上了，改址来不及。可以直接联系快递员改投递方式。"
    else:
        text = result.error or "改址提交失败，稍后再试。"
    logger.info(f"[address] 失败 status={result.status} tracking={tracking}")
    return ActionResult(messages=[{"text": text}], abort=True)


@register("action_urge_delivery")
async def action_urge_delivery(ctx: ActionContext) -> ActionResult:
    known = ctx.state.known_slots()
    tracking = known.get("tracking_number") or ctx.slots.get("tracking_number", "")
    if not tracking:
        return ActionResult(messages=[{"text": "请先告诉我要催哪张运单。"}], abort=True)

    result = await commerce_client.urge_delivery(tracking, operator=ctx.sender_id)
    if not result.ok:
        if result.status == 409:
            return ActionResult(messages=[{"text": result.error}], abort=True)
        return ActionResult(messages=[{"text": result.error}], abort=True)

    return ActionResult(
        messages=[],
        slots={"urge_id": result.data.get("request_id", ""), "tracking_number": tracking},
    )
