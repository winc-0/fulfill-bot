"""规划结果校验（防幻觉）。

模型有三类典型错误，这里逐一拦掉：
1. 编一个 YAML 里根本不存在的 flow；
2. 往槽位里塞没定义过的字段；
3. 状态对不上（没有挂起任务却说"恢复"、没有进行中的任务却说"取消"）。

校验失败**不是报错**，而是降级成 clarify——宁可多问一句，也不要照着幻觉往下执行。
"""

from __future__ import annotations

import re

from app.conf.settings import settings
from app.dialogue.planner import ACTIONS, TurnPlan
from app.core.text import find_tracking_number, normalize
from app.dialogue.state import TASK_SCOPED_SLOTS, DialogueState
from app.flows.schema import FlowRegistry

# "运单号是多少""什么时候到"这类问句问的是**当前这一单**的具体字段，
# 答案在状态里而不在知识库里，应该回业务流程取数，而不是走规则检索。
LOGISTICS_ATTR = re.compile(
    r"运单|快递单|物流单|单号|到哪|几天到|几天能到|什么时候到|几号到|签收|谁送|"
    r"哪家快递|什么快递|物流信息|物流进度|轨迹|走到哪"
)

# "能理赔吗""可以改址吗"是问可行性，"帮我报异常"是要办事。模型经常把前者当后者，
# 一串追问下来用户还没说要办，就被问"异常情况是什么"了。
FEASIBILITY_ASK = re.compile(r"(能不能|可不可以|能|可以|支持).{0,4}(赔|理赔|改|改址|拦截|拒收)")
ACTION_REQUEST = ("帮我", "我要", "给我", "申请", "麻烦", "帮忙", "报案", "报一下")

# 异常说明里不该出现"帮我报异常"这类意图词——那不是情况说明，是用户把整句话塞进了槽位
INTENT_IN_REASON = ("报异常", "理赔", "申请", "帮我", "我要", "给我", "报案", "取消")


def _reconcile_tracking_number(
    plan: TurnPlan, state: DialogueState, user_text: str
) -> str | None:
    """运单号只认两个来源：用户这句话、已知槽位。模型的抄写一律不认。

    实测模型会把 13 位的 YD20260410001 抄成 12 位的 YD2026041001（丢一位），
    这种错校验不住就会打到中台变成 404。规则很硬：
    - 用户原话里有单号 → 以原话为准；
    - 原话里没有 → 沿用已知单号，模型填的新值直接丢掉。
    """
    claimed = str(plan.slots.get("tracking_number") or "")
    if not claimed:
        return None

    from_text = find_tracking_number(normalize(user_text or ""))
    if from_text:
        if claimed != from_text:
            plan.slots["tracking_number"] = from_text
            return f"运单号以用户原话为准：{claimed} → {from_text}"
        return None

    known = str(state.known_slots().get("tracking_number") or "")
    if known and claimed != known:
        plan.slots["tracking_number"] = known
        return f"运单号与已知信息不一致（模型写 {claimed}），已纠正为 {known}"
    return None


