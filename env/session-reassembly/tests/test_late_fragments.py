"""迟到片段（late fragments）的测试：

- 稿发出去之后才到的片段：已发的稿不改、待签不动、停在缺口上的回放不突然听完；
- 迟到的段单独记录，看得出补在哪一稿后面（到达时最新的一稿）；
- 发稿之前到的片段、内容相同的重传都不算迟到，不进记录；
- 两通电话的迟到记录不串；
- 记录落 SQLite，重启后还在。
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


def late(client, call_id):
    r = client.get(f"/sessions/{call_id}/late-fragments")
    assert r.status_code == 200, r.text
    return r.json()["late_fragments"]


def late_for(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/late-fragments")
    assert r.status_code == 200, r.text
    return r.json()["late_fragments"]


# ------------------------------------------------------------- 单独记录

def test_fragment_arriving_after_draft_is_recorded_against_that_draft(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")                          # 带缺口给出

    r = post(client, "C1", 2, "听得到吗")             # 稿发出后才到
    assert r["ingest"]["late"] is True                # 接收结果直接标出迟到

    records = late(client, "C1")
    assert len(records) == 1
    rec = records[0]
    assert rec["call_id"] == "C1"
    assert rec["seq"] == 2
    assert rec["text"] == "听得到吗"
    assert rec["after_draft_no"] == "C1-D0001"        # 看得出补在哪一稿后面
    assert rec["after_draft_seq"] == 1
    assert rec["arrived_at"] >= d1["issued_at"]

    # 按稿号查，拿到的是同一笔
    assert late_for(client, "C1-D0001") == records


def test_fragments_before_first_draft_and_retransmissions_are_not_late(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    assert late(client, "C1") == []                   # 还没发过稿，没有迟到

    issue(client, "C1")
    r = post(client, "C1", 1, "你好")                 # 内容相同的重传：没带来新东西
    assert r["ingest"]["duplicate"] is True
    assert r["ingest"]["late"] is False
    assert late(client, "C1") == []

    post(client, "C1", 3, "补充一句")                 # 新片段才算迟到
    assert [x["seq"] for x in late(client, "C1")] == [3]


def test_late_record_follows_the_latest_draft(client):
    post(client, "C1", 1, "一")
    post(client, "C1", 3, "三")                       # 缺第 2 段
    issue(client, "C1")                               # D0001

    post(client, "C1", 2, "二")                       # 补在 D0001 后面
    d2 = issue(client, "C1")                          # D0002
    post(client, "C1", 4, "四")                       # 补在 D0002 后面

    records = late(client, "C1")
    assert [(x["seq"], x["after_draft_no"]) for x in records] == [
        (2, "C1-D0001"),
        (4, "C1-D0002"),
    ]
    assert [x["seq"] for x in late_for(client, "C1-D0001")] == [2]
    assert [x["seq"] for x in late_for(client, d2["draft_no"])] == [4]


# ------------------------------------------------------------- 什么也不改

def test_late_fragment_changes_neither_draft_receipt_nor_blocked_playback(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")

    client.post(f"/drafts/{d1['draft_no']}/playback")
    client.post(f"/drafts/{d1['draft_no']}/playback/advance")      # 听到片段 1
    blocked = client.post(f"/drafts/{d1['draft_no']}/playback/advance").json()
    assert blocked["status"] == "blocked"             # 停在当时的缺口 [2,2]

    post(client, "C1", 2, "听得到吗")                 # 迟到的段到了
    assert late(client, "C1")[0]["after_draft_no"] == d1["draft_no"]

    # 已发出的稿一个字没改
    assert client.get(f"/drafts/{d1['draft_no']}").json() == d1
    # 待签没被动过
    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt").json()
    assert receipt["status"] == "pending"
    assert receipt["signed_at"] is None
    assert receipt["draft"] == d1
    # 停在缺口上的回放没有突然听完：仍 blocked、位置不动、再推也不过去
    pb = client.get(f"/drafts/{d1['draft_no']}/playback").json()
    assert pb["status"] == "blocked"
    assert pb["position"] == 1
    assert pb["next"]["gap"] == [2, 2]
    again = client.post(f"/drafts/{d1['draft_no']}/playback/advance").json()
    assert again["status"] == "blocked"
    assert again["position"] == 1


# ------------------------------------------------------------- 两通电话不串

def test_late_records_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 2, "甲第二句", is_last=True)
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 2, "乙第二句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")

    post(client, "call-A", 3, "甲补充")
    post(client, "call-B", 3, "乙补充")

    la = late(client, "call-A")
    lb = late(client, "call-B")
    assert [(x["seq"], x["text"], x["after_draft_no"]) for x in la] == [
        (3, "甲补充", da["draft_no"])]
    assert [(x["seq"], x["text"], x["after_draft_no"]) for x in lb] == [
        (3, "乙补充", db["draft_no"])]
    # 按稿号查也各回各家，正文互不混入
    assert late_for(client, da["draft_no"])[0]["text"] == "甲补充"
    assert late_for(client, db["draft_no"])[0]["text"] == "乙补充"


# ------------------------------------------------------------- 重启不丢

def test_late_records_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "那就这样", is_last=True)
        issue(c1, "C1")
        post(c1, "C1", 2, "听得到吗")                 # 迟到，记在 D0001 后面
        assert len(late(c1, "C1")) == 1

    with TestClient(create_app(db)) as c2:            # 同一数据库重新拉起
        records = late(c2, "C1")                      # 记录还在
        assert len(records) == 1
        assert records[0]["seq"] == 2
        assert records[0]["after_draft_no"] == "C1-D0001"
        assert late_for(c2, "C1-D0001") == records

        post(c2, "C1", 4, "重启后又到一段")            # 重启后继续记
        assert [x["seq"] for x in late(c2, "C1")] == [2, 4]


# ------------------------------------------------------------- 杂项

def test_unknown_call_or_draft_returns_404(client):
    assert client.get("/sessions/nope/late-fragments").status_code == 404
    assert client.get("/drafts/nope-D0001/late-fragments").status_code == 404

    post(client, "C1", 1, "你好")
    r = client.get("/sessions/C1/late-fragments")     # 通话在、没发过稿：空表
    assert r.status_code == 200
    assert r.json()["late_fragments"] == []
