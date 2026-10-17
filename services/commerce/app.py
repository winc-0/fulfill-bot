"""履约中台应用装配。

单独跑：python -m services.commerce.app  （默认 18081）
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.commerce.api import router
from services.commerce.db import init_schema


def create_app() -> FastAPI:
    app = FastAPI(title="履约客服助手 · 模拟履约中台", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    init_schema()
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "services.commerce.app:app",
        host="127.0.0.1",
        port=int(os.getenv("COMMERCE_PORT", "18081")),
        log_level="info",
    )
