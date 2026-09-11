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
    POST /drafts/{draft_no}/claim            按稿号认领（首次 201；本人重复认领 200；别人已认领 409）
    POST /drafts/{draft_no}/claim/release    把当前认领交出去，之后才能换人认（未认领 409）
    GET  /drafts/{draft_no}/claim            这一稿现在谁认着（含历次认领/交出记录）
    GET  /sessions/{call_id}/claims          该通话全部稿的认领状态（按发稿顺序）
    POST /drafts/{draft_no}/delivery         按稿号往下游投（待回音/已收下 409；退了才能再投）
    POST /drafts/{draft_no}/delivery/accept  下游收下（200；终态，不能再收/退/投）
    POST /drafts/{draft_no}/delivery/return  下游退回（200；退了之后才能再投）
    GET  /drafts/{draft_no}/delivery         这一稿投到哪了（历次投递 + 当前状态）
    GET  /sessions/{call_id}/deliveries      该通话全部稿的投递状态（按发稿顺序）
    POST /drafts/{draft_no}/errata           按稿号对某一段出勘误（201；未认领/已撤回/段不在稿里/该段已出过 409）
    GET  /drafts/{draft_no}/errata           这一稿出过的全部勘误（按段序）
    GET  /sessions/{call_id}/errata          该通话出过的全部勘误（按发稿顺序、段序）
    GET  /healthz                            健康检查

    发出去的稿要先有人认领：没认领的稿不能签收、不能回放、不能撤回、不能
    往下游投递，也不能出勘误（409）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Response

