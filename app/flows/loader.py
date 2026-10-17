"""把 YAML 读成 FlowRegistry。

支持多文件合并：user_flows.yml 放业务，system_flows.yml 放系统应答。
同名 flow 直接报错——静默覆盖是最难查的一类 bug。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.flows.schema import Branch, Flow, FlowConfigError, FlowRegistry, SlotDef, Step, validate


def _parse_next(raw: Any) -> tuple[Branch, ...]:
    if raw is None or raw == [] or raw == "":
        return ()
    if isinstance(raw, str):
        return (Branch(condition="", target=raw),)
    if isinstance(raw, list):
        branches: list[Branch] = []
        for item in raw:
            if isinstance(item, str):
                branches.append(Branch(condition="", target=item))
            elif isinstance(item, dict):
                if "else" in item:
                    branches.append(Branch(condition="", target=str(item["else"])))
                elif "if" in item and "then" in item:
                    branches.append(Branch(condition=str(item["if"]), target=str(item["then"])))
                else:
                    raise FlowConfigError(f"无法识别的转移写法：{item}")
        return tuple(branches)
    raise FlowConfigError(f"next 只能是字符串或列表，收到 {type(raw).__name__}")


def _parse_step(raw: dict[str, Any]) -> Step:
    step_id = raw.get("id")
    if not step_id:
        raise FlowConfigError("步骤缺少 id")
    step_type = raw.get("type")
    if step_type not in {"start", "collect", "action", "end"}:
        raise FlowConfigError(f"步骤 `{step_id}` 的 type 非法：{step_type}")
    return Step(
        id=str(step_id),
        type=step_type,
        next=_parse_next(raw.get("next")),
        slot_name=str(raw.get("slot_name") or ""),
        response=raw.get("response") or {},
        action=str(raw.get("action") or ""),
        args=raw.get("args"),
    )


def _parse_flow(flow_id: str, raw: dict[str, Any], *, is_system: bool) -> Flow:
    steps: dict[str, Step] = {}
    for raw_step in raw.get("steps") or []:
        step = _parse_step(raw_step)
        if step.id in steps:
            raise FlowConfigError(f"flow `{flow_id}` 里步骤 id 重复：`{step.id}`")
        steps[step.id] = step
    return Flow(
        id=flow_id,
        name=str(raw.get("name") or flow_id),
        description=str(raw.get("description") or ""),
        steps=steps,
        is_system=is_system,
    )


def load(flow_dir: Path) -> FlowRegistry:
    if not flow_dir.is_dir():
        raise FlowConfigError(f"流程目录不存在：{flow_dir}")

    slots: dict[str, SlotDef] = {}
    flows: dict[str, Flow] = {}
    system_flows: dict[str, Flow] = {}

    for path in sorted(flow_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        is_system = path.name.startswith("system")

        for name, raw in (data.get("slots") or {}).items():
            if name in slots:
                raise FlowConfigError(f"槽位 `{name}` 重复定义（{path.name}）")
            slots[name] = SlotDef(
                name=name,
                type=str(raw.get("type") or "text"),
                label=str(raw.get("label") or name),
                description=str(raw.get("description") or ""),
            )

        bucket = system_flows if is_system else flows
        for flow_id, raw in (data.get("flows") or {}).items():
            if flow_id in flows or flow_id in system_flows:
                raise FlowConfigError(f"flow `{flow_id}` 重复定义（{path.name}）")
            bucket[flow_id] = _parse_flow(flow_id, raw, is_system=is_system)

    validate(flows, slots)
    validate(system_flows, slots)
    return FlowRegistry(slots=slots, flows=flows, system_flows=system_flows)
