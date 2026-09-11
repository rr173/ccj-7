"""下游投递（delivery）的测试：

- 发出去的稿按稿号往下游投，投完看得出投的是哪一稿（稿号/通话/第几稿都在）；
- 没人认领的不能投；
- 投出去还没回音之前不能再投一次；下游可以收下，也可以退回；
- 退了之后才能再投（新一次 attempt，历次投递都留着）；
- 收下之后不能再退、也不能再投；
- 投递、退回、再投、收下都不改那一稿当时的正文和缺口；
- 两通电话的投递不串：拿一通的号投不到另一通的稿；
- 投递记录落 SQLite，重启后投到哪了还在；
- 稿已撤回不能投；投递待回音/已收下时稿不能撤回。
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


def deliver(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/delivery")
    assert r.status_code == 201, r.text
    return r.json()


def delivery(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/delivery")
    assert r.status_code == 200, r.text
    return r.json()


def get_draft(client, draft_no):
    r = client.get(f"/drafts/{draft_no}")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 按稿号投

def test_deliver_by_draft_no_and_view_shows_what_was_delivered(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    v = deliver(client, d1["draft_no"])
    assert v["draft_no"] == "C1-D0001"                  # 投的是哪一稿
    assert v["call_id"] == "C1"
    assert v["draft_seq"] == 1
    assert v["status"] == "pending"                     # 投出去，等回音
    assert v["attempt_count"] == 1
    assert v["current_attempt"] == 1
    assert v["delivered_at"] is not None
    assert v["accepted_at"] is None
    assert v["returned_at"] is None
    assert v["attempts"] == [
        {
            "attempt": 1,
            "delivered_at": v["delivered_at"],
            "accepted_at": None,
            "returned_at": None,
            "status": "pending",
        }
    ]
    # 投递记录嵌的就是发稿当时那份快照
    assert v["draft"] == d1

    # 没投过之前状态是 none
    d2 = issue(client, "C1")
    claim(client, d2["draft_no"])
    assert delivery(client, d2["draft_no"])["status"] == "none"
    assert delivery(client, d2["draft_no"])["attempts"] == []

    listed = client.get("/sessions/C1/deliveries").json()["deliveries"]
    assert [(x["draft_no"], x["status"]) for x in listed] == [
        ("C1-D0001", "pending"),
        ("C1-D0002", "none"),
    ]


# ------------------------------------------------------------- 没认领不能投

def test_unclaimed_draft_cannot_be_delivered(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")

    r = client.post(f"/drafts/{d1['draft_no']}/delivery")
    assert r.status_code == 409
    # 没产生任何投递
    assert delivery(client, d1["draft_no"])["status"] == "none"

    # 交出后、没人认期间同样不能投
    claim(client, d1["draft_no"])
    client.post(f"/drafts/{d1['draft_no']}/claim/release")
    assert client.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409
    assert delivery(client, d1["draft_no"])["status"] == "none"


# ------------------------------------------------------------- 没回音不能再投

def test_cannot_deliver_twice_while_awaiting_response(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    first = deliver(client, d1["draft_no"])

    # 还没回音，再投一次：409，仍是原来那次投递，时间不动
    r = client.post(f"/drafts/{d1['draft_no']}/delivery")
    assert r.status_code == 409
    v = delivery(client, d1["draft_no"])
    assert v["status"] == "pending"
    assert v["attempt_count"] == 1
    assert v["delivered_at"] == first["delivered_at"]


# ------------------------------------------------------------- 下游收下 / 退回

def test_downstream_can_accept_then_no_more_actions(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    deliver(client, d1["draft_no"])

    r = client.post(f"/drafts/{d1['draft_no']}/delivery/accept")
    assert r.status_code == 200
    v = r.json()
    assert v["status"] == "accepted"
    assert v["accepted_at"] is not None
    assert v["returned_at"] is None
    assert v["current_attempt"] == 1

    # 收下是终态：不能再退、不能再投
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/return").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409
    v = delivery(client, d1["draft_no"])
    assert v["status"] == "accepted"
    assert v["attempt_count"] == 1

    # 重复收下同样 409，首次收下时间不动
    accepted_at = v["accepted_at"]
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/accept").status_code == 409
    assert delivery(client, d1["draft_no"])["accepted_at"] == accepted_at


def test_downstream_can_return_then_redeliver_once(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    first = deliver(client, d1["draft_no"])

    r = client.post(f"/drafts/{d1['draft_no']}/delivery/return")
    assert r.status_code == 200
    v = r.json()
    assert v["status"] == "returned"
    assert v["returned_at"] is not None
    assert v["accepted_at"] is None

    # 退了之后才能再投：新起一次，attempt=2；待回音期间又不能再投
    again = deliver(client, d1["draft_no"])
    assert again["status"] == "pending"
    assert again["current_attempt"] == 2
    assert again["attempt_count"] == 2
    assert client.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409

    # 历次投递都留着：第一次退回、第二次待回音，看得出来龙去脉
    v = delivery(client, d1["draft_no"])
    assert [a["status"] for a in v["attempts"]] == ["returned", "pending"]
    assert v["attempts"][0]["delivered_at"] == first["delivered_at"]
    assert v["attempts"][0]["returned_at"] is not None
    assert v["attempts"][1]["accepted_at"] is None

    # 第二次被收下：终态，不能再退、不能再投
    client.post(f"/drafts/{d1['draft_no']}/delivery/accept")
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/return").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409
    assert delivery(client, d1["draft_no"])["status"] == "accepted"


def test_answer_without_pending_delivery_is_409_unknown_is_404(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    # 还没投过，下游无从答复
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/accept").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/return").status_code == 409

    deliver(client, d1["draft_no"])
    client.post(f"/drafts/{d1['draft_no']}/delivery/return")
    # 已退回（不在等回音），也不能再答复
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/accept").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/delivery/return").status_code == 409

    # 未知稿号一律 404
    assert client.post("/drafts/nope-D0001/delivery").status_code == 404
    assert client.post("/drafts/nope-D0001/delivery/accept").status_code == 404
    assert client.post("/drafts/nope-D0001/delivery/return").status_code == 404
    assert client.get("/drafts/nope-D0001/delivery").status_code == 404
    assert client.get("/sessions/nope/deliveries").status_code == 404


# ------------------------------------------------------------- 投递不改稿

def test_delivery_never_changes_draft_snapshot(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)         # 缺第 2 段时发稿
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    deliver(client, d1["draft_no"])
    client.post(f"/drafts/{d1['draft_no']}/delivery/return")
    deliver(client, d1["draft_no"])
    client.post(f"/drafts/{d1['draft_no']}/delivery/accept")

    # 投、退、再投、收 —— 那一稿当时的正文和缺口一个字不变
    after = get_draft(client, d1["draft_no"])
    assert after == d1
    assert after["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in after["content"]


# ------------------------------------------------------------- 两通电话不串

def test_deliveries_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句", is_last=True)
    post(client, "call-B", 1, "乙第一句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")
    claim(client, da["draft_no"], by="张三")

    # 拿甲的号投不到乙的稿：乙还没人认
    assert client.post(f"/drafts/{db['draft_no']}/delivery").status_code == 409

    deliver(client, da["draft_no"])                          # 只投了甲的
    assert client.post(
        f"/drafts/{db['draft_no']}/delivery/accept"
    ).status_code == 409                                     # 乙的号、甲也代答不了

    # 各回各的投递列表
    la = client.get("/sessions/call-A/deliveries").json()["deliveries"]
    lb = client.get("/sessions/call-B/deliveries").json()["deliveries"]
    assert [(x["draft_no"], x["status"]) for x in la] == [
        ("call-A-D0001", "pending")
    ]
    assert [(x["draft_no"], x["status"]) for x in lb] == [
        ("call-B-D0001", "none")
    ]

    # 甲收下，完全碰不到乙
    assert client.post(f"/drafts/{da['draft_no']}/delivery/accept").status_code == 200
    assert delivery(client, db["draft_no"])["status"] == "none"


# ------------------------------------------------------------- 与撤回互斥

def test_withdrawn_draft_cannot_be_delivered(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    r = client.post(f"/drafts/{d1['draft_no']}/withdrawal")
    assert r.status_code == 201

    # 撤过的稿不能投
    assert client.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409
    assert delivery(client, d1["draft_no"])["status"] == "none"


def test_pending_or_accepted_delivery_blocks_withdrawal(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    # 投出去还没回音：不能撤
    deliver(client, d1["draft_no"])
    assert client.post(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 409

    # 下游退回后，可以撤
    client.post(f"/drafts/{d1['draft_no']}/delivery/return")
    assert client.post(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 201

    # 另一稿：收下后不能撤
    d2 = issue(client, "C1")
    claim(client, d2["draft_no"])
    deliver(client, d2["draft_no"])
    client.post(f"/drafts/{d2['draft_no']}/delivery/accept")
    assert client.post(f"/drafts/{d2['draft_no']}/withdrawal").status_code == 409


# ------------------------------------------------------------- 重启不丢

def test_deliveries_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 2, "听得到吗", is_last=True)
        d1 = issue(c1, "C1")
        claim(c1, d1["draft_no"], by="张三")
        deliver(c1, d1["draft_no"])                         # 投出去，还在等回音
        assert client_get(c1, d1["draft_no"])["status"] == "pending"

    with TestClient(create_app(db)) as c2:                 # 同一数据库重新拉起
        v = client_get(c2, d1["draft_no"])                 # 投到哪了还在
        assert v["status"] == "pending"
        assert v["attempt_count"] == 1
        assert v["draft"] == d1

        # 没回音前重启后仍不能再投；下游这时收下即终态
        assert c2.post(f"/drafts/{d1['draft_no']}/delivery").status_code == 409
        assert c2.post(f"/drafts/{d1['draft_no']}/delivery/accept").status_code == 200
        assert client_get(c2, d1["draft_no"])["status"] == "accepted"


def client_get(c, draft_no):
    r = c.get(f"/drafts/{draft_no}/delivery")
    assert r.status_code == 200, r.text
    return r.json()