from .models import ClaimIn, ErrataIn, FragmentIn
from .store import Store


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "sessions.db")
    store = Store(db_path)

    app = FastAPI(
        title="通话片段拼接服务",
        version="1.8.0",
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
        看得出签的是哪一稿；同一份不能签两次，撤过的稿不能再签（409）。
        稿要先有人认领：没认领的稿不能签（409）。"""
        receipt, result = store.sign_draft(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        if result == "already_withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        if result == "already_signed":
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
        稿要先有人认领：没认领的稿不能撤（409）。
        """
        withdrawal, result = store.withdraw_draft(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        if result == "already_signed":
            raise HTTPException(status_code=409, detail="draft already signed")
        if result == "delivery_pending":
            raise HTTPException(status_code=409, detail="delivery awaiting response")
        if result == "delivery_accepted":
            raise HTTPException(status_code=409, detail="delivery already accepted")
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
        回到当前进度，绝不重头再听。稿要先有人认领：没认领的稿不能听（409）。
        首次开始 201；已存在 200；未知稿号 404；已撤回/未认领 409。
        """
        playback, result = store.start_playback(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        response.status_code = 201 if result == "started" else 200
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

    # -------------------------------------------------------------- 认领

    @app.post("/drafts/{draft_no}/claim")
    def claim_draft(draft_no: str, body: ClaimIn, response: Response):
        """按稿号认领一稿。发出去的稿要先有人认领：没认领不能签收、
        不能回放、也不能撤回。一个人认了，别人不能再认走（409）——
        得等当前认领人把它交出去。本人重复认领幂等（200，时间不动）。
        认领只记“谁认的”，不改那一稿当时的正文和缺口。
        首次认领 201；本人重复 200；别人已认领 409；未知稿号 404。"""
        claim, result = store.claim_draft(draft_no, body.claimed_by)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "already_held":
            raise HTTPException(status_code=409, detail="draft already claimed")
        response.status_code = 201 if result == "claimed" else 200
        return claim

    @app.post("/drafts/{draft_no}/claim/release")
    def release_claim(draft_no: str):
        """把当前认领交出去。交出去之后别人才能认领这一稿；交出后、
        还没人认领期间，这稿同样不能签、不能听、不能撤。
        交出 200；当前没人认领 409；未知稿号 404。"""
        claim, result = store.release_claim(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        return claim

    @app.get("/drafts/{draft_no}/claim")
    def get_claim(draft_no: str):
        """这一稿现在谁认着（含历次认领/交出记录）。稿快照原样嵌在里面 ——
        认领来认领去，当时那一稿的正文和缺口一个字不变。"""
        claim = store.get_claim(draft_no)
        if claim is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return claim

    @app.get("/sessions/{call_id}/claims")
    def list_claims(call_id: str):
        """该通话全部稿的认领状态（按发稿顺序）。查询带 call_id，
        结构上列不出另一通电话的认领。"""
        claims = store.list_claims(call_id)
        if claims is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"claims": claims}

    # -------------------------------------------------------------- 下游投递

    @app.post("/drafts/{draft_no}/delivery", status_code=201)
    def deliver_draft(draft_no: str):
        """按稿号把一稿往下游投，返回投递记录（带稿号、通话、第几稿、第几次投，
        看得出投的是哪一稿）。没人认领不能投；上一次投出去还没回音不能再投；
        下游已经收下不能再投；稿撤过也不能投。只有退回之后才能再投一次
        （attempt 递增，历次投递都留着）。201 本次投出；上述冲突 409；
        未知稿号 404。"""
        delivery, result = store.deliver_draft(draft_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        if result == "withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        if result == "pending":
            raise HTTPException(status_code=409, detail="delivery awaiting response")
        if result == "accepted":
            raise HTTPException(status_code=409, detail="delivery already accepted")
        return delivery

    @app.post("/drafts/{draft_no}/delivery/accept")
    def accept_delivery(draft_no: str):
        """下游收下最近一次待回音的投递。收下是终态：不能再退、不能再投。
        200；没在等回音（没投过/已答复过）、稿已撤回或未认领 → 409；
        未知稿号 → 404。"""
        return _decide_delivery(draft_no, accepted=True)

    @app.post("/drafts/{draft_no}/delivery/return")
    def return_delivery(draft_no: str):
        """下游退回最近一次待回音的投递。退回之后这稿才能再投一次；
        退回本身不改那一稿当时的正文和缺口。
        200；没在等回音、稿已撤回或未认领 → 409；未知稿号 → 404。"""
        return _decide_delivery(draft_no, accepted=False)

    def _decide_delivery(draft_no: str, accepted: bool):
        delivery, result = store.decide_draft(draft_no, accepted)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        if result == "withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        if result == "not_pending":
            raise HTTPException(status_code=409, detail="no delivery awaiting response")
        return delivery

    @app.get("/drafts/{draft_no}/delivery")
    def get_delivery(draft_no: str):
        """这一稿投到哪了：当前状态（none/pending/accepted/returned）和历次
        投递（投出时间、收下/退回时间）。记录嵌的仍是发稿当时的正文和缺口。"""
        delivery = store.get_delivery(draft_no)
        if delivery is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return delivery

    @app.get("/sessions/{call_id}/deliveries")
    def list_deliveries(call_id: str):
        """该通话全部稿的投递状态（按发稿顺序）。查询带 call_id，
        结构上列不出另一通电话的投递。"""
        deliveries = store.list_deliveries(call_id)
        if deliveries is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"deliveries": deliveries}

    # -------------------------------------------------------------- 勘误

    @app.post("/drafts/{draft_no}/errata", status_code=201)
    def issue_errata(draft_no: str, body: ErrataIn):
        """按稿号对这一稿的某一段出勘误：记录带稿号、通话、第几稿、对着
        哪一段、当时的原文和改成什么，看得出勘的是哪一稿的哪一段。
        勘误只新增记录，不改那一稿当时的正文和缺口。没人认领的不能出；
        撤过的稿不能出；对着的段必须在这一稿里（缺口不是段）；同一段
        不能出两次。201；上述冲突 409；未知稿号 404。"""
        erratum, result = store.issue_errata(draft_no, body.seq, body.new_text)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == "not_claimed":
            raise HTTPException(status_code=409, detail="draft not claimed")
        if result == "withdrawn":
            raise HTTPException(status_code=409, detail="draft already withdrawn")
        if result == "unknown_seq":
            raise HTTPException(status_code=409, detail="segment not in this draft")
        if result == "already_issued":
            raise HTTPException(status_code=409, detail="errata already issued for this segment")
        return erratum

    @app.get("/drafts/{draft_no}/errata")
    def get_errata(draft_no: str):
        """这一稿出过的全部勘误（按段序）：各自对着哪一段、当时是什么、
        改成什么。稿快照原样嵌在里面 —— 勘误碰不到它的正文和缺口。"""
        errata = store.get_errata(draft_no)
        if errata is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return errata

    @app.get("/sessions/{call_id}/errata")
    def list_errata(call_id: str):
        """该通话出过的全部勘误（按发稿顺序、段序）。查询带 call_id，
        结构上列不出另一通电话的勘误。"""
        errata = store.list_errata(call_id)
        if errata is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"errata": errata}

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
