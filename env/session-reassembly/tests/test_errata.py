"""勘误（errata）的测试：

- 发出去的稿按稿号出勘误，看得出是哪一稿、对着哪一段、改成什么；
- 勘误只新增记录，不改那一稿当时的正文和缺口，也不动待签和回放；
- 没人认领的不能出（交出后没人认期间同样不能出）；
- 同一段不能出两次（409，原记录时间不动）；
- 对着的段必须在这一稿里：缺口不是段、越出该稿范围的序号也不是（409）；
- 撤过的稿不能出；已签收、已投下游的稿照样能出（发出去的稿要能出勘误）；
- 两通电话的勘误不串：拿一通的号给另一通出不了勘误；
- 勘误记录落 SQLite，重启后出过的勘误还在。
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


def errata(client, draft_no, seq, new_text):
    r = client.post(f"/drafts/{draft_no}/errata",
                    json={"seq": seq, "new_text": new_text})
    assert r.status_code == 201, r.text
    return r.json()


def get_draft(client, draft_no):
    r = client.get(f"/drafts/{draft_no}")
    assert r.status_code == 200, r.text
    return r.json()


def get_errata(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/errata")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 按稿号出

def test_issue_errata_by_draft_no_shows_what_changed(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    e = errata(client, d1["draft_no"], 2, "听不清")
    assert e["draft_no"] == "C1-D0001"                  # 勘的是哪一稿
    assert e["call_id"] == "C1"
    assert e["draft_seq"] == 1
    assert e["seq"] == 2                                # 对着哪一段
    assert e["old_text"] == "听得到吗"                   # 当时的原文
    assert e["new_text"] == "听不清"                     # 改成什么
    assert e["issued_at"] is not None
    assert e["draft"] == d1                             # 当时那一稿原样嵌着

    # 按稿查：这一稿出过的勘误都在，按段序
    got = get_errata(client, d1["draft_no"])
    assert got["draft_no"] == "C1-D0001"
    assert [(x["seq"], x["old_text"], x["new_text"]) for x in got["errata"]] == [
        (2, "听得到吗", "听不清")
    ]
    assert got["draft"] == d1

    # 没出过勘误的稿：空列表
    d2 = issue(client, "C1")
    assert get_errata(client, d2["draft_no"])["errata"] == []

    # 按通话查：该通话出过的全部勘误
    listed = client.get("/sessions/C1/errata").json()["errata"]
    assert [(x["draft_no"], x["seq"], x["new_text"]) for x in listed] == [
        ("C1-D0001", 2, "听不清")
    ]


def test_errata_unknown_draft_or_call_is_404(client):
    assert client.post("/drafts/nope-D0001/errata",
                       json={"seq": 1, "new_text": "x"}).status_code == 404
    assert client.get("/drafts/nope-D0001/errata").status_code == 404
    assert client.get("/sessions/nope/errata").status_code == 404


# ------------------------------------------------------------- 勘误不改稿

def test_errata_never_changes_draft_snapshot(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)         # 缺第 2 段时发稿
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    errata(client, d1["draft_no"], 1, "您好")
    errata(client, d1["draft_no"], 3, "先这样")

    # 那一稿当时的正文和缺口一个字不变
    after = get_draft(client, d1["draft_no"])
    assert after == d1
    assert after["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in after["content"]
    assert "您好" not in after["content"]               # 改正文只进勘误记录

    # 待签原样欠着，没被勘误动过
    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt").json()
    assert receipt["status"] == "pending"
    assert receipt["signed_at"] is None
    assert receipt["draft"] == d1

    # 停在缺口上的回放不受影响：仍停在当时的缺口前
    assert client.post(f"/drafts/{d1['draft_no']}/playback").status_code == 201
    client.post(f"/drafts/{d1['draft_no']}/playback/advance")
    blocked = client.post(f"/drafts/{d1['draft_no']}/playback/advance").json()
    assert blocked["status"] == "blocked"
    assert blocked["next"]["gap"] == [2, 2]


# ------------------------------------------------------------- 没人认领不能出

def test_unclaimed_draft_cannot_issue_errata(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")

    r = client.post(f"/drafts/{d1['draft_no']}/errata",
                    json={"seq": 1, "new_text": "您好"})
    assert r.status_code == 409
    assert get_errata(client, d1["draft_no"])["errata"] == []   # 没出成

    # 交出后、还没人认期间同样不能出
    claim(client, d1["draft_no"])
    client.post(f"/drafts/{d1['draft_no']}/claim/release")
    assert client.post(f"/drafts/{d1['draft_no']}/errata",
                       json={"seq": 1, "new_text": "您好"}).status_code == 409
    assert get_errata(client, d1["draft_no"])["errata"] == []

    # 有人认领之后就能出
    claim(client, d1["draft_no"], by="李四")
    assert errata(client, d1["draft_no"], 1, "您好")["new_text"] == "您好"


# ------------------------------------------------------------- 同一段不能出两次

def test_same_segment_cannot_be_corrected_twice(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    first = errata(client, d1["draft_no"], 2, "听不清")

    # 同一段再出一次：409，原记录原样还在、时间不动
    r = client.post(f"/drafts/{d1['draft_no']}/errata",
                    json={"seq": 2, "new_text": "又改"})
    assert r.status_code == 409
    got = get_errata(client, d1["draft_no"])["errata"]
    assert len(got) == 1
    assert got[0]["new_text"] == "听不清"
    assert got[0]["issued_at"] == first["issued_at"]

    # 别的段不受影响，照出
    assert errata(client, d1["draft_no"], 1, "您好")["seq"] == 1
    assert [x["seq"] for x in get_errata(client, d1["draft_no"])["errata"]] == [1, 2]

    # 另一稿的同一段是另一段：各出各的，互不冲突
    d2 = issue(client, "C1")
    claim(client, d2["draft_no"])
    assert errata(client, d2["draft_no"], 2, "听得见")["draft_no"] == d2["draft_no"]


# ------------------------------------------------------------- 对着的段得在稿里

def test_segment_must_exist_in_that_draft(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)         # 缺第 2 段
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    # 缺口不是段：不能对着 [缺口:片段2] 出勘误
    assert client.post(f"/drafts/{d1['draft_no']}/errata",
                       json={"seq": 2, "new_text": "补上"}).status_code == 409
    # 越出该稿范围的序号也不是这一稿的段
    assert client.post(f"/drafts/{d1['draft_no']}/errata",
                       json={"seq": 4, "new_text": "没有"}).status_code == 409
    assert get_errata(client, d1["draft_no"])["errata"] == []

    # 稿里真实存在的段照出
    assert errata(client, d1["draft_no"], 3, "先这样")["old_text"] == "那就这样"


# ------------------------------------------------------------- 与生命周期共存

def test_withdrawn_draft_cannot_issue_errata(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])
    assert client.post(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 201

    # 撤过的稿不能出勘误
    assert client.post(f"/drafts/{d1['draft_no']}/errata",
                       json={"seq": 1, "new_text": "您好"}).status_code == 409
    assert get_errata(client, d1["draft_no"])["errata"] == []


def test_signed_or_delivered_draft_can_still_issue_errata(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    # 已签收的稿：照样能出勘误（发出去的稿要能出勘误）
    assert client.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 201
    assert errata(client, d1["draft_no"], 1, "您好")["new_text"] == "您好"

    # 已投下游并被收下的稿：照样能出
    d2 = issue(client, "C1")
    claim(client, d2["draft_no"])
    client.post(f"/drafts/{d2['draft_no']}/delivery")
    client.post(f"/drafts/{d2['draft_no']}/delivery/accept")
    assert errata(client, d2["draft_no"], 1, "嗨")["draft_no"] == d2["draft_no"]


# ------------------------------------------------------------- 两通电话不串

def test_errata_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句", is_last=True)
    post(client, "call-B", 1, "乙第一句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")
    claim(client, da["draft_no"], by="张三")                # 只认甲的

    # 拿甲的号给乙出不了：乙的稿还没人认，乙的号自己也出不了
    assert client.post(f"/drafts/{db['draft_no']}/errata",
                       json={"seq": 1, "new_text": "改乙"}).status_code == 409

    errata(client, da["draft_no"], 1, "甲第一句改")          # 只勘了甲的

    # 各回各的勘误列表：甲的列表里没有乙，乙的列表是空
    la = client.get("/sessions/call-A/errata").json()["errata"]
    lb = client.get("/sessions/call-B/errata").json()["errata"]
    assert [(x["draft_no"], x["call_id"], x["new_text"]) for x in la] == [
        ("call-A-D0001", "call-A", "甲第一句改")
    ]
    assert lb == []

    # 乙后来自己认领、自己出：两条记录各挂各的稿号，互不串
    claim(client, db["draft_no"], by="李四")
    errata(client, db["draft_no"], 1, "乙第一句改")
    la = client.get("/sessions/call-A/errata").json()["errata"]
    lb = client.get("/sessions/call-B/errata").json()["errata"]
    assert [x["draft_no"] for x in la] == ["call-A-D0001"]
    assert [x["draft_no"] for x in lb] == ["call-B-D0001"]


# ------------------------------------------------------------- 重启不丢

def test_errata_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 2, "听得到吗", is_last=True)
        d1 = issue(c1, "C1")
        claim(c1, d1["draft_no"], by="张三")
        first = errata(c1, d1["draft_no"], 2, "听不清")

    with TestClient(create_app(db)) as c2:                  # 同一数据库重新拉起
        got = get_errata(c2, d1["draft_no"])["errata"]      # 出过的勘误还在
        assert len(got) == 1
        assert got[0]["seq"] == 2
        assert got[0]["old_text"] == "听得到吗"
        assert got[0]["new_text"] == "听不清"
        assert got[0]["issued_at"] == first["issued_at"]

        # 重启后同一段仍不能出第二次；那一稿的正文仍原样
        assert c2.post(f"/drafts/{d1['draft_no']}/errata",
                       json={"seq": 2, "new_text": "又改"}).status_code == 409
        assert get_draft(c2, d1["draft_no"]) == d1

        # 别的段还能接着出
        assert errata(c2, d1["draft_no"], 1, "您好")["seq"] == 1
