"""动作注册表。

流程 DSL 里写的 `action: action_lookup_logistics` 就是这里的键。
新增一种能力 = 写一个 handler + 在 schema.VALID_ACTIONS 里登记，
YAML 那边就能直接用——这是"配置驱动"能成立的前提。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.dialogue.state import DialogueState
from app.flows.schema import Flow, Step


@dataclass
class ActionContext:
    state: DialogueState
    flow: Flow
    step: Step
    slots: dict[str, str]
    context: dict[str, Any]
    user_text: str
    sender_id: str

    @property
    def flow_name(self) -> str:
        return self.flow.name


@dataclass
class ActionResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    slots: dict[str, str] = field(default_factory=dict)
    ok: bool = True
    # 动作失败时：给用户一句实话，然后结束这个任务，不继续念后面的模板
    abort: bool = False
    handoff: bool = False


Handler = Callable[[ActionContext], Awaitable[ActionResult]]
_REGISTRY: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    def wrapper(fn: Handler) -> Handler:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"动作 {name} 必须是 async 函数")
        _REGISTRY[name] = fn
        return fn

    return wrapper


def get(name: str) -> Handler | None:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)
