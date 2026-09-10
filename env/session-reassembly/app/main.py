"""HTTP API 层。

    POST /fragments          接收一个片段（可乱序、可重传），返回该会话当前视图
    GET  /sessions           所有会话摘要
    GET  /sessions/{call_id} 单个会话的拼装结果（内容、状态、缺口、历史）
    GET  /healthz            健康检查
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from .models import FragmentIn
from .store import Store


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "sessions.db")
    store = Store(db_path)

    app = FastAPI(
        title="通话片段拼接服务",
        version="1.0.0",
        description="把同一条链路上乱序、带重传的通话片段拼回完整会话。",
    )
    app.state.store = store

    @app.post("/fragments")
    def post_fragment(f: FragmentIn):
        result = store.ingest(f.call_id, f.seq, f.text, f.is_last)
        return {"ingest": result, "session": store.get_session(f.call_id)}

    @app.get("/sessions")
    def list_sessions():
        return {"sessions": store.list_sessions()}

    @app.get("/sessions/{call_id}")
    def get_session(call_id: str):
        session = store.get_session(call_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return session

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
