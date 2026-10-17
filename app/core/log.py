"""日志：一行一条，带模块名与耗时，方便从终端直接看出某轮走了哪条轨道。"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("zhifu")
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"zhifu.{name}")
