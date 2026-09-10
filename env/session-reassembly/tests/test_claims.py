"""认领（claim）的测试：

- 发出去的稿要先有人认领：没认领不能签、不能听、也不能撤（409）；
- 一个人认了，别人不能再认走（409）；本人重复认领幂等，时间不动；
- 交出去（release）之后才能换人认；交出后没人认期间又回到不能签/听/撤；
- 认领不改那一稿当时的正文和缺口，也不动待签和回放；
- 两通电话的认领不串：拿一通的号认不到另一通的稿；
- 认领记录落 SQLite，重启后认了谁还在。
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
    assert r.status_code == 201, r.text
    return r.json()


def get_claim(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/claim")
    assert r.status_code == 200, r.text
    return r.json()


def release(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/claim/release")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 没认领不行

def test_unclaimed_draft_cannot_be_signed_played_or_withdrawn(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")

    # 没认领：签、听、撤全都 409，且都不产生任何效果
    assert client.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/playback").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 409

    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt").json()
    assert receipt["status"] == "pending"              # 待签原样欠着
    assert client.get(f"/drafts/{d1['draft_no']}/playback").status_code == 409
    assert client.get(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 404

    c = get_claim(client, d1["draft_no"])
    assert c["status"] == "unclaimed"
    assert c["claimed_by"] is None
    assert c["history"] == []


def test_claimed_draft_can_be_signed_played_and_withdrawn(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"])

    assert client.post(f"/drafts/{d1['draft_no']}/playback").status_code == 201
    assert client.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 201
    # 已签的稿不能撤是既有规则；换一稿验证认领后能撤
    d2 = issue(client, "C1")
    claim(client, d2["draft_no"])
    assert client.post(f"/drafts/{d2['draft_no']}/withdrawal").status_code == 201


# ------------------------------------------------------------- 认领

def test_claim_by_draft_no_and_view_shows_who_holds_it(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)     # 缺第 2 段
    d1 = issue(client, "C1")

    c = claim(client, d1["draft_no"], by="张三")
    assert c["draft_no"] == "C1-D0001"                  # 认的是哪一稿
    assert c["call_id"] == "C1"
    assert c["draft_seq"] == 1
    assert c["status"] == "claimed"
    assert c["claimed_by"] == "张三"
    assert c["claimed_at"] is not None
    assert c["draft"] == d1                             # 当时那一稿原样嵌着

    again = get_claim(client, d1["draft_no"])
    assert again == c

    listed = client.get("/sessions/C1/claims").json()["claims"]
    assert [x["draft_no"] for x in listed] == ["C1-D0001"]
    assert listed[0]["claimed_by"] == "张三"


def test_one_holder_others_cannot_take_it_away(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    first = claim(client, d1["draft_no"], by="张三")

    # 别人来认：409，认不走
    r = client.post(f"/drafts/{d1['draft_no']}/claim", json={"claimed_by": "李四"})
    assert r.status_code == 409
    c = get_claim(client, d1["draft_no"])
    assert c["claimed_by"] == "张三"                    # 还是张三的
    assert c["claimed_at"] == first["claimed_at"]       # 时间不动
    assert len(c["history"]) == 1                       # 没多出认领记录

    # 本人重复认领：幂等 200，时间不动
    r = client.post(f"/drafts/{d1['draft_no']}/claim", json={"claimed_by": "张三"})
    assert r.status_code == 200
    assert r.json()["claimed_at"] == first["claimed_at"]
    assert len(get_claim(client, d1["draft_no"])["history"]) == 1


def test_release_then_someone_else_can_claim(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    claim(client, d1["draft_no"], by="张三")

    # 没交出去之前，别人认不走
    assert client.post(
        f"/drafts/{d1['draft_no']}/claim", json={"claimed_by": "李四"}
    ).status_code == 409

    released = release(client, d1["draft_no"])          # 张三交出去
    assert released["status"] == "unclaimed"
    assert released["claimed_by"] is None

    # 交出后、还没人认期间：又不能签/听/撤了
    assert client.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/playback").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/withdrawal").status_code == 409

    c = claim(client, d1["draft_no"], by="李四")        # 换人认
    assert c["claimed_by"] == "李四"

    history = get_claim(client, d1["draft_no"])["history"]
    assert len(history) == 2                            # 两次认领都留痕
    assert history[0]["claimed_by"] == "张三"
    assert history[0]["released_at"] is not None        # 第一段已交出
    assert history[1]["claimed_by"] == "李四"
    assert history[1]["released_at"] is None            # 当前李四持有

    # 李四认了之后能正常签收
    assert client.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 201


def test_release_without_claim_is_409_unknown_is_404(client):
    assert client.post("/drafts/nope-D0001/claim",
                       json={"claimed_by": "张三"}).status_code == 404
    assert client.post("/drafts/nope-D0001/claim/release").status_code == 404
    assert client.get("/drafts/nope-D0001/claim").status_code == 404
    assert client.get("/sessions/nope/claims").status_code == 404

    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    # 从来没认过：无可交
    assert client.post(f"/drafts/{d1['draft_no']}/claim/release").status_code == 409
    # 认了再交、交完再交：第二次也是 409
    claim(client, d1["draft_no"])
    release(client, d1["draft_no"])
    assert client.post(f"/drafts/{d1['draft_no']}/claim/release").status_code == 409


# ------------------------------------------------------------- 认领不改稿

def test_claim_changes_neither_draft_nor_receipt_nor_playback(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)     # 缺第 2 段时发稿
    d1 = issue(client, "C1")
    before = client.get(f"/drafts/{d1['draft_no']}").json()

    claim(client, d1["draft_no"], by="张三")
    release(client, d1["draft_no"])
    claim(client, d1["draft_no"], by="李四")

    # 认领、交出、换人认 —— 那一稿当时的正文和缺口一个字不动
    after = client.get(f"/drafts/{d1['draft_no']}").json()
    assert after == before == d1
    assert after["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in after["content"]

    # 待签没被动过
    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt").json()
    assert receipt["status"] == "pending"
    assert receipt["signed_at"] is None
    assert receipt["draft"] == d1

    # 认领后开的回放照常停在当时的缺口，认领动作碰不到它
    assert client.post(f"/drafts/{d1['draft_no']}/playback").status_code == 201
    client.post(f"/drafts/{d1['draft_no']}/playback/advance")
    blocked = client.post(f"/drafts/{d1['draft_no']}/playback/advance").json()
    assert blocked["status"] == "blocked"
    assert blocked["next"]["gap"] == [2, 2]


# ------------------------------------------------------------- 两通电话不串

def test_claims_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 2, "甲第二句", is_last=True)
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 2, "乙第二句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")

    claim(client, da["draft_no"], by="张三")            # 只认甲的

    # 乙的稿还是没人认：拿甲的号认不到乙的稿
    cb = get_claim(client, db["draft_no"])
    assert cb["status"] == "unclaimed"
    assert client.post(f"/drafts/{db['draft_no']}/receipt").status_code == 409
    assert client.post(f"/drafts/{db['draft_no']}/playback").status_code == 409

    # 各回各的认领列表
    la = client.get("/sessions/call-A/claims").json()["claims"]
    lb = client.get("/sessions/call-B/claims").json()["claims"]
    assert [(x["draft_no"], x["claimed_by"]) for x in la] == [
        ("call-A-D0001", "张三")
    ]
    assert [(x["draft_no"], x["claimed_by"]) for x in lb] == [
        ("call-B-D0001", None)
    ]

    # 甲的认领、签收一路走通，完全碰不到乙
    assert client.post(f"/drafts/{da['draft_no']}/receipt").status_code == 201
    assert client.get(f"/drafts/{db['draft_no']}/receipt").json()["status"] == "pending"


# ------------------------------------------------------------- 重启不丢

def test_claims_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 2, "听得到吗", is_last=True)
        d1 = issue(c1, "C1")
        held = claim(c1, d1["draft_no"], by="张三")
        d2 = issue(c1, "C1")                            # 另一稿始终没人认

    with TestClient(create_app(db)) as c2:              # 同一数据库重新拉起
        c = get_claim(c2, d1["draft_no"])               # 认了谁还在
        assert c["status"] == "claimed"
        assert c["claimed_by"] == "张三"
        assert c["claimed_at"] == held["claimed_at"]

        # 别人还是认不走；本人能直接签收
        assert c2.post(f"/drafts/{d1['draft_no']}/claim",
                       json={"claimed_by": "李四"}).status_code == 409
        assert c2.post(f"/drafts/{d1['draft_no']}/receipt").status_code == 201

        # 没认过的那稿，重启后依然不能签
        assert c2.post(f"/drafts/{d2['draft_no']}/receipt").status_code == 409
