"""命令行对话，不启前端也能演示完整链路。

    .venv/Scripts/python scripts/chat.py            # u1001
    .venv/Scripts/python scripts/chat.py --sender u1002
    .venv/Scripts/python scripts/chat.py --reset    # 先清掉这个人的会话
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.conf.settings import settings  # noqa: E402
from app.core.log import setup as setup_log  # noqa: E402
from app.dialogue.engine import DialogueEngine  # noqa: E402
from app.flows.loader import load as load_flows  # noqa: E402
from app.infra.repo import StateRepo  # noqa: E402
from app.tracks.knowledge import load_passages  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sender", default="u1001")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    setup_log("ERROR")

    engine = DialogueEngine(
        load_flows(settings.dialogue.flow_dir),
        StateRepo(settings.dialogue.db_path),
        load_passages(settings.dialogue.kb_dir),
    )
    if args.reset:
        engine.repo.reset(args.sender)

    mode = "模型规划" if settings.llm.enabled else "规则规划（未配置 LLM_API_KEY）"
    print(f"履约客服助手 · {mode} · 用户 {args.sender}（Ctrl+C 或 exit 退出）\n")

    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit", "q"}:
            break

        result = await engine.handle(args.sender, text)
        for message in result.messages:
            if message.get("object"):
                obj = message["object"]
                label = obj.get("title") or obj.get("tracking_number")
                print(f"履约客服助手 > [卡片] {obj.get('type')}：{label}")
            else:
                print(f"履约客服助手 > {message.get('text', '')}")
        plan = result.plan
        if plan:
            print(
                f"      （{plan.action}{'/' + plan.flow_id if plan.flow_id else ''}"
                f" conf={plan.confidence:.2f} src={plan.source}）"
            )
        print()


if __name__ == "__main__":
    asyncio.run(main())
