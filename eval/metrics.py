"""把逐会话记录汇成指标。

四个数，按面试时被追问的概率排序：
1. 会话完成率——端到端把事办成（或按预期失败）的比例
2. 轨道路由准确率——每一跳的意图判断对不对
3. 中断恢复成功率——这是本项目的核心卖点，单独拎出来
4. 误转人工率——把不该转的转了，等于把用户扔出去
"""

from __future__ import annotations

from typing import Any

from eval.runner import SessionRecord

GROUP_NAMES = {
    "single_turn": "单轮查询",
    "slot_filling": "多轮补槽",
    "coreference": "指代消解",
    "interrupt_resume": "中断恢复",
    "cancel": "中途取消",
    "business_failure": "业务失败",
    "policy": "规则问答",
    "handoff": "转人工",
    "clarify": "澄清兜底",
    "object": "对象卡片",
    "delivery": "催派送",
}


def aggregate(records: list[SessionRecord]) -> dict[str, Any]:
    total = len(records)
    success = sum(1 for r in records if r.success)
    route_hits = sum(r.route_hits for r in records)
    route_total = sum(r.route_total for r in records)
    elapsed = sum(r.elapsed for r in records)
    turns = sum(len(r.turns) for r in records)

    # 误转人工：非转人工分组的会话，结束状态却置了转人工位。
    # 按状态位判定而不是按回复文本——模型会把兜底话术改写成"帮你转接人工"，
    # 那只是话术，真转没转要看状态。
    handoff_fp = 0
    handoff_fp_total = 0
    for r in records:
        if r.group != "handoff":
            handoff_fp_total += 1
            if r.handoff:
                handoff_fp += 1

    # 规划器失败回落（仅 LLM 变体有意义）
    llm_turns = sum(
        1 for r in records for t in r.turns if t.source == "llm"
    )
    rule_turns = sum(1 for r in records for t in r.turns if t.source == "rule")

    by_group: dict[str, dict[str, Any]] = {}
    for r in records:
        g = by_group.setdefault(
            r.group, {"total": 0, "success": 0, "route_hits": 0, "route_total": 0}
        )
        g["total"] += 1
        g["success"] += 1 if r.success else 0
        g["route_hits"] += r.route_hits
        g["route_total"] += r.route_total

    return {
        "sessions": total,
        "session_success": success,
        "session_rate": success / total if total else 0,
        "route_hits": route_hits,
        "route_total": route_total,
        "route_rate": route_hits / route_total if route_total else 0,
        "turns": turns,
        "avg_turn_elapsed": elapsed / turns if turns else 0,
        "handoff_fp": handoff_fp,
        "handoff_fp_total": handoff_fp_total,
        "llm_turns": llm_turns,
        "rule_turns": rule_turns,
        "by_group": by_group,
    }


def _joined(r: SessionRecord) -> str:
    return " ".join(t for turn in r.turns for t in turn.replies)


def comparison_table(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| 指标 | " + " | ".join(results.keys()) + " |",
        "|---|" + "---|" * len(results),
    ]
    rows = [
        ("会话完成率", lambda m: f"{m['session_rate'] * 100:.1f}%"),
        ("轨道路由准确率", lambda m: f"{m['route_rate'] * 100:.1f}%"),
        ("平均每轮耗时(s)", lambda m: f"{m['avg_turn_elapsed']:.1f}"),
        ("误转人工", lambda m: f"{m['handoff_fp']}/{m['handoff_fp_total']}"),
    ]
    for name, fmt in rows:
        lines.append(f"| {name} | " + " | ".join(fmt(m) for m in results.values()) + " |")
    return "\n".join(lines)


def group_table(results: dict[str, dict[str, Any]]) -> str:
    groups = sorted({g for m in results.values() for g in m["by_group"]})
    lines = [
        "| 分组 | " + " | ".join(f"{k} 完成率" for k in results) + " |",
        "|---|" + "---|" * len(results),
    ]
    for g in groups:
        cells = []
        for m in results.values():
            info = m["by_group"].get(g)
            if info and info["total"]:
                cells.append(f"{info['success']}/{info['total']}")
            else:
                cells.append("-")
        label = GROUP_NAMES.get(g, g)
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
