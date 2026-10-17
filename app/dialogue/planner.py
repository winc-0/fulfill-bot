"""理解用户这一句想干什么。

两条路：
1. LLM 规划器——把流程清单和当前状态一起喂进去，要结构化输出；
2. 规则规划器——没有 API key、模型超时、输出解析失败时兜底。

兜底不是"降级到不行"，而是**保证仍能完成主流程**：运单号能抽、常见意图能认、
正在问什么就把回答当什么。这条路径同时是评测里的基线。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from app.conf.settings import settings
from app.core.jsonx import extract_json
from app.core.log import get_logger
from app.core.text import find_tracking_number, normalize
from app.dialogue.state import DialogueState
from app.flows.schema import FlowRegistry

logger = get_logger("planner")

ACTIONS = ("start", "cancel", "resume", "answer", "handoff", "chitchat", "clarify")


@dataclass
class TurnPlan:
    action: str
    flow_id: str = ""
    slots: dict[str, str] = field(default_factory=dict)
    confidence: float = 1.0
    reason: str = ""
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "flow_id": self.flow_id,
            "slots": self.slots,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
        }


SYSTEM_PROMPT = """你是仓配履约客服系统的"本轮意图规划器"。你不负责说话，只负责判断用户这一句要做什么，并输出 JSON。

`action` 字段**只能取下面 7 个英文单词之一**，不许填流程名：
- start   开始或继续一个业务流程（此时才在 `flow` 字段里填流程 id）
- cancel  用户想放弃当前正在办的事
- resume  用户想回到之前被打断、挂起的事
- answer  询问服务规则、赔付标准、时效等知识型问题（不需要操作运单）
- chitchat 寒暄、感谢、与业务无关的闲聊
- handoff 用户明确要人工，或情绪激烈、要求投诉
- clarify 实在拿不准

输出格式（只输出 JSON，不要解释）：
{"action": "start", "flow": "claim_request", "slots": {"tracking_number": "YD20260410001"}, "confidence": 0.0-1.0, "reason": "一句话理由"}

注意 `flow` 只是 start 时的附属字段，取值来自下方"业务流程清单"的 id。

规则（务必遵守）：
1. flow 只能从给定清单里选，不允许编造。
2. 运单号只能来自"用户这句话"或"已知信息"，**绝对不许自己编**。
   如果办这件事必须要运单号但用户没给，也要照常选 flow，把 slots 留空，系统会去问。
3. 用户在回答上一轮的追问时（比如系统刚问"请告诉我运单号"），就把这句当作答案，选它对应的 flow 并填槽。
4. 用户说"取消/算了/不用了/先不办"→ cancel；说"继续/接着刚才/回到前面"→ resume。
5. 只是问"能不能""怎么赔""多久""多少钱"这类规则问题 → answer，不要选业务 flow。
6. 拿不准就用 clarify（confidence 给低值），不要硬猜。
7. **只要用户提到了具体的运单号并要求查询或办理，就是业务 flow，不是 answer**：
   - "YD20260405003 现在什么情况" → start / shipment_status_query
   - "包裹破损了怎么赔"、"延误多久能赔" → answer（没有任何运单号，纯规则问题）
8. 追问**当前这一单**的字段，即使不含单号也是业务 flow，因为答案在运单数据里：
   - 刚查完运单又问"运单号是多少""到哪了""什么时候能到" → start / tracking_query
