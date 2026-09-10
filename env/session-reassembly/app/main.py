"""HTTP API 层。

    POST /fragments                        接收一个片段（可乱序、可重传），返回该会话当前视图
    GET  /sessions                         所有会话摘要
    GET  /sessions/{call_id}               单个会话的拼装结果（内容、状态、缺口、历史）—— 永远是**最新**样子
    POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（快照从此不可改）
    GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
    GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
    GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿的正文和缺口
    POST /drafts/{draft_no}/receipt        按稿号签收（同一份不能签两次：409）
    GET  /drafts/{draft_no}/receipt        该稿的签收单（待签/已签 + 当时那稿的正文和缺口）
    POST /drafts/{draft_no}/withdrawal     按稿号撤回未签稿（已签/已撤：409；/withdraw 为别名）
    GET  /drafts/{draft_no}/withdrawal     该稿的撤回记录（未撤回 404）
    GET  /sessions/{call_id}/withdrawals   该通话已撤回的全部稿
    GET  /sessions/{call_id}/receipts      该通话的全部签收单（按发稿顺序）
    POST /drafts/{draft_no}/playback       拿稿号开始回放（已开始则只回到当前进度，不重头）
    GET  /drafts/{draft_no}/playback       听到哪了（未开始 409；未知稿号 404）
    POST /drafts/{draft_no}/playback/advance  按顺序听下一段：轮到缺口或稿已撤回就停住
    GET  /sessions/{call_id}/playbacks     该通话已开始的全部回放（按发稿顺序）
    GET  /sessions/{call_id}/late-fragments  该通话的全部迟到片段（按到达顺序，各自补在哪一稿后面）
    GET  /drafts/{draft_no}/late-fragments   补在这一稿后面的迟到片段
    GET  /healthz                          健康检查
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Response

from .models import FragmentIn
from .store import Store


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "sessions.db")
    store = Store(db_path)

    app = FastAPI(
        title="通话片段拼接服务",
        version="1.5.0",
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

    @app.post("/drafts/{draft_no}/receipt", status_code=201)
    def sign_receipt(draft_no: str):
        """按稿号签收。返回的签收单上带稿号、通话、第几稿和签收时间，
        看得出签的是哪一稿；同一份不能签两次，撤过的稿不能再签（409）。"""
        receipt, created = store.sign_draft(draft_no)
        if receipt is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if not created:
            if receipt["status"] == "withdrawn":
                raise HTTPException(status_code=409, detail="draft already withdrawn")
            raise HTTPException(status_code=409, detail="draft already signed")
        return receipt

    @app.get("/drafts/{draft_no}/receipt")
    def get_receipt(draft_no: str):
        """取一稿的签收单。待签时取出仍是发稿那一刻的正文和缺口 ——
        后来补段、重传、会话变新都不改这份。"""
        receipt = store.get_receipt(draft_no)
        if receipt is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return receipt

    @app.get("/sessions/{call_id}/receipts")
    def list_receipts(call_id: str):
        """该通话的全部签收单（按发稿顺序）：哪些还欠着、哪些已签。"""
        receipts = store.list_receipts(call_id)
        if receipts is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"receipts": receipts}

    # -------------------------------------------------------------- 撤回

    @app.post("/drafts/{draft_no}/withdrawal", status_code=201)
    @app.post("/drafts/{draft_no}/withdraw", status_code=201, include_in_schema=False)
    def withdraw_draft(draft_no: str):
        """按稿号撤回一份尚未签收的稿。

        撤回不改 drafts 里的正文和缺口；响应与撤回记录仍带整份稿，明确看得出
        撤的是哪一稿。已签收或已撤回返回 409；未知稿号返回 404。
        """
        withdrawal, result = store.withdraw_draft(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "already_signed":
            raise HTTPException(status_code=409, detail="draft already signed")
        if result == "already_withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        return withdrawal

    @app.get("/drafts/{draft_no}/withdrawal")
    def get_withdrawal(draft_no: str):
        withdrawal = store.get_withdrawal(draft_no)
        if withdrawal is None:
            raise HTTPException(status_code=404, detail="draft not withdrawn")
        return withdrawal

    @app.get("/sessions/{call_id}/withdrawals")
    def list_withdrawals(call_id: str):
        withdrawals = store.list_withdrawals(call_id)
        if withdrawals is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"withdrawals": withdrawals}

    # -------------------------------------------------------------- 回放

    @app.post("/drafts/{draft_no}/playback")
    def start_playback(draft_no: str, response: Response):
        """拿稿号开始听，从这一稿的第 1 个单元顺序往后。

        回放读的是发稿那一刻钉死的快照：后来补段不会让它变完整，轮到稿里
        当时的缺口必须停住；稿撤回后也不能再开始。已经开始过且未撤回则幂等
        回到当前进度，绝不重头再听。
        首次开始 201；已存在 200；未知稿号 404；已撤回 409。
        """
        playback, started = store.start_playback(draft_no)
        if playback is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if not started and playback["status"] == "withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        response.status_code = 201 if started else 200
        return playback

    @app.post("/drafts/{draft_no}/playback/advance")
    def advance_playback(draft_no: str):
        """按顺序听下一个单元。

        轮到正常片段就前进并返回听到的那段；下一个是发稿当时的缺口、或稿已
        撤回就停住（status=blocked/withdrawn，位置不动，不跳过）；到末尾
        finished。还没开始回放 → 409；未知稿号 → 404。
        """
        playback = store.advance_playback(draft_no)
        if playback is None:
            # 区分“号不存在”与“还没开始”，错误信息不误导调用方
            if store.get_draft(draft_no) is None:
                raise HTTPException(status_code=404, detail="unknown draft_no")
            raise HTTPException(status_code=409, detail="playback not started")
        return playback

    @app.get("/drafts/{draft_no}/playback")
    def get_playback(draft_no: str):
        """听到哪了。进度落 SQLite，重启后还停在那儿。稿已撤回显示
        withdrawn；未开始 409。"""
        playback = store.get_playback(draft_no)
        if playback is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if playback["status"] == "not_started":
            raise HTTPException(status_code=409, detail="playback not started")
        return playback

    @app.get("/sessions/{call_id}/playbacks")
    def list_playbacks(call_id: str):
        """该通话已开始的全部回放（按发稿顺序）。查询带 call_id，
        结构上列不出另一通电话的回放。"""
        playbacks = store.list_playbacks(call_id)
        if playbacks is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"playbacks": playbacks}

    # -------------------------------------------------------------- 迟到片段

    @app.get("/sessions/{call_id}/late-fragments")
    def list_late_fragments(call_id: str):
        """该通话的全部迟到片段（按到达顺序）：发过至少一稿之后才新到的段，
        各自看得出补在哪一稿后面。查询带 call_id，两通电话的记录不串。"""
        late = store.list_late_fragments(call_id)
        if late is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"late_fragments": late}

    @app.get("/drafts/{draft_no}/late-fragments")
    def list_late_fragments_for_draft(draft_no: str):
        """补在这一稿后面的迟到片段：它发出之后才到、没能进这一稿的段。
        只读迟到记录 —— 稿、待签、回放都不受影响。"""
        late = store.list_late_fragments_for_draft(draft_no)
        if late is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return {"draft_no": draft_no, "late_fragments": late}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
