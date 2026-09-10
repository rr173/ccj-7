"""已发稿撤回（withdrawal）的测试：

- 拿稿号撤回；撤回记录和稿本身都看得出撤的是哪一稿；
- 撤回不改发稿当时钉住的正文和缺口；
- 已签收的稿不能撤；撤过的未签稿不能再签；
- 正在听的稿撤回后停在当前位置，不能继续推进或重新开始；
- 两通电话按各自稿号隔离，不能拿一通的号撤另一通；
- 撤回记录落 SQLite，重启后仍在。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.db"))) as c:
        yield c


def post(client, call_id, seq, text, is_last=False):
    r = client.post(
        "/fragments",
        json={"call_id": call_id, "seq": seq, "text": text, "is_last": is_last},
    )
    assert r.status_code == 200, r.text
    return r.json()


def issue(client, call_id):
    r = client.post(f"/sessions/{call_id}/drafts")
    assert r.status_code == 201, r.text
    return r.json()


def claim(client, draft_no, by="张三"):
    r = client.post(f"/drafts/{draft_no}/claim", json={"claimed_by": by})
    assert r.status_code in (200, 201), r.text
    return r.json()


def get_draft(client, draft_no):
    r = client.get(f"/drafts/{draft_no}")
    assert r.status_code == 200, r.text
    return r.json()


def withdraw(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/withdrawal")
    assert r.status_code == 201, r.text
    return r.json()


def receipt(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/receipt")
    assert r.status_code == 200, r.text
    return r.json()


def sign(client, draft_no):
    return client.post(f"/drafts/{draft_no}/receipt")


def start(client, draft_no):
    return client.post(f"/drafts/{draft_no}/playback")


def advance(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/playback/advance")
    assert r.status_code == 200, r.text
    return r.json()


def playback(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/playback")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 基本撤回

def test_withdraw_by_draft_no_and_record_shows_exact_draft(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)       # 缺第 2 段
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])                        # 先认领，才能撤

    w = withdraw(client, d1["draft_no"])

    assert w["draft_no"] == "C1-D0001"                    # 撤的是哪一稿
    assert w["call_id"] == "C1"
    assert w["draft_seq"] == 1
    assert w["withdrawn_at"]
    assert w["receipt_status"] == "withdrawn"
    # 带当时完整快照；撤回标记不改变快照正文和缺口
    assert w["draft"]["draft_no"] == d1["draft_no"]
    assert w["draft"]["content"] == d1["content"]
    assert w["draft"]["parts"] == d1["parts"]
    assert w["draft"]["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in w["draft"]["content"]

    fetched = client.get("/drafts/C1-D0001/withdrawal").json()
    assert fetched == w

    # 直接取稿和按通话列稿也看得出已撤回，但稿号仍是同一个
    marked = get_draft(client, d1["draft_no"])
    assert marked["is_withdrawn"] is True
    assert marked["withdrawn_at"] == w["withdrawn_at"]
    assert marked["draft_no"] == d1["draft_no"]

    listed = client.get("/sessions/C1/drafts").json()["drafts"]
    assert [(d["draft_no"], d["is_withdrawn"]) for d in listed] == [
        ("C1-D0001", True)
    ]
    assert client.get("/sessions/C1/withdrawals").json()["withdrawals"] == [w]


def test_withdrawal_does_not_modify_pinned_content_or_gaps(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)       # 缺第 3 段
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    w = withdraw(client, d1["draft_no"])
    post(client, "C1", 3, "信号不太好")                    # 撤回后缺口才补齐
    post(client, "C1", 2, "听得到吗")                     # 重传也不改稿

    marked = get_draft(client, d1["draft_no"])
    assert marked["is_withdrawn"] is True
    for field in (
        "draft_no", "call_id", "draft_seq", "status", "content", "parts",
        "gaps", "gap_history", "fragment_count", "version", "issued_at",
    ):
        assert marked[field] == d1[field]
    assert w["draft"]["content"] == d1["content"]
    assert w["draft"]["parts"] == d1["parts"]
    assert d1["status"] == "incomplete"
    assert d1["gaps"] == [[3, 3]]
    assert "[缺口:片段3]" in d1["content"]
    assert client.get("/sessions/C1").json()["status"] == "complete"


def test_withdrawal_is_idempotent_conflict_not_second_record(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    first = withdraw(client, d1["draft_no"])

    again = client.post(f"/drafts/{d1['draft_no']}/withdrawal")
    assert again.status_code == 409
    assert client.get(f"/drafts/{d1['draft_no']}/withdrawal").json() == first


# ------------------------------------------------------------- 签收互斥

def test_signed_draft_cannot_be_withdrawn(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    signed = sign(client, d1["draft_no"])
    assert signed.status_code == 201

    r = client.post(f"/drafts/{d1['draft_no']}/withdrawal")
    assert r.status_code == 409
    assert client.get(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 404
    assert get_draft(client, d1["draft_no"])["is_withdrawn"] is False
    assert receipt(client, d1["draft_no"])["status"] == "signed"


def test_withdrawn_unsigned_draft_cannot_be_signed(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "结束", is_last=True)
    d1 = issue(client, "C1")
    before = receipt(client, d1["draft_no"])
    assert before["status"] == "pending"

    claim(client, d1["draft_no"])
    withdraw(client, d1["draft_no"])

    r = sign(client, d1["draft_no"])
    assert r.status_code == 409
    after = receipt(client, d1["draft_no"])
    assert after["status"] == "withdrawn"
    assert after["signed_at"] is None
    assert after["withdrawn_at"] is not None
    assert after["draft"]["content"] == d1["content"]
    assert after["draft"]["parts"] == d1["parts"]
    assert after["draft"]["gaps"] == d1["gaps"]

    listed = client.get("/sessions/C1/receipts").json()["receipts"]
    assert [(x["draft_no"], x["status"]) for x in listed] == [
        ("C1-D0001", "withdrawn")
    ]


def test_unknown_withdrawal_target_returns_404(client):
    assert client.post("/drafts/nope-D0001/withdrawal").status_code == 404
    assert client.get("/drafts/nope-D0001/withdrawal").status_code == 404
    post(client, "C1", 1, "你好", is_last=True)
    assert client.get("/sessions/C1/withdrawals").status_code == 200
    assert client.get("/sessions/nope/withdrawals").status_code == 404


# ------------------------------------------------------------- 正在听则停住

def test_withdrawal_freezes_active_playback_at_current_position(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    assert start(client, d1["draft_no"]).status_code == 201
    advance(client, d1["draft_no"])                      # 已听到第 1 段
    withdraw(client, d1["draft_no"])

    pb = playback(client, d1["draft_no"])
    assert pb["status"] == "withdrawn"
    assert pb["position"] == 1                           # 听到哪停在哪
    assert pb["heard"] is None
    assert pb["next"] is None
    assert pb["finished_at"] is None
    assert pb["draft"]["content"] == d1["content"]
    assert pb["draft"]["parts"] == d1["parts"]
    assert pb["draft"]["gaps"] == d1["gaps"]

    again = advance(client, d1["draft_no"])
    assert again["status"] == "withdrawn"
    assert again["position"] == 1                        # 不能再往下听
    assert again["heard"] is None

    restart = start(client, d1["draft_no"])
    assert restart.status_code == 409                    # 也不能重新开始


def test_withdraw_before_playback_starts_then_playback_cannot_start(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    withdraw(client, d1["draft_no"])

    assert start(client, d1["draft_no"]).status_code == 409
    # 没有进度行：GET 也明确是 withdrawn，而不是普通 not_started
    pb = playback(client, d1["draft_no"])
    assert pb["status"] == "withdrawn"
    assert pb["position"] == 0


# ------------------------------------------------------------- 两通电话不串

def test_withdrawals_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 2, "甲第二句", is_last=True)
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 2, "乙第二句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")
    claim(client, da["draft_no"])
    claim(client, db["draft_no"])

    w = withdraw(client, da["draft_no"])
    assert w["call_id"] == "call-A"
    assert w["draft"]["content"] == "甲第一句\n甲第二句"

    # 乙完全不受影响：未撤、仍可签、可继续听
    assert get_draft(client, db["draft_no"])["is_withdrawn"] is False
    assert receipt(client, db["draft_no"])["status"] == "pending"
    assert sign(client, db["draft_no"]).status_code == 201
    assert start(client, db["draft_no"]).status_code == 201
    assert advance(client, db["draft_no"])["status"] == "playing"

    assert client.post("/drafts/call-B-D0001/withdrawal").status_code == 409
    assert client.get("/drafts/call-A-D0001/withdrawal").status_code == 200
    assert client.get("/drafts/call-B-D0001/withdrawal").status_code == 404

    assert [x["draft_no"] for x in
            client.get("/sessions/call-A/withdrawals").json()["withdrawals"]] == [
        "call-A-D0001"
    ]
    assert client.get("/sessions/call-B/withdrawals").json()["withdrawals"] == []


# ------------------------------------------------------------- 重启不丢

def test_withdrawals_survive_restart_and_remain_terminal(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "结束", is_last=True)
        d1 = issue(c1, "C1")
        claim(c1, d1["draft_no"])
        start(c1, d1["draft_no"])
        advance(c1, d1["draft_no"])
        w1 = withdraw(c1, d1["draft_no"])

    with TestClient(create_app(db)) as c2:
        assert c2.get(f"/drafts/{d1['draft_no']}/withdrawal").json() == w1
        marked = get_draft(c2, d1["draft_no"])
        assert marked["is_withdrawn"] is True
        assert marked["gaps"] == [[2, 2]]
        assert marked["content"] == d1["content"]

        assert sign(c2, d1["draft_no"]).status_code == 409

        pb = playback(c2, d1["draft_no"])
        assert pb["status"] == "withdrawn"
        assert pb["position"] == 1
        assert advance(c2, d1["draft_no"])["position"] == 1
