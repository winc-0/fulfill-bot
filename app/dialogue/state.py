"""对话状态。

三样东西决定"这句话该接哪"：
- active_task   正在办的事，卡在哪一步、已经填了哪些槽
- paused_tasks  被打断挂起的事，恢复时要从原断点接着跑，而不是从头问
- focused_object 用户当前盯着哪张运单（点了卡片，或者说"这个"）

再叠一个全局 slots：同一轮对话里说过的运单号算"已知"，后面用户说"它的轨迹呢"
就不该再问一遍运单号。这就是单轮系统做不到的指代消解。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# 只对"当前这件事"成立的槽位：不参与跨任务的自动补槽
TASK_SCOPED_SLOTS = {"claim_reason", "new_address"}


@dataclass
class TaskInstance:
    flow_id: str
    step_id: str
    slots: dict[str, str] = field(default_factory=dict)
    turn_started: int = 0
    status: str = "active"  # active | paused | done | canceled
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "step_id": self.step_id,
            "slots": dict(self.slots),
            "turn_started": self.turn_started,
            "status": self.status,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskInstance":
        return cls(
            flow_id=str(raw.get("flow_id") or ""),
            step_id=str(raw.get("step_id") or "start"),
            slots={k: str(v) for k, v in (raw.get("slots") or {}).items()},
            turn_started=int(raw.get("turn_started") or 0),
            status=str(raw.get("status") or "active"),
            reason=str(raw.get("reason") or ""),
        )


@dataclass
class DialogueState:
    sender_id: str
    turn_index: int = 0
    active_task: TaskInstance | None = None
    paused_tasks: list[TaskInstance] = field(default_factory=list)
    focused_object: dict[str, Any] | None = None
    slots: dict[str, str] = field(default_factory=dict)
    # 上一轮没听懂时留个底：用户说"不是这个意思"时能判断是澄清被拒
    pending_clarify: dict[str, Any] | None = None
    handoff: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ---------- 便捷访问 ----------
    def known_slots(self) -> dict[str, str]:
        """合并"任务内槽位 + 全局槽位 + 聚焦对象"，用于自动补槽。

        但有例外：像"异常说明""新地址"这种只对当前这件事成立的槽位，不跟着全局走。
        否则用户下次给另一张运单报异常，会被上一单的情况说明悄悄顶掉——这类串场 bug
        在演示里很难复现，上线后却天天发生。
        """
        merged: dict[str, str] = {
            k: v for k, v in self.slots.items() if k not in TASK_SCOPED_SLOTS
        }
        if self.focused_object:
            if self.focused_object.get("tracking_number"):
                merged.setdefault("tracking_number", str(self.focused_object["tracking_number"]))
        if self.active_task:
            for key, value in self.active_task.slots.items():
                merged[key] = value
        return merged

    def pause_active(self, max_paused: int) -> TaskInstance | None:
        if not self.active_task:
            return None
        task = self.active_task
        task.status = "paused"
        self.paused_tasks = [t for t in self.paused_tasks if t.flow_id != task.flow_id]
        self.paused_tasks.append(task)
        # 只留最近几条，避免长会话无限膨胀
        self.paused_tasks = self.paused_tasks[-max_paused:]
        self.active_task = None
        return task

    def pop_paused(self, flow_id: str | None = None) -> TaskInstance | None:
        if not self.paused_tasks:
            return None
        if flow_id:
            for idx, task in enumerate(self.paused_tasks):
                if task.flow_id == flow_id:
                    return self.paused_tasks.pop(idx)
            return None
        return self.paused_tasks.pop()

    def push_message(self, role: str, payload: dict[str, Any]) -> None:
        self.messages.append({"role": role, **payload})
        if len(self.messages) > 200:
            self.messages = self.messages[-200:]

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "sender_id": self.sender_id,
            "turn_index": self.turn_index,
            "active_task": self.active_task.to_dict() if self.active_task else None,
            "paused_tasks": [t.to_dict() for t in self.paused_tasks],
            "focused_object": self.focused_object,
            "slots": dict(self.slots),
            "pending_clarify": self.pending_clarify,
            "handoff": self.handoff,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DialogueState":
        return cls(
            sender_id=str(raw.get("sender_id") or ""),
            turn_index=int(raw.get("turn_index") or 0),
            active_task=TaskInstance.from_dict(raw["active_task"]) if raw.get("active_task") else None,
            paused_tasks=[TaskInstance.from_dict(t) for t in (raw.get("paused_tasks") or [])],
            focused_object=raw.get("focused_object"),
            slots={k: str(v) for k, v in (raw.get("slots") or {}).items()},
            pending_clarify=raw.get("pending_clarify"),
            handoff=bool(raw.get("handoff")),
            messages=list(raw.get("messages") or []),
        )

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