def _reconcile_task_scoped_slots(
    plan: TurnPlan, state: DialogueState, registry: FlowRegistry, user_text: str
) -> list[str]:
    """理赔原因 / 新地址这类自由文本槽位，只认两个来源：**正在追问它**、或**这句话里逐字说过**。

    实测模型会在用户只说"包裹破损了，我要报异常"时就把 claim_reason 填成"破损"——值
    是从意图句里抽的，不是用户描述的。后果是下一轮用户刚报完运单号，流程就跳过
    ask_reason 直接提交：用户根本没机会说清损失情况，理赔单上写的是模型替他编的原因。

    为什么还要"单号已知"这个附加条件：意图句里的"破损"本来就逐字出现在用户原话里，
    只判"值在原话里"挡不住它——那个词是触发词，不是答案。所以它必须同时满足"运单号
    这一轮已经拿到"（说明用户在完整地办这件事，不是在报意图）。自由文本槽位没有
    可校验的形状（不像运单号有 YD+11 位可正则校验），可信来源只剩这两条。
    """
    notes: list[str] = []
    if not plan.slots:
        return notes

    flow_id = plan.flow_id or (state.active_task.flow_id if state.active_task else None)
    flow = registry.flows.get(flow_id) if flow_id else None
    if not flow:
        return notes

    # 正在追问哪个槽：只有续跑同一个任务时才有意义，新一轮还没问过任何东西
    asking = ""
    task = state.active_task
    if task and task.flow_id == flow_id:
        step = flow.step(task.step_id)
        if step and step.type == "collect":
            asking = step.slot_name

    spoken = normalize(user_text or "")
    tracking_known = bool(
        find_tracking_number(spoken)
        or plan.slots.get("tracking_number")
        or state.known_slots().get("tracking_number")
    )

    for name in TASK_SCOPED_SLOTS:
        if name not in plan.slots or name == asking:
            continue
        value = normalize(str(plan.slots[name] or ""))
        if value and value in spoken and tracking_known:
            continue
        dropped = str(plan.slots.pop(name))
        notes.append(f"「{name}」不是在回答追问时说的（模型代填「{dropped}」），已丢弃，稍后重新追问")

    return notes


