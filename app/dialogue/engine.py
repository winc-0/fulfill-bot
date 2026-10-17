"""对话引擎：把"一句话"变成"状态怎么变、回什么"。

一次处理分四步：
1. 规划：这句话想干什么（LLM，失败回落规则）
2. 校验：flow / 槽位 / 状态是否自洽（防幻觉）
3. 调度：按动作改状态（开始 / 中断 / 挂起 / 恢复 / 取消）
4. 推进：从断点执行流程步骤，直到需要用户输入或流程结束

**所有状态变更都发生在这一层**，动作和轨道都是无状态的纯函数——
这样"中断后恢复"这种事只需要读一个 step_id，不用到处找散落的上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.actions import business, respond  # noqa: F401  触发动作注册
from app.actions.registry import ActionContext, get as get_action
from app.conf.settings import settings
from app.core.log import get_logger
from app.core.text import eval_condition, render
from app.dialogue.planner import Planner, TurnPlan
from app.dialogue.state import DialogueState, TaskInstance
from app.dialogue.validator import validate
from app.flows.schema import Flow, FlowRegistry, Step
from app.infra.repo import StateRepo
from app.tracks import chitchat, knowledge

logger = get_logger("engine")

MAX_STEPS = 40  # 单轮最多推进多少步，防止配置写出环时把进程卡死


@dataclass
class TurnResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: TurnPlan | None = None
    notes: list[str] = field(default_factory=list)
    state: DialogueState | None = None
    transitions: list[str] = field(default_factory=list)


class DialogueEngine:
    def __init__(self, registry: FlowRegistry, repo: StateRepo, passages: list[knowledge.Passage]):
        self.registry = registry
        self.repo = repo
        self.passages = passages
        self.planner = Planner()

    # ================= 对外入口 =================
    async def handle(
        self, sender_id: str, text: str = "", obj: dict[str, Any] | None = None
    ) -> TurnResult:
        state = self.repo.get(sender_id)
        state.turn_index += 1
        result = TurnResult(state=state)

        if obj:
            state.focused_object = obj
            state.push_message("user", {"object": obj})
            text = text or str(obj.get("title") or "")
        else:
            state.push_message("user", {"text": text})

        plan = self._plan_from_object(obj) if obj else await self.planner.plan(
            text, state, self.registry
        )
        plan, notes = validate(plan, state, self.registry, text)
        result.plan, result.notes = plan, notes
        logger.info(
            f"[turn {state.turn_index}] {plan.action}"
            f"{'/' + plan.flow_id if plan.flow_id else ''} conf={plan.confidence:.2f} "
            f"src={plan.source} | {text[:40]}"
        )

        messages = await self._dispatch(plan, state, text, result)
        for message in messages:
            state.push_message("bot", message)
        result.messages = messages

        self.repo.save(state)
        return result

    # ================= 规划与调度 =================
    def _plan_from_object(self, obj: dict[str, Any]) -> TurnPlan:
        """用户点了对象卡片：意图是确定的，不用模型猜。"""
        if obj.get("type") == "shipment":
            return TurnPlan("start", "shipment_status_query",
                            {"tracking_number": str(obj.get("tracking_number") or "")},
                            confidence=1.0, reason="点击运单卡片", source="object")
        # 未知卡片类型不硬猜：宁可让规划器重新判断，也别带着空单号去查
        return TurnPlan("clarify", confidence=0.0, reason="未知的对象卡片类型", source="object")

    async def _dispatch(
        self, plan: TurnPlan, state: DialogueState, text: str, result: TurnResult
    ) -> list[dict[str, Any]]:
        if plan.action == "cancel":
            return await self._cancel(state, result)
        if plan.action == "resume":
            return await self._resume(state, plan, result)
        if plan.action == "handoff":
            return await self._handoff(state, result)
        if plan.action == "answer":
            return await self._answer(state, text)
        if plan.action == "chitchat":
            return [{"text": await chitchat.reply(text)}]
        if plan.action == "clarify":
            return await self._cannot_handle(state, "clarification_rejected" if state.pending_clarify else "")
        return await self._start(state, plan, result)

    # ---------- 取消 ----------
    async def _cancel(self, state: DialogueState, result: TurnResult) -> list[dict[str, Any]]:
        task = state.active_task
        if not task:
            return [{"text": "现在没有正在办的事，你要取消什么？"}]
        flow = self.registry.get(task.flow_id)
        state.active_task = None
        state.pending_clarify = None
        result.transitions.append(f"{task.flow_id}:active->canceled")
        return await self._run_system(state, "system_task_canceled",
                                      {"canceled_flow_name": flow.name if flow else task.flow_id})

    # ---------- 恢复 ----------
    async def _resume(
        self, state: DialogueState, plan: TurnPlan, result: TurnResult
    ) -> list[dict[str, Any]]:
        task = state.pop_paused(plan.flow_id or None)
        if not task:
            return await self._cannot_handle(state, "")

        # 恢复时如果已经有别的事在做，先把它挂起来——恢复不等于抢占
        if state.active_task:
            state.pause_active(settings.dialogue.max_paused)
        task.status = "active"
        state.active_task = task
        flow = self.registry.get(task.flow_id)
        if not flow:
            state.active_task = None
            return await self._cannot_handle(state, "not_supported")

        result.transitions.append(f"{task.flow_id}:paused->active（从 {task.step_id} 续跑）")
        messages = await self._run_system(
            state, "system_task_resumed", {"resumed_flow_name": flow.name}
        )
        messages += await self._run_flow(state, flow, task, result)
        return messages

    # ---------- 转人工 ----------
    async def _handoff(self, state: DialogueState, result: TurnResult) -> list[dict[str, Any]]:
        flow = self.registry.flows.get("human_handoff")
        if not flow:
            return [{"text": "好的，我帮你转人工客服。"}]
        if state.active_task:
            state.pause_active(settings.dialogue.max_paused)
            result.transitions.append(f"{state.paused_tasks[-1].flow_id}:active->paused")
        state.handoff = True
        task = TaskInstance(flow_id=flow.id, step_id="start", turn_started=state.turn_index)
        state.active_task = task
        return await self._run_flow(state, flow, task, result)

    # ---------- 知识轨道 ----------
    async def _answer(self, state: DialogueState, text: str) -> list[dict[str, Any]]:
        # 指代消解：用户只说"这个能退吗"时，把当前聚焦对象的信息补进检索查询
        context_bits: list[str] = []
        if state.focused_object:
            context_bits.append(str(state.focused_object.get("title") or ""))
            if state.focused_object.get("status"):
                context_bits.append(str(state.focused_object["status"]))
        if state.slots.get("shipment_status"):
            context_bits.append(str(state.slots["shipment_status"]))
        if state.active_task:
            flow = self.registry.get(state.active_task.flow_id)
            if flow:
                context_bits.append(flow.name)

        reply_text, hits, found = await knowledge.answer(text, self.passages, " ".join(context_bits))
        state.pending_clarify = None
        if not found:
            # 有聚焦运单时，别问"你是什么意思"，直接给两条明确的岔路
            if state.focused_object and state.focused_object.get("type") == "shipment":
                tracking_number = state.focused_object.get("tracking_number", "")
                return [
                    {
                        "text": f"你是想了解异常件赔付规则，还是直接给运单 {tracking_number} 提交理赔申请？",
                        "suggestions": ["直接帮我报异常", "先说说怎么赔"],
                    }
                ]
            return await self._cannot_handle(state, "no_relevant_answer")
        logger.info(f"[knowledge] 命中 {hits[0].title}（{hits[0].score}）")
        return [{"text": reply_text}]

    # ---------- 澄清 / 兜底 ----------
    async def _cannot_handle(self, state: DialogueState, reason: str) -> list[dict[str, Any]]:
        context = {"reason": reason or "not_supported"}
        messages = await self._run_system(state, "system_cannot_handle", context)
        # 记一笔：下一轮用户如果说"不是这个意思"，能识别成"澄清被拒"
        state.pending_clarify = {"turn": state.turn_index, "reason": context["reason"]}
        return messages

    # ---------- 开始 / 继续业务任务 ----------
    async def _start(
        self, state: DialogueState, plan: TurnPlan, result: TurnResult
    ) -> list[dict[str, Any]]:
        flow = self.registry.flows.get(plan.flow_id)
        if not flow:
            return await self._cannot_handle(state, "not_supported")

        messages: list[dict[str, Any]] = []

        if state.active_task and state.active_task.flow_id == flow.id:
            # 同一件事的后续一句（多半是在回答追问），原地推进即可
            task = state.active_task
            result.transitions.append(f"{flow.id}:active->active（补槽）")
        elif state.active_task:
            # 换任务：旧的挂起，并按 system_task_interrupted 给用户一句交代
            old = state.active_task
            old_flow = self.registry.get(old.flow_id)
            state.pause_active(settings.dialogue.max_paused)
            result.transitions.append(f"{old.flow_id}:active->paused（被 {flow.id} 打断）")
            messages += await self._run_system(
                state,
                "system_task_interrupted",
                {
                    "interrupted_flow_name": old_flow.name if old_flow else old.flow_id,
                    "started_flow_name": flow.name,
                },
            )
            task = TaskInstance(flow_id=flow.id, step_id="start", turn_started=state.turn_index)
            state.active_task = task
        else:
            # 挂起列表里已有同一件事：接着它往下跑，不要重新问一遍
            revived = state.pop_paused(flow.id)
            if revived:
                task = revived
                task.status = "active"
                state.active_task = task
                result.transitions.append(f"{flow.id}:paused->active（用户重新提起，从 {task.step_id} 续跑）")
            else:
                task = TaskInstance(flow_id=flow.id, step_id="start", turn_started=state.turn_index)
                state.active_task = task
                result.transitions.append(f"{flow.id}:new->active")

        # 槽位必须写进「这一轮要跑的任务」，不能写进刚被挂起的那个。
        # 真实踩过：理赔办到一半去查轨迹，轨迹的运单号被写进了挂起的理赔任务，
        # 回来接着办时赔的是另一单。顺序必须是「先定任务归属，再落槽位」。
        for name, value in plan.slots.items():
            state.slots[name] = value
            task.slots.setdefault(name, value)

        messages += await self._run_flow(state, flow, task, result)
        return messages

    # ================= 流程推进 =================
    async def _run_flow(
        self, state: DialogueState, flow: Flow, task: TaskInstance, result: TurnResult
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        step = flow.step(task.step_id) or flow.start_step

        for _ in range(MAX_STEPS):
            if step.type == "start":
                nxt = self._next_step(flow, step, state, task, {})
                if not nxt:
                    task.status, state.active_task = "done", None
                    return messages
                task.step_id, step = nxt.id, nxt
                continue

            if step.type == "collect":
                known = {**state.known_slots(), **task.slots}
                value = str(known.get(step.slot_name) or "")
                if value:
                    task.slots[step.slot_name] = value
                    state.slots.setdefault(step.slot_name, value)
                    nxt = self._next_step(flow, step, state, task, task.slots)
                    if not nxt:
                        task.status, state.active_task = "done", None
                        return messages
                    task.step_id, step = nxt.id, nxt
                    continue

                # 缺这个槽：念追问，停在原地等下一轮
                text = render(str(step.response.get("text") or ""), {**state.slots, **task.slots}, {})
                payload: dict[str, Any] = {"text": text}
                if step.response.get("suggestions"):
                    payload["suggestions"] = step.response["suggestions"]
                messages.append(payload)
                task.step_id = step.id
                return messages

            if step.type == "action":
                ctx = ActionContext(
                    state=state,
                    flow=flow,
                    step=step,
                    slots={**state.known_slots(), **task.slots},
                    context={},
                    user_text="",
                    sender_id=state.sender_id,
                )
                handler = get_action(step.action)
                if handler is None:
                    logger.error(f"[engine] 动作 {step.action} 未注册")
                    task.status, state.active_task = "failed", None
                    return messages + [{"text": "这个操作暂时用不了，我帮你转人工处理。"}]

                outcome = await handler(ctx)
                messages += outcome.messages
                task.slots.update({k: v for k, v in outcome.slots.items() if v})
                for key, value in outcome.slots.items():
                    if value:
                        state.slots.setdefault(key, value)
                if outcome.handoff:
                    state.handoff = True

                if outcome.abort:
                    # 动作失败：说清原因、结束任务，不继续念后面的模板
                    task.status, state.active_task = "failed", None
                    result.transitions.append(f"{flow.id}:active->failed（{step.action} 失败）")
                    return messages

                nxt = self._next_step(flow, step, state, task, task.slots)
                if not nxt:
                    task.status, state.active_task = "done", None
                    result.transitions.append(f"{flow.id}:active->done")
                    return messages
                task.step_id, step = nxt.id, nxt
                continue

            # end
            task.status, state.active_task = "done", None
            result.transitions.append(f"{flow.id}:active->done")
            return messages

        logger.error(f"[engine] {flow.id} 超过 {MAX_STEPS} 步仍未结束，疑似配置成环")
        task.status, state.active_task = "failed", None
        return messages + [{"text": "这个问题我处理得不太顺，帮你转人工继续。"}]

    @staticmethod
    def _next_step(
        flow: Flow,
        step: Step,
        state: DialogueState,
        task: TaskInstance,
        slots: dict[str, str],
        context: dict[str, Any] | None = None,
    ) -> Step | None:
        if not step.next:
            return None
        merged = {**state.known_slots(), **task.slots, **slots}
        for branch in step.next:
            if not branch.condition:
                return flow.step(branch.target)
            # 条件和槽位是两个命名空间：系统流程靠 context 分支（比如"为什么无法处理"），
            # 早先把 context 混进 slots 传下来，导致 context.get(...) 永远取不到值。
            if eval_condition(branch.condition, merged, context or {}):
                return flow.step(branch.target)
        return None

    async def _run_system(
        self, state: DialogueState, flow_id: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """跑一段系统应答流程（只为说一句话），不改动 active_task。"""
        flow = self.registry.system_flows.get(flow_id)
        if not flow:
            return []
        messages: list[dict[str, Any]] = []
        step = flow.start_step
        for _ in range(MAX_STEPS):
            if step.type == "end":
                break
            if step.type in {"start", "collect"}:
                nxt = self._next_step(
                    flow, step, state, TaskInstance(flow.id, step.id), {}, context
                )
                if not nxt:
                    break
                step = nxt
                continue
            handler = get_action(step.action)
            if handler is None:
                break
            ctx = ActionContext(
                state=state,
                flow=flow,
                step=step,
                slots={**state.known_slots()},
                context=context,
                user_text="",
                sender_id=state.sender_id,
            )
            outcome = await handler(ctx)
            messages += outcome.messages
            nxt = self._next_step(
                flow, step, state, TaskInstance(flow.id, step.id), {}, context
            )
            if not nxt:
                break
            step = nxt
        return messages
