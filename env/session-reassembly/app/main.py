"""HTTP API 层。

    POST /fragments                        接收一个片段（可乱序、可重传），返回该会话当前视图
    GET  /sessions                         所有会话摘要
    GET  /sessions/{call_id}               单个会话的拼装结果（内容、状态、缺口、历史）—— 永远是**最新**样子
    POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（快照从此不可改）
    GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
    GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
    GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿的正文和缺口
    GET  /healthz                          健康检查
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
        version="1.1.0",
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

    @app.post("/sessions/{call_id}/drafts", status_code=201)
    def issue_draft(call_id: str):
        """对外给出：把当前视图钉成一稿，返回稿号。此后这一稿不可改。"""
        draft = store.issue_draft(call_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return draft

    @app.get("/sessions/{call_id}/drafts")
    def list_drafts(call_id: str):
        drafts = store.list_drafts(call_id)
        if drafts is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"drafts": drafts}

    @app.get("/sessions/{call_id}/drafts/{draft_seq}")
    def get_draft_by_seq(call_id: str, draft_seq: int):
        draft = store.get_draft_by_seq(call_id, draft_seq)
        if draft is None:
            raise HTTPException(status_code=404, detail="unknown draft")
        return draft

    @app.get("/drafts/{draft_no}")
    def get_draft(draft_no: str):
        """按稿号取稿。无论后来补段、重传还是又发了新稿，这里永远返回
        当时那一稿的正文和缺口。"""
        draft = store.get_draft(draft_no)
        if draft is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return draft

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