def validate(
    plan: TurnPlan, state: DialogueState, registry: FlowRegistry, user_text: str = ""
) -> tuple[TurnPlan, list[str]]:
    notes: list[str] = []

    if plan.action not in ACTIONS:
        # 模型很常见的串字段：把 flow 名填进了 action。能认出来就顺手修正，
        # 认不出来才走澄清——比直接丢掉一轮对话体验好得多。
        if plan.action in registry.flows:
            notes.append(f"模型把 flow 填进了 action 字段，已修正为 start/{plan.action}")
            plan = TurnPlan(
                "start",
                flow_id=plan.action,
                slots=plan.slots,
                confidence=min(plan.confidence, 0.9),
                reason=plan.reason,
                source=plan.source,
            )
        else:
            notes.append(f"动作 `{plan.action}` 不在允许集合内，改走澄清")
            return TurnPlan("clarify", confidence=0.0, reason=notes[-1], source=plan.source), notes

    # 0. answer 却带着运单号：说明是查这一单，不是问规则
    if plan.action == "answer":
        tracking = find_tracking_number(normalize(user_text or ""))
        if tracking and "shipment_status_query" in registry.flows:
            notes.append(f"answer 却带着运单号 {tracking}，改派运单查询")
            plan = TurnPlan(
                "start",
                flow_id="shipment_status_query",
                slots={**plan.slots, "tracking_number": tracking},
                confidence=min(plan.confidence, 0.9),
                reason=plan.reason,
                source=plan.source,
            )
        # 问的是当前这单的物流字段（运单号/到哪/几天到），状态里有数据可答，
        # 走轨迹流程取最新节点比在规则库里干搜靠谱。
        elif (
            LOGISTICS_ATTR.search(normalize(user_text or ""))
            and not any(w in (user_text or "") for w in ("怎么赔", "赔付", "理赔标准", "理赔单号"))
            and "tracking_query" in registry.flows
            and (
                state.known_slots().get("tracking_number")
                or state.known_slots().get("latest_trace")
            )
        ):
            notes.append("answer 但问的是当前运单的物流字段，改派轨迹查询")
            plan = TurnPlan(
                "start",
                flow_id="tracking_query",
                slots=plan.slots,
                confidence=min(plan.confidence, 0.9),
                reason=plan.reason,
                source=plan.source,
            )

    # 0.5 运单号一致性：模型抄错单号的代价是打到中台 404，必须在这里拦住
    fix = _reconcile_tracking_number(plan, state, user_text)
    if fix:
        notes.append(fix)

    # 0.6 可行性问句不是办事请求："那这单能理赔吗"要走知识轨道，
    # 否则接下来会莫名其妙追问"异常情况是什么"。带请求动词的不在此列。
    if (
        plan.action == "start"
        and plan.flow_id in {"claim_request", "address_change"}
        and not any(w in (user_text or "") for w in ACTION_REQUEST)
        and FEASIBILITY_ASK.search(normalize(user_text or ""))
    ):
        notes.append("用户问的是能不能办，不是要现在办，改走规则问答")
        plan = TurnPlan(
            "answer",
            slots={k: v for k, v in plan.slots.items() if k == "tracking_number"},
            confidence=min(plan.confidence, 0.85),
            reason=plan.reason,
            source=plan.source,
        )

    # 0.7 异常说明不能是"帮我报异常"本身。模型常把用户整句话填进这个槽，
    # 结果说明变成"直接帮我报异常"，还一路提交上去了。
    reason_value = str(plan.slots.get("claim_reason") or "")
    if reason_value and (
        len(reason_value) < 2 or any(w in reason_value for w in INTENT_IN_REASON)
    ):
        plan.slots.pop("claim_reason", None)
        notes.append(f"「{reason_value}」是意图不是情况说明，不作为理赔原因，稍后重新追问")

    # 0.8 任务域自由文本槽位不许模型代填（理赔原因 / 新地址）
    notes += _reconcile_task_scoped_slots(plan, state, registry, user_text)

    # 1. flow 必须真实存在
    if plan.action == "start":
        if not plan.flow_id:
            notes.append("start 没给 flow，改走澄清")
            return TurnPlan("clarify", confidence=0.0, reason=notes[-1], source=plan.source), notes
        flow = registry.flows.get(plan.flow_id)
        if not flow:
            notes.append(f"flow `{plan.flow_id}` 不存在于配置，改走澄清（防幻觉）")
            return TurnPlan("clarify", confidence=0.0, reason=notes[-1], source=plan.source), notes

    # 2. 槽位必须已定义；未定义的直接丢掉，不往状态里写脏数据
    unknown = [name for name in plan.slots if name not in registry.slots]
    if unknown:
        for name in unknown:
            plan.slots.pop(name, None)
        notes.append(f"丢弃未定义槽位：{unknown}")

    # 3. 状态一致性
    if plan.action == "resume":
        if not state.paused_tasks:
            # 取消掉的任务不进挂起列表，此时"继续刚才的理赔"没法恢复。
            # 但用户意图明确，重新发起比反问"你什么意思"有用得多。
            if plan.flow_id and plan.flow_id in registry.flows:
                notes.append(f"没有挂起任务可恢复，改为重新开始 `{plan.flow_id}`")
                plan = TurnPlan(
                    "start",
                    flow_id=plan.flow_id,
                    slots=plan.slots,
                    confidence=min(plan.confidence, 0.8),
                    reason=plan.reason,
                    source=plan.source,
                )
            else:
                notes.append("没有挂起任务却要恢复，改走澄清")
                return TurnPlan("clarify", confidence=0.0, reason=notes[-1], source=plan.source), notes
        elif plan.flow_id and not any(t.flow_id == plan.flow_id for t in state.paused_tasks):
            notes.append(f"`{plan.flow_id}` 不在挂起列表里，改走澄清")
            return TurnPlan("clarify", confidence=0.0, reason=notes[-1], source=plan.source), notes

    if plan.action == "cancel" and not state.active_task:
        notes.append("没有进行中的任务却要取消，按闲聊处理")
        return TurnPlan("chitchat", confidence=0.4, reason=notes[-1], source=plan.source), notes

    # 4. 置信度太低就澄清，别猜
    if plan.action in {"start", "answer"} and plan.confidence < settings.dialogue.clarify_threshold:
        notes.append(f"置信度 {plan.confidence:.2f} 低于阈值，改走澄清")
        return TurnPlan("clarify", confidence=plan.confidence, reason=notes[-1], source=plan.source), notes

    return plan, notes
