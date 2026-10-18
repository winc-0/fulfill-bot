"""一条命令把三个服务拉起来：模拟履约中台 → 履约客服助手后端 → 前端。

    .venv/Scripts/python scripts/dev.py            # 全套
    .venv/Scripts/python scripts/dev.py --no-front # 只要后端
    .venv/Scripts/python scripts/dev.py --check    # 只体检不启动

Ctrl+C 一起退。依赖服务挂了不阻断启动——客服本来就该降级跑，
只是对应那一路的回答会变成"暂时连不上"。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMERCE_PORT = int(os.getenv("COMMERCE_PORT", "18081"))
SERVER_PORT = int(os.getenv("SERVER_PORT", "18082"))
FRONTEND_PORT = 5280


def _say(msg: str) -> None:
    print(f"[dev] {msg}", flush=True)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    # urllib 默认走系统代理，本机地址会被代理劫持，这里显式绕开
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def check() -> int:
    _say("体检：")
    ok = True

    env_file = ROOT / ".env"
    if env_file.exists():
        _say(f"  ✓ .env 存在")
    else:
        _say("  ! .env 不存在 → 规划将走规则引擎（能用，但指代消解差）。复制 .env.example 改名即可")
        ok = False

    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from app.conf.settings import settings

    if settings.llm.enabled:
        _say(f"  ✓ 模型已配置（规划 {settings.llm.planner_model}）")
    else:
        _say("  ! LLM_API_KEY 未配置 → 全部走规则规划器")
        ok = False

    if _http_ok(f"http://127.0.0.1:{COMMERCE_PORT}/health"):
        _say(f"  ✓ 履约中台 :{COMMERCE_PORT} 在跑")
    else:
        _say(f"  ! 中台 :{COMMERCE_PORT} 不可达 → 查状态/查轨迹/理赔/改址/催派全部降级（dev.py 会自动拉起）")

    db = ROOT / "data" / "commerce.db"
    if db.exists():
        _say(f"  ✓ 中台数据 {db.name} 存在")
    else:
        _say("  ! 中台数据不存在 → 首次启动会自动建库+写种子")

    return 0 if ok else 1


def _spawn(cmd: list[str], cwd: Path, label: str) -> subprocess.Popen:
    _say(f"启动 {label}: {' '.join(cmd[:4])}...")
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只体检")
    parser.add_argument("--no-front", action="store_true", help="不起前端")
    args = parser.parse_args()

    if args.check:
        return check()
    check()

    children: list[subprocess.Popen] = []

    if not _port_open(COMMERCE_PORT):
        seed = ROOT / "data" / "commerce.db"
        if not seed.exists():
            _say("初始化中台数据...")
            subprocess.run(
                [sys.executable, "-m", "services.commerce.seed"],
                cwd=str(ROOT), check=False,
            )
        children.append(
            _spawn([sys.executable, "-m", "services.commerce.app"], ROOT, f"中台 :{COMMERCE_PORT}")
        )
    else:
        _say(f"中台 :{COMMERCE_PORT} 已在跑，跳过")

    if _port_open(SERVER_PORT):
        _say(f"端口 {SERVER_PORT} 已被占用 → 后端可能已在跑，跳过")
    else:
        children.append(
            _spawn(
                [sys.executable, "-m", "uvicorn", "app.api.server:app",
                 "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
                ROOT, f"后端 :{SERVER_PORT}",
            )
        )

    if not args.no_front and (ROOT / "frontend" / "node_modules").is_dir():
        if _port_open(FRONTEND_PORT):
            _say(f"前端 :{FRONTEND_PORT} 已在跑，跳过")
        else:
            npm = shutil.which("npm") or "npm"
            children.append(
                _spawn(
                    [npm, "run", "dev", "--", "--host", "127.0.0.1",
                     "--port", str(FRONTEND_PORT), "--strictPort"],
                    ROOT / "frontend", f"前端 :{FRONTEND_PORT}",
                )
            )
    elif not args.no_front:
        _say("前端依赖未安装（frontend/node_modules 不存在），跳过前端。先 cd frontend && npm install")

    # 等后端就绪
    deadline = time.time() + 20
    while time.time() < deadline and not _http_ok(f"http://127.0.0.1:{SERVER_PORT}/health"):
        time.sleep(0.5)
    if _http_ok(f"http://127.0.0.1:{SERVER_PORT}/health"):
        _say(f"后端就绪  http://127.0.0.1:{SERVER_PORT}")
    else:
        _say("后端 20s 内没就绪，看上面各进程的报错")
    if _port_open(FRONTEND_PORT):
        _say(f"界面      http://127.0.0.1:{FRONTEND_PORT}")

    _say("Ctrl+C 一起退出")
    try:
        while True:
            time.sleep(1)
            for child in children:
                code = child.poll()
                if code is not None:
                    _say(f"一个进程退出了（code={code}），全部收摊")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        _say("正在退出...")
    finally:
        for child in children:
            with contextlib.suppress(Exception):
                child.send_signal(signal.CTRL_BREAK_EVENT)
        for child in children:
            with contextlib.suppress(Exception):
                child.wait(timeout=5)
        _say("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
