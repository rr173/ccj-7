"""HTTP API 层。

    POST /fragments                        接收一个片段（可乱序、可重传），返回该会话当前视图
    GET  /sessions                         所有会话摘要
    GET  /sessions/{call_id}               单个会话的拼装结果（内容、状态、缺口、历史）—— 永远是**最新**样子
    POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（快照从此不可改）
    GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
    GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
    GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿的正文和缺口
    POST /drafts/{draft_no}/hold            把稿压到指定时刻才见（201；压过 409；时刻早于发稿 422；逆序 409）
    GET  /drafts/{draft_no}/hold            这一稿压到何时、现在还压着没有（没压过 404）
    GET  /sessions/{call_id}/holds          该通话压过的全部稿（按发稿顺序）
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
    POST /bridges                            拿两通不同的电话搭一座桥（201；空通话/同一通/已在活动桥 409；未知 404）
    GET  /bridges                            全部桥（搭着的和已拆的）
    GET  /bridges/{bridge_no}                按桥号取桥：逐对对齐、缺口缺哪一边（活动桥现算/已拆桥给拆时快照）
    POST /bridges/{bridge_no}/dismantle      拆桥（两通恢复各自独立，对齐进度留痕；已拆 409）
    POST /bridges/{bridge_no}/swap           桥还搭着时换掉其中一边（桥号不变、按新两边重新对齐；旧两边与旧对齐留痕）
    GET  /bridges/{bridge_no}/swaps          这座桥历次换边的留痕（换下/换上/不动各是谁、何时换、换边前对齐）
    POST /bridges/{bridge_no}/photos         桥还搭着时拍一张照（钉住此刻两边对到哪一对；已拆 409）
    GET  /bridges/{bridge_no}/photos         这座桥拍过的全部照片（第几张、何时拍、拍时对齐）
    GET  /bridges/{bridge_no}/photos/{n}     这座桥的第 n 张照片（永远是拍时那份对齐）
    GET  /sessions/{call_id}/bridges         该通话上过的全部桥（标明左/右；被换下去的也在）
    GET  /healthz                            健康检查

    发出去的稿要先有人认领：没认领的稿不能签收、不能回放、不能撤回、不能
    往下游投递，也不能出勘误（409）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Response

from .models import BridgeIn, BridgeSwapIn, ClaimIn, ErrataIn, FragmentIn, HoldIn
from .store import Store


def create_app(
    db_path: str | None = None, now_fn=None
) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "sessions.db")
    store = Store(db_path, now_fn=now_fn)

    app = FastAPI(
        title="通话片段拼接服务",
        version="1.12.0",
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
        当时那一稿的正文和缺口。稿还压着（没到解禁时刻）时只回“还压着、
        何时解”：正文、拼装单元、缺口等全为 null；到点后同一调用自动给回
        当时那份完整快照，不需要任何“解开”操作。"""
        draft = store.get_draft(draft_no)
        if draft is None:
            raise HTTPException(status_code=404, detail="unknown draft_no")
        return draft

    # -------------------------------------------------------------- 压稿

    @app.post("/drafts/{draft_no}/hold", status_code=201)
    def hold_draft(draft_no: str, body: HoldIn):
        """把一稿压到指定时刻才见：写明几点几分，到点自动可见。

        解禁时刻不能早于这稿发出的时刻；写上去就不能改、也不能提前解开
        （一稿只能压一次，重复压 409）；同一通电话里后发出的稿不能比先发
        的更早解。压上后取这稿只能看到“还压着、何时解”，正文和缺口不可见；
        到点后无需任何操作，再取就是发稿当时的正文和缺口。
        """
        hold, result = store.hold_draft(draft_no, body.release_at)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown draft_no")
        if result == store.ALREADY_HELD:
            raise HTTPException(status_code=409, detail="draft already held")
        if result == store.ORDER_VIOLATION:
            raise HTTPException(
                status_code=409,
                detail="release_at must be no earlier than prior drafts of this call",
            )
        if result == store.RELEASE_IN_PAST:
            # 解禁时刻早于发稿时刻：请求本身不合法（422），稿不动
            raise HTTPException(
                status_code=422, detail="release_at must not be before issued_at"
            )
        return hold

    @app.get("/drafts/{draft_no}/hold")
    def get_hold(draft_no: str):
        """这一稿压到何时、现在还压着没有。压着时嵌的 draft 只有身份信息
        和“还压着、何时解”，正文/缺口为 null；到点后 draft 自动是完整快照。
        没压过 404。"""
        hold = store.get_hold(draft_no)
        if hold is None:
            raise HTTPException(status_code=404, detail="draft not held")
        return hold

    @app.get("/sessions/{call_id}/holds")
    def list_holds(call_id: str):
        """该通话压过的全部稿（按发稿顺序）。查询带 call_id，两通电话的
        压稿记录不串。未知 call_id 404。"""
        holds = store.list_holds(call_id)
        if holds is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"holds": holds}

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
        if result == "held":
            raise HTTPException(status_code=409, detail="draft is held until release_at")
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
        if playback["status"] in ("not_started", "held"):
            # 还没开始 / 稿还压着：没有可听进度，正文也不可见（409）
            raise HTTPException(status_code=409, detail=f"playback {playback['status']}")
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
        if result == "held":
            raise HTTPException(status_code=409, detail="draft is held until release_at")
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
        if result == "held":
            raise HTTPException(status_code=409, detail="draft is held until release_at")
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
        if result == "held":
            raise HTTPException(status_code=409, detail="draft is held until release_at")
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

    # -------------------------------------------------------------- 桥

    @app.post("/bridges", status_code=201)
    def create_bridge(body: BridgeIn):
        """拿两通不同的电话搭一座桥，桥上按序号一对一对齐。

        同一序号两边都到了才算一对对齐；一边缺了这一对就是缺口（标明缺哪
        一边），绝不拿另一边的字顶替。同一通电话不能同时待在两座桥里；空的
        通话不能拿来搭。201 搭好；未知通话 404；空通话/同一通/已在活动桥里
        409。
        """
        bridge, result = store.create_bridge(body.left_call_id, body.right_call_id)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown call_id")
        if result == "empty":
            raise HTTPException(status_code=409, detail="call has no fragments")
        if result == "same_call":
            raise HTTPException(status_code=409, detail="a bridge needs two different calls")
        if result == "already_bridged":
            raise HTTPException(status_code=409, detail="call already in an active bridge")
        return bridge

    @app.get("/bridges")
    def list_bridges():
        """全部桥（搭着的和已拆的，按搭桥顺序）。活动桥对齐现算；已拆桥给
        拆桥那一刻的对齐快照。"""
        return {"bridges": store.list_bridges()}

    @app.get("/bridges/{bridge_no}")
    def get_bridge(bridge_no: str):
        """按桥号取一座桥：两边各是哪通、逐对对齐到哪、缺口缺哪一边。
        活动桥永远反映最新片段；已拆桥停在拆时快照。未知桥号 404。"""
        bridge = store.get_bridge(bridge_no)
        if bridge is None:
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        return bridge

    @app.post("/bridges/{bridge_no}/dismantle", status_code=201)
    @app.post(
        "/bridges/{bridge_no}/teardown", status_code=201, include_in_schema=False
    )
    def dismantle_bridge(bridge_no: str):
        """拆桥：两通电话恢复成各自独立的会话（片段一个不删、不改），同时把
        这座桥当时对齐到哪一对整体留痕。拆过的桥返回 409（原快照不动）；
        未知桥号 404。"""
        bridge, result = store.dismantle_bridge(bridge_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        if result == "already_dismantled":
            raise HTTPException(status_code=409, detail="bridge already dismantled")
        return bridge

    @app.post("/bridges/{bridge_no}/swap", status_code=201)
    def swap_bridge_side(bridge_no: str, body: BridgeSwapIn):
        """桥还搭着时把其中一边换成另一通已经有片段的电话。

        桥号不变，换完按新的两边重新对齐；被换下去的那通恢复自由（可再搭
        新桥）。换上来的不能是空通话（409）、不能已经在别的活动桥里（409）、
        也不能就是这座桥当前两边中的一通（409）；已拆掉的桥不能再换边（409）；
        未知桥号 404、未知通话 404。每次换边旧两边是谁、旧对齐到哪都单独
        留痕（GET /bridges/{桥号}/swaps），不被新两边盖掉。
        """
        bridge, result = store.swap_bridge_side(bridge_no, body.side, body.call_id)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        if result == "dismantled":
            raise HTTPException(status_code=409, detail="bridge already dismantled")
        if result == "unknown_call":
            raise HTTPException(status_code=404, detail="unknown call_id")
        if result == "empty":
            raise HTTPException(status_code=409, detail="call has no fragments")
        if result == "already_on_bridge":
            raise HTTPException(
                status_code=409,
                detail="incoming call is already a side of this bridge",
            )
        if result == "already_bridged":
            raise HTTPException(
                status_code=409, detail="call already in an active bridge"
            )
        return bridge

    @app.get("/bridges/{bridge_no}/swaps")
    def get_bridge_swaps(bridge_no: str):
        """这座桥历次换边的留痕（按换边先后）：每次换的是哪一边、谁被换下、
        谁换上来、另一边是谁、何时换，以及换边前旧两边的逐对对齐快照。
        没换过边返回空列表；未知桥号 404。"""
        swaps = store.get_bridge_swaps(bridge_no)
        if swaps is None:
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        return swaps

    # -------------------------------------------------------------- 桥拍照

    @app.post("/bridges/{bridge_no}/photos", status_code=201)
    def take_bridge_photo(bridge_no: str):
        """桥还搭着时，把此刻两边对到哪一对拍下来。

        同一座桥可以拍好几次：响应带 photo_seq（这座桥第几张，从 1 递增），
        每张都钉着拍照那一刻两边各是谁、逐对对齐到哪、缺口缺哪一边。拍照只
        新增记录：之后两通再来新段、缺口补齐，只让活动桥的现行对齐继续现算，
        这张照片一个字不变；拍照也不改两通各自的会话。已拆掉的桥不能再拍
        （409，拆时对齐另有拆桥快照留痕）；未知桥号 404。照片落库，服务重启
        后拍过的仍在。
        """
        photo, result = store.take_bridge_photo(bridge_no)
        if result == "unknown":
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        if result == "dismantled":
            raise HTTPException(status_code=409, detail="bridge already dismantled")
        return photo

    @app.get("/bridges/{bridge_no}/photos")
    def list_bridge_photos(bridge_no: str):
        """这座桥拍过的全部照片（按拍照先后）：每张看得出是第几张、何时拍、
        拍时两边各是谁、当时对到哪一对。拍完后再来的段碰不到旧照片。活动桥、
        已拆桥都能列（拆后不能再拍，但拆前拍过的照片照样查得到）。
        未知桥号 404。"""
        photos = store.list_bridge_photos(bridge_no)
        if photos is None:
            raise HTTPException(status_code=404, detail="unknown bridge_no")
        return photos

    @app.get("/bridges/{bridge_no}/photos/{photo_seq}")
    def get_bridge_photo(bridge_no: str, photo_seq: int):
        """取这座桥的某一张照片（按第几张）：永远是拍照那一刻的两边身份与
        逐对对齐，后来补段、换边、拆桥都不改它。没拍过这么多张/未知桥号
        404。"""
        photo = store.get_bridge_photo(bridge_no, photo_seq)
        if photo is None:
            raise HTTPException(status_code=404, detail="unknown bridge photo")
        return photo

    @app.get("/sessions/{call_id}/bridges")
    def list_bridges_for_call(call_id: str):
        """这通电话上过的全部桥（搭着的和拆掉的，按搭桥顺序），每座都看得出
        它在左边还是右边。查询带 call_id，两通电话的桥列不串。未知通话 404。"""
        bridges = store.list_bridges_for_call(call_id)
        if bridges is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return {"call_id": call_id, "bridges": bridges}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
