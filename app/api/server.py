"""HTTP 入口。

与前端约定（沿用项目里既有的契约，前端不用改）：
    POST /api/chat            {sender_id, text} 或 {sender_id, object}
    GET  /api/chat/history    ?sender_id=
    GET  /health
额外的运维接口：
    GET  /api/debug/state/{sender_id}   直接看状态机快照
    POST /api/reset/{sender_id}         清空某个会话
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.conf.settings import settings
from app.core.log import get_logger
from app.dialogue.engine import DialogueEngine
from app.flows.loader import load as load_flows
from app.infra.repo import StateRepo
from app.tracks.knowledge import load_passages

logger = get_logger("server")


class ChatRequest(BaseModel):
    sender_id: str
    text: str = ""
    object: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    messages: list[dict[str, Any]]
    debug: dict[str, Any] | None = None


def _normalize_object(raw: dict[str, Any]) -> dict[str, Any]:
    """前端点卡片时传的是 {type, id, attributes:{...}}，这里抹平成内部结构。"""
    kind = str(raw.get("type") or "")
    ident = str(raw.get("id") or raw.get("tracking_number") or "")
    attrs = raw.get("attributes") or {}
    if kind == "shipment":
        return {
            "type": "shipment",
            "tracking_number": ident,
            "title": raw.get("title") or attrs.get("title") or "运单",
            "status": attrs.get("status") or raw.get("status") or "",
            "carrier": attrs.get("carrier") or raw.get("carrier") or "",
        }
    # 未知类型原样透传，交给引擎走澄清，不在这里硬造一个类型
    return {
        "type": kind or "unknown",
        "tracking_number": ident,
        "title": raw.get("title") or attrs.get("title") or "",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = load_flows(settings.dialogue.flow_dir)
    repo = StateRepo(settings.dialogue.db_path)
    passages = load_passages(settings.dialogue.kb_dir)
    app.state.engine = DialogueEngine(registry, repo, passages)
    logger.info(
        f"履约客服助手启动：{len(registry.flows)} 个业务流程，{len(registry.system_flows)} 个系统流程，"
        f"{len(passages)} 段知识；模型={'已配置' if settings.llm.enabled else '未配置（走规则）'}"
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="履约客服助手", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm": settings.llm.enabled}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        if not req.sender_id.strip():
            raise HTTPException(status_code=400, detail="sender_id 不能为空")
        obj = _normalize_object(req.object) if req.object else None
        result = await app.state.engine.handle(req.sender_id.strip(), req.text, obj)
        debug = None
        if settings.debug:
            debug = {
                "plan": result.plan.to_dict() if result.plan else None,
                "notes": result.notes,
                "transitions": result.transitions,
            }
        return ChatResponse(messages=result.messages, debug=debug)

    @app.get("/api/chat/history")
    async def history(sender_id: str):
        state = app.state.engine.repo.get(sender_id.strip())
        return {"messages": state.messages}

    @app.get("/api/debug/state/{sender_id}")
    async def debug_state(sender_id: str):
        state = app.state.engine.repo.get(sender_id.strip())
        return state.to_dict()

    @app.post("/api/reset/{sender_id}")
    async def reset(sender_id: str):
        app.state.engine.repo.reset(sender_id.strip())
        return {"ok": True}

    @app.post("/api/chat/tts")
    async def tts():
        # 语音播报不在本期范围，明确返回 501，前端会自己隐藏播放按钮
        raise HTTPException(status_code=501, detail="本期未接入语音合成")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.api.server:app",
        host=settings.server.host,
        port=int(os.getenv("SERVER_PORT", settings.server.port)),
        log_level="info",
    )