9. 已经出现过的运单号原样沿用，**不许改写、缩写或补零**（抄错一位就会查不到单）。
10. 报异常（破损/丢失/延误）要办事 → start / claim_request；
    问"破损怎么赔""赔多少"是问规则 → answer。"""


class LLMPlanner:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    @property
    def available(self) -> bool:
        return settings.llm.enabled

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=settings.llm.api_key,
                base_url=settings.llm.base_url,
                timeout=settings.llm.timeout,
            )
        return self._client

    def _messages(self, text: str, state: DialogueState, registry: FlowRegistry) -> list[dict]:
        known = state.known_slots()
        context = {
            "业务流程清单": registry.catalogue(),
            "已知信息": {
                "用户ID": state.sender_id,
                "已知槽位": {k: v for k, v in known.items() if k in registry.slots},
                "用户正在看的对象": state.focused_object or None,
            },
            "当前任务": (
                {
                    "flow": state.active_task.flow_id,
                    "停在步骤": state.active_task.step_id,
                    "已填槽位": state.active_task.slots,
                }
                if state.active_task
                else None
            ),
            "挂起的任务": [
                {"flow": t.flow_id, "停在步骤": t.step_id, "已填槽位": t.slots}
                for t in state.paused_tasks
            ],
            "最近对话": state.messages[-settings.dialogue.history_window :],
            "用户这句话": normalize(text),
        }
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]

    async def plan(self, text: str, state: DialogueState, registry: FlowRegistry) -> TurnPlan:
        resp = await self.client.chat.completions.create(
            model=settings.llm.planner_model,
            messages=self._messages(text, state, registry),
            temperature=settings.llm.temperature,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = extract_json(raw)
        if not data:
            raise ValueError(f"规划器输出无法解析：{raw[:120]}")
        return TurnPlan(
            action=str(data.get("action") or "clarify"),
            flow_id=str(data.get("flow") or data.get("flow_id") or ""),
            slots={k: str(v) for k, v in (data.get("slots") or {}).items() if v not in (None, "")},
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
            source="llm",
        )


KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("human_handoff", ("转人工", "人工客服", "找人工", "投诉", "要人工")),
    ("claim_request", ("破损", "坏了", "丢了", "丢失", "少了", "延误", "理赔", "报异常", "赔付")),
    ("address_change", ("改地址", "改派送地址", "换个地址", "地址改", "改送到")),
    # 轨迹排在催派前面：否则"查一下派送到哪了"会被"派送"抢走
    ("tracking_query", ("轨迹", "快递", "到哪", "什么时候到", "配送", "签收", "物流")),
    ("delivery_reminder", ("催派", "催一下", "催", "快点送", "还没送", "尽快送", "派送")),
    # 只收"怎么样了"：单收"怎么样"会把"今天天气怎么样"也当成查运单
    ("shipment_status_query", ("运单", "状态", "进度", "情况", "怎么样了")),
]

CANCEL_WORDS = ("取消", "算了", "不办了", "不用了", "先不", "别办了")
RESUME_WORDS = ("继续", "接着", "回到", "刚才说", "我们刚才")
REASON_PATTERN = re.compile(r"(?:原因|因为)\s*(?:是|为)?\s*[:：]?\s*([^，。,.!！]{2,20})")

# 带疑问词、又不带单号 → 问的是规则而不是某一单。没有模型时只能靠这个区分
# "理赔多久到账"（问规则）和"我要报异常"（要办事）。
# 注意别用单字"怎么"：它会把"今天天气怎么样"也当成政策问句
POLICY_WORDS = ("吗", "谁", "多久", "多长时间", "怎么处理", "怎么办", "怎么弄",
                "如何", "是否", "几天", "多少钱",
                # 物流场景特有：带这些词多半在问赔付标准，不是要现在报案
                "怎么赔", "赔多少", "赔付", "能不能", "可不可以", "哪些东西", "怎么算")
# 出现这些词说明用户在提要求，不是在提问——别把"能不能帮我报个异常"当规则问。
ACTION_WORDS = ("帮我", "我要", "给我", "麻烦", "请帮")


class RulePlanner:
    """不依赖模型的下限保障。故意做得简单直接，好解释也好测。"""

    def plan(self, text: str, state: DialogueState, registry: FlowRegistry) -> TurnPlan:
        norm = normalize(text)
        tracking = find_tracking_number(norm)
        slots: dict[str, str] = {"tracking_number": tracking} if tracking else {}

        if any(w in norm for w in CANCEL_WORDS):
            return TurnPlan("cancel", confidence=0.8, reason="命中取消词", source="rule")

        if any(w in norm for w in RESUME_WORDS) and state.paused_tasks:
            target = state.paused_tasks[-1].flow_id
            return TurnPlan("resume", flow_id=target, slots=slots, confidence=0.7,
                            reason="命中恢复词且存在挂起任务", source="rule")

        # "报异常，原因是箱子压坏了" 这种一句话带说明的说法，顺手抽出来
        reason_match = REASON_PATTERN.search(norm)
        if reason_match:
            slots["claim_reason"] = reason_match.group(1).strip(" ，。,.!！")

        # "破损怎么赔""延误多久能赔"是问规则，不是要办事：
        # 没有运单号 + 有疑问词 + 没有请求动作 → 走知识轨道
        if (
            not tracking
            and any(w in norm for w in POLICY_WORDS)
            and not any(w in norm for w in ACTION_WORDS)
        ):
            return TurnPlan("answer", confidence=0.6, reason="疑问句且没有运单号，按规则问答处理",
                            source="rule")

        for flow_id, words in KEYWORDS:
            if not any(w in norm for w in words):
                continue
            if flow_id == "human_handoff":
                return TurnPlan("handoff", confidence=0.85, reason="命中转人工词", source="rule")

            # 命中了关键词、但用户此刻正在同一个流程里被追问（比如刚问完
            # "请说一下异常情况"，用户回"箱子压坏了"）——这句话是**答案**，不是又一次
            # "报异常"。不这么判，槽位永远填不上，流程会一直重复追问同一句。
            active = state.active_task
            if active and active.flow_id == flow_id:
                flow = registry.get(flow_id)
                step = flow.step(active.step_id) if flow else None
                if (
                    step
                    and step.type == "collect"
                    and step.slot_name != "tracking_number"   # 运单号只认正则抽出来的，不认整句
                    and 0 < len(norm) <= 60
                ):
                    if flow_id != "claim_request":
                        slots.pop("claim_reason", None)
                    slots[step.slot_name] = norm
                    return TurnPlan(
                        "start", flow_id=flow_id, slots=slots, confidence=0.75,
                        reason=f"命中关键词但正在追问 {step.slot_name}，整句作为槽位值",
                        source="rule",
                    )

            if flow_id != "claim_request":
                slots.pop("claim_reason", None)
            return TurnPlan("start", flow_id=flow_id, slots=slots, confidence=0.7,
                            reason=f"命中关键词 {words}", source="rule")

        # 用户正在回答某个追问：把这句话当作当前 collect 槽的值
        if state.active_task and registry.get(state.active_task.flow_id):
            flow = registry.get(state.active_task.flow_id)
            step = flow.step(state.active_task.step_id) if flow else None
            if step and step.type == "collect" and norm and len(norm) <= 60:
                slots = {step.slot_name: norm}
                return TurnPlan("start", flow_id=state.active_task.flow_id, slots=slots,
                                confidence=0.6, reason="按追问顺序取槽位值", source="rule")

        # 只给了一个运单号、又没在回答追问（"查一下 YD20260410001"）：
        # 没有任何流程线索时，默认就是查这一单。这一步必须排在"按追问取槽"之后，
        # 否则"报理赔时用户只回一句运单号"会被误判成查询。
        if tracking:
            return TurnPlan("start", flow_id="shipment_status_query", slots=slots, confidence=0.65,
                            reason="句子里只有运单号，按运单状态查询处理", source="rule")

        if len(norm) <= 20 and any(w in norm for w in ("你好", "在吗", "谢谢", "好的", "嗯")):
            return TurnPlan("chitchat", confidence=0.7, reason="寒暄", source="rule")

        return TurnPlan("clarify", confidence=0.3, reason="规则未命中", source="rule")


class Planner:
    """LLM 优先，失败即回落规则；两条都记来源，便于事后分析。"""

    def __init__(self) -> None:
        self.llm = LLMPlanner()
        self.rule = RulePlanner()

    async def plan(self, text: str, state: DialogueState, registry: FlowRegistry) -> TurnPlan:
        if not self.llm.available:
            plan = self.rule.plan(text, state, registry)
            plan.reason = f"{plan.reason}（未配置模型，走规则）"
            return plan

        try:
            plan = await self.llm.plan(text, state, registry)
        except Exception as exc:  # 超时、解析失败、限流都在这里回落
            logger.warning(f"[planner] LLM 失败，回落规则：{type(exc).__name__} {str(exc)[:80]}")
            plan = self.rule.plan(text, state, registry)
            plan.reason = f"{plan.reason}（模型失败回落）"
            return plan

        # 模型没抽到运单号但用户明明写了，补上——正则比模型稳
        if not plan.slots.get("tracking_number"):
            tracking = find_tracking_number(normalize(text))
            if tracking:
                plan.slots["tracking_number"] = tracking
        return plan
