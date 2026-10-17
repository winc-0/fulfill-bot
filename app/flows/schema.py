"""流程 DSL 的数据结构与校验。

设计原则：**配置写错要在加载时炸，不能等到用户说到那一句才炸。**
所以这里把引用完整性（next 指向、slot 定义、动作名）全部前置校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StepType = Literal["start", "collect", "action", "end"]

VALID_ACTIONS = {
    "action_response",     # 渲染一段文本（static / rephrase）
    "action_listen",       # 停下来等用户输入
    "action_lookup_shipment_status",
    "action_lookup_tracking",
    "action_submit_claim",
    "action_change_address",
    "action_urge_delivery",
}


class FlowConfigError(ValueError):
    """配置错误。加载期就该暴露，不能留到运行期。"""


@dataclass(frozen=True)
class SlotDef:
    name: str
    type: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class Branch:
    """一条转移。condition 为空表示无条件转移。"""

    condition: str
    target: str


@dataclass(frozen=True)
class Step:
    id: str
    type: StepType
    next: tuple[Branch, ...] = ()
    slot_name: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    action: str = ""
    args: Any = None


@dataclass(frozen=True)
class Flow:
    id: str
    name: str
    description: str
    steps: dict[str, Step]
    is_system: bool = False

    @property
    def start_step(self) -> Step:
        return self.steps["start"]

    @property
    def required_slots(self) -> tuple[str, ...]:
        return tuple(s.slot_name for s in self.steps.values() if s.type == "collect")

    def step(self, step_id: str) -> Step | None:
        return self.steps.get(step_id)


@dataclass
class FlowRegistry:
    slots: dict[str, SlotDef]
    flows: dict[str, Flow]
    system_flows: dict[str, Flow]

    def get(self, flow_id: str) -> Flow | None:
        return self.flows.get(flow_id) or self.system_flows.get(flow_id)

    def catalogue(self) -> list[dict[str, Any]]:
        """给规划器看的流程清单。只暴露该暴露的字段。"""
        return [
            {
                "id": f.id,
                "name": f.name,
                "description": f.description,
                "required_slots": list(f.required_slots),
            }
            for f in self.flows.values()
        ]


def validate(flows: dict[str, Flow], slots: dict[str, SlotDef]) -> None:
    for flow in flows.values():
        if "start" not in flow.steps:
            raise FlowConfigError(f"flow `{flow.id}` 缺少 start 步骤")
        if not any(s.type == "end" for s in flow.steps.values()):
            raise FlowConfigError(f"flow `{flow.id}` 没有 end 步骤，会走不出去")

        for step in flow.steps.values():
            for branch in step.next:
                if branch.target not in flow.steps:
                    raise FlowConfigError(
                        f"flow `{flow.id}` 的步骤 `{step.id}` 指向不存在的 `{branch.target}`"
                    )
            if step.type == "collect":
                if not step.slot_name:
                    raise FlowConfigError(f"flow `{flow.id}` 的 collect `{step.id}` 没写 slot_name")
                if step.slot_name not in slots:
                    raise FlowConfigError(
                        f"flow `{flow.id}` 的 collect `{step.id}` 用了未定义的槽位 `{step.slot_name}`"
                    )
            if step.type == "action":
                if not step.action:
                    raise FlowConfigError(f"flow `{flow.id}` 的 action `{step.id}` 没写 action")
                if step.action not in VALID_ACTIONS:
                    raise FlowConfigError(
                        f"flow `{flow.id}` 的 action `{step.id}` 用了未注册的动作 `{step.action}`"
                    )
