"""会话评测的执行器。

与物流研究助手的消融不同，这里没有"证据池回放"的问题——客服的输入就是一句话，
每段脚本都是真实跑完整链路（规划 → 状态机 → 中台）。为了让"理赔已提交"
这类写操作不互相污染，**每段会话前都把中台写表恢复到种子状态**。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.conf.settings import settings
from app.dialogue.engine import DialogueEngine, TurnResult
from app.flows.loader import load as load_flows
from app.infra.repo import StateRepo
from app.tracks.knowledge import load_passages

ROOT = Path(__file__).resolve().parents[1]
COMMERCE_DB = ROOT / "data" / "commerce.db"

# 种子里"已存在的理赔"挂在 YD20260403004 上，用于测 409，不能动
_PROTECTED_CLAIM_SHIPMENT = "YD20260403004"


def reset_commerce() -> None:
    """把中台里会话可能产生的写操作清掉，恢复到种子状态。"""
    with sqlite3.connect(COMMERCE_DB) as db:
        db.execute(
            "DELETE FROM claim_requests WHERE shipment_id IN "
            "(SELECT id FROM shipments WHERE tracking_number != ?)",
            (_PROTECTED_CLAIM_SHIPMENT,),
        )
        db.execute("DELETE FROM address_change_requests")
        db.execute("DELETE FROM delivery_urge_requests")


def load_sessions(path: Path) -> list[dict[str, Any]]:
    sessions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sessions.append(json.loads(line))
    return sessions


@dataclass
class TurnRecord:
    idx: int
    text: str
    action: str
    flow_id: str
    source: str
    confidence: float
    reason: str = ""
    transitions: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)


@dataclass
class SessionRecord:
    id: str
    group: str
    variant: str
    turns: list[TurnRecord] = field(default_factory=list)
    route_hits: int = 0
    route_total: int = 0
    failures: list[str] = field(default_factory=list)
    final_ok: bool = True
    success: bool = False
    handoff: bool = False      # 会话结束时的真实转人工状态位
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def _all_bot_text(records: list[TurnRecord]) -> str:
    return " ".join(t for r in records for t in r.replies)


def _state_slots(engine: DialogueEngine, sender: str) -> dict[str, str]:
    state = engine.repo.get(sender)
    merged = dict(state.slots)
    for task in [state.active_task, *state.paused_tasks]:
        if task:
            merged.update(task.slots)
    return merged


def _check_turn(expect: dict, record: TurnRecord, engine: DialogueEngine, sender: str) -> list[str]:
    problems: list[str] = []
    want_action = expect.get("action")
    if want_action:
        allowed = want_action if isinstance(want_action, list) else [want_action]
        if record.action not in allowed:
            problems.append(f"turn{record.idx}: 期望 action={allowed}，实际 {record.action}")
    want_flow = expect.get("flow")
    if want_flow:
        allowed = want_flow if isinstance(want_flow, list) else [want_flow]
        if record.flow_id not in allowed:
            problems.append(f"turn{record.idx}: 期望 flow={allowed}，实际 {record.flow_id}")
    for paused_flow in expect.get("paused_contains", []):
        # turn 级断言是在整段会话跑完之后才做的，此时"当前挂起列表"可能已经被
        # 后续轮次（resume / 重新提起）清空。所以先看这一轮自己产生的状态变更，
        # 有记录就算过；没有再退回看末态（适用于会话结束时任务仍挂着的场景）。
        if any(t.startswith(f"{paused_flow}:active->paused") for t in record.transitions):
            continue
        state = engine.repo.get(sender)
        if paused_flow not in [t.flow_id for t in state.paused_tasks]:
            problems.append(
                f"turn{record.idx}: 期望 {paused_flow} 在本轮被挂起（transitions 无记录，末态也不在挂起列表）"
            )
    return problems


def _check_final(expect: dict, records: list[TurnRecord], engine: DialogueEngine, sender: str) -> list[str]:
    problems: list[str] = []
    text = _all_bot_text(records)

    # contains_any 是"命中任一即可"。之前这里写成了逐个必含，
    # 导致 s27 这种列了四个同义说法的脚本无论怎么答都判失败——是断言实现的错，不是系统答错。
    needles = expect.get("contains_any") or []
    if needles and not any(n in text for n in needles):
        problems.append(f"final: 回复里没有 {needles} 里的任何一个，实际答：{text[:60]}")

    for needle in expect.get("not_contains", []):
        if needle in text:
            problems.append(f"final: 回复里不该出现「{needle}」却出现了")

    state = engine.repo.get(sender)
    if "slots" in expect:
        actual = _state_slots(engine, sender)
        for key, value in expect["slots"].items():
            if str(actual.get(key) or "") != str(value):
                problems.append(f"final: 槽位 {key} 期望 {value}，实际 {actual.get(key)}")
    if "active_flow" in expect:
        actual = state.active_task.flow_id if state.active_task else None
        if actual != expect["active_flow"]:
            problems.append(f"final: 期望 active={expect['active_flow']}，实际 {actual}")
    for paused_flow in expect.get("paused_contains", []):
        if paused_flow not in [t.flow_id for t in state.paused_tasks]:
            problems.append(f"final: 期望 {paused_flow} 仍在挂起列表")
    if "handoff" in expect:
        if bool(state.handoff) != bool(expect["handoff"]):
            problems.append(f"final: 期望 handoff={expect['handoff']}，实际 {state.handoff}")
    return problems


async def run_session(
    engine: DialogueEngine, session: dict[str, Any], variant: str
) -> SessionRecord:
    sender = f"{session['sender_id']}-ev-{uuid.uuid4().hex[:6]}"
    engine.repo.reset(sender)
    reset_commerce()

    record = SessionRecord(id=session["id"], group=session["group"], variant=variant)
    started = time.perf_counter()
    turn_results: list[TurnResult] = []

    for idx, turn in enumerate(session["turns"], start=1):
        result = await engine.handle(sender, turn.get("text", ""), turn.get("object"))
        turn_results.append(result)
        plan = result.plan
        assert plan is not None
        record.turns.append(
            TurnRecord(
                idx=idx,
                text=turn.get("text", "") or "[点卡片]",
                action=plan.action,
                flow_id=plan.flow_id,
                source=plan.source,
                confidence=plan.confidence,
                reason=plan.reason,
                transitions=result.transitions,
                replies=[m.get("text", "") for m in result.messages if m.get("text")],
            )
        )

    record.elapsed = time.perf_counter() - started

    expect = session.get("expect") or {}
    turn_expects = expect.get("turns") or []
    for i, turn_expect in enumerate(turn_expects):
        if i < len(record.turns):
            problems = _check_turn(turn_expect, record.turns[i], engine, sender)
            record.failures += problems
            if turn_expect.get("action"):
                record.route_total += 1
                if not problems:
                    record.route_hits += 1

    record.handoff = bool(engine.repo.get(sender).handoff)
    final_problems = _check_final(expect.get("final") or {}, record.turns, engine, sender)
    record.failures += final_problems
    record.final_ok = not final_problems
    record.success = record.final_ok and record.route_hits == record.route_total
    return record
