"""会话评测入口。

    python -m eval.run_eval                 # 跑 llm + rule 两个变体并出报告
    python -m eval.run_eval --variant llm   # 只跑一个
    python -m eval.run_eval --limit 5       # 先冒烟几段
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from app.conf.settings import settings
from app.core.log import setup as setup_log
from app.dialogue.engine import DialogueEngine
from app.flows.loader import load as load_flows
from app.infra.repo import StateRepo
from app.tracks.knowledge import load_passages
from eval.metrics import aggregate, comparison_table, group_table
from eval.runner import load_sessions, run_session

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = Path(__file__).resolve().parent / "sessions.jsonl"
RESULTS = ROOT / "eval" / "results"

VARIANTS = ("llm", "rule")


async def run_variant(name: str, sessions: list[dict], engine: DialogueEngine) -> list:
    records = []
    for i, session in enumerate(sessions, start=1):
        record = await run_session(engine, session, name)
        records.append(record)
        mark = "✓" if record.success else "✗"
        print(
            f"  [{i:>2}/{len(sessions)}] {mark} {record.id} {record.group:<16}"
            f" 路由 {record.route_hits}/{record.route_total}"
            f" {record.elapsed:.1f}s"
            + (f" ← {'; '.join(record.failures[:2])}" if record.failures else "")
        )
    return records


def write_report(out_dir: Path, results: dict, records_by_variant: dict) -> None:
    lines = [
        "# 履约客服助手 会话级评测",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 脚本：`eval/sessions.jsonl`，{len(next(iter(records_by_variant.values())))} 段多轮会话",
        f"- 规划模型：`{settings.llm.planner_model}`；对照变体关闭模型、走规则规划器",
        "- 每段会话前重置对话状态与中台写表，互不污染",
        "",
        "## 总表",
        "",
        comparison_table(results),
        "",
        "## 分组完成率",
        "",
        group_table(results),
        "",
    ]

    for name, records in records_by_variant.items():
        failed = [r for r in records if not r.success]
        if failed:
            lines += [f"## {name} 变体的失败明细", ""]
            for r in failed:
                lines.append(f"### {r.id}（{r.group}）")
                lines.append("")
                for turn in r.turns:
                    lines.append(
                        f"- turn{turn.idx} [{turn.action}/{turn.flow_id or '-'}"
                        f" conf={turn.confidence:.2f} src={turn.source}] {turn.text}"
                    )
                    for reply in turn.replies:
                        lines.append(f"  - 答：{reply[:80]}")
                lines.append("")
                for failure in r.failures:
                    lines.append(f"  - ✗ {failure}")
                lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=VARIANTS + ("both",), default="both")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 段（冒烟用）")
    parser.add_argument("--only", default="", help="只跑指定 id，逗号分隔，如 s06,s10")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    setup_log("WARNING")

    sessions = load_sessions(SESSIONS)
    if args.only:
        wanted_ids = {x.strip() for x in args.only.split(",") if x.strip()}
        sessions = [s for s in sessions if s["id"] in wanted_ids]
        missing = wanted_ids - {s["id"] for s in sessions}
        if missing:
            raise SystemExit(f"脚本里没有这些 id：{sorted(missing)}")
    if args.limit:
        sessions = sessions[: args.limit]

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    registry = load_flows(settings.dialogue.flow_dir)
    passages = load_passages(settings.dialogue.kb_dir)

    wanted = VARIANTS if args.variant == "both" else (args.variant,)
    results: dict[str, dict] = {}
    records_by_variant: dict[str, list] = {}

    for name in wanted:
        print(f"== 变体 {name} ==")
        original_key = settings.llm.api_key
        if name == "rule":
            settings.llm.api_key = ""  # 关模型，强制走规则规划器
        try:
            engine = DialogueEngine(
                registry, StateRepo(ROOT / "data" / "eval_dialogue.db"), passages
            )
            records = await run_variant(name, sessions, engine)
        finally:
            settings.llm.api_key = original_key

        records_by_variant[name] = records
        results[name] = aggregate(records)
        (out_dir / f"{name}.json").write_text(
            json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        m = results[name]
        print(
            f"  完成 {m['session_success']}/{m['sessions']}"
            f" 路由 {m['route_hits']}/{m['route_total']}"
        )

    write_report(out_dir, results, records_by_variant)
    print(f"\n报告：{out_dir / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
