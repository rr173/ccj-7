"""两稿对照（comparisons）的测试：

- 发出去的两份稿按两个稿号出对照单，看得出是哪两稿、哪一段、两边当时各是什么；
- 对照单只记对不上的段：两边同文、两边都缺的段不进单；
- 对照只读两份发稿快照，不改那两稿当时的正文和缺口，也不动待签/回放/勘误；
- 不是同一通电话的稿不能对（409）；一稿不能和自己对（409）；
- 同一对稿不能对两次（409，原单时间不动；与请求里两稿先后无关）；
- 两份完全一致时，对照单对不上的段数为 0（仍算出过一次单，不能再对）；
- 两通电话的对照不串；按通话、按稿都列得出；
- 对照记录落 SQLite，重启后对照过的还在。
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


def compare(client, no_a, no_b):
    r = client.post(f"/drafts/{no_a}/compare/{no_b}")
    assert r.status_code == 201, r.text
    return r.json()


def get_draft(client, draft_no):
    r = client.get(f"/drafts/{draft_no}")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 对得上不上的段

def test_comparison_records_only_mismatching_segments(client):
    # D1：缺第 3 段时发出（正文含 [缺口:片段3]）
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)
    d1 = issue(client, "C1")

    # 补上第 3 段后再发一稿
    post(client, "C1", 3, "信号不太好")
    d2 = issue(client, "C1")

    sheet = compare(client, d1["draft_no"], d2["draft_no"])

    # 看得出是哪两稿、哪通电话、各是第几稿（a/b 按请求里的先后）
    assert sheet["call_id"] == "C1"
    assert sheet["draft_no_a"] == "C1-D0001"
    assert sheet["draft_no_b"] == "C1-D0002"
    assert sheet["draft_seq_a"] == 1
    assert sheet["draft_seq_b"] == 2
    assert sheet["compared_at"] is not None

    # 只记对不上的段：第 1、2、4 段两边同文，不进单；只有第 3 段对不上
    assert sheet["mismatch_count"] == 1
    (m,) = sheet["mismatches"]
    assert m["seq"] == 3                                  # 哪一段
    assert m["a"] == {"kind": "gap", "gap": [3, 3], "marker": "[缺口:片段3]"}
    assert m["b"] == {"kind": "text", "seq": 3, "text": "信号不太好"}

    # 两份发稿快照原样嵌在对照单里，看得出两边当时各是什么
    assert sheet["draft_a"] == d1
    assert sheet["draft_b"] == d2

    # 同一条对照单按稿号取得到（反过来取也取得到同一条）
    got = client.get(f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}").json()
    assert got["mismatches"] == sheet["mismatches"]
    got_rev = client.get(f"/drafts/{d2['draft_no']}/compare/{d1['draft_no']}").json()
    assert got_rev["draft_no_a"] == "C1-D0001"            # 出单时的先后不变
    assert got_rev["draft_no_b"] == "C1-D0002"
    assert got_rev["mismatches"] == sheet["mismatches"]


def test_matching_segments_are_not_recorded(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)     # 两稿发出时都缺第 2 段
    d1 = issue(client, "C1")
    d2 = issue(client, "C1")                            # 没补段，再发一稿

    sheet = compare(client, d1["draft_no"], d2["draft_no"])
    # 两边第 1、3 段同文、第 2 段两边都缺：全部对得上，单是空的
    assert sheet["mismatch_count"] == 0
    assert sheet["mismatches"] == []


def test_tail_absent_is_recorded(client):
    # D1 只到第 2 段（assembling，后面的还没来，不是缺口）
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    d1 = issue(client, "C1")

    # 第 4 段先到：第 3 段成了缺口，正文一直到第 4 段
    post(client, "C1", 4, "那就这样", is_last=True)
    d2 = issue(client, "C1")

    sheet = compare(client, d1["draft_no"], d2["draft_no"])
    by_key = {tuple(m["range"]) if "range" in m else m["seq"]: m
              for m in sheet["mismatches"]}
    # 第 3 段：D1 当时还没到这里（absent），D2 当时是缺口
    assert by_key[3] == {
        "seq": 3,
        "a": {"kind": "absent"},
        "b": {"kind": "gap", "gap": [3, 3], "marker": "[缺口:片段3]"},
    }
    # 第 4 段：D1 没有，D2 有文本
    assert by_key[4]["a"] == {"kind": "absent"}
    assert by_key[4]["b"] == {"kind": "text", "seq": 4, "text": "那就这样"}


def test_gap_vs_gap_overlap_is_not_recorded(client):
    # 两稿发出时都是“只有第 1、5 段”，中间整段缺：两边都缺，对得上，不记
    post(client, "C1", 1, "一")
    post(client, "C1", 5, "五", is_last=True)
    d1 = issue(client, "C1")
    d2 = issue(client, "C1")
    sheet = compare(client, d1["draft_no"], d2["draft_no"])
    assert sheet["mismatches"] == []


# ------------------------------------------------------------- 对照不改稿

def test_comparison_never_changes_the_two_drafts(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")
    post(client, "C1", 2, "听得到吗")
    d2 = issue(client, "C1")

    compare(client, d1["draft_no"], d2["draft_no"])

    # 那两稿当时的正文和缺口一个字不变
    assert get_draft(client, d1["draft_no"]) == d1
    assert get_draft(client, d2["draft_no"]) == d2
    assert "[缺口:片段2]" in d1["content"]
    assert d1["gaps"] == [[2, 2]]

    # 待签原样欠着
    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt").json()
    assert receipt["status"] == "pending"
    assert receipt["draft"] == d1


def test_comparison_does_not_require_claim(client):
    # 对照是只读对账，不需要先认领：没人认也能出单
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "第二段", is_last=True)
    d2 = issue(client, "C1")
    assert compare(client, d1["draft_no"], d2["draft_no"])["draft_no_a"] == d1["draft_no"]


# ------------------------------------------------------------- 不能对的情形

def test_unknown_draft_is_404(client):
    assert client.post("/drafts/nope-D0001/compare/C1-D0001").status_code == 404
    assert client.post("/drafts/C1-D0001/compare/nope-D0001").status_code == 404
    assert client.get("/drafts/nope-D0001/compare/C1-D0001").status_code == 404


def test_draft_cannot_compare_with_itself(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    assert client.post(f"/drafts/{d1['draft_no']}/compare/{d1['draft_no']}").status_code == 409


def test_drafts_from_different_calls_cannot_compare(client):
    post(client, "call-A", 1, "甲第一句", is_last=True)
    post(client, "call-B", 1, "乙第一句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")

    r = client.post(f"/drafts/{da['draft_no']}/compare/{db['draft_no']}")
    assert r.status_code == 409
    # 没出成：两通电话各自的对照列表都是空
    assert client.get("/sessions/call-A/comparisons").json()["comparisons"] == []
    assert client.get("/sessions/call-B/comparisons").json()["comparisons"] == []


def test_same_pair_cannot_compare_twice(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")
    post(client, "C1", 2, "听得到吗")
    d2 = issue(client, "C1")

    first = compare(client, d1["draft_no"], d2["draft_no"])

    # 同一对再对一次：409，原单时间不动
    r = client.post(f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}")
    assert r.status_code == 409
    # 两稿先后倒过来仍是同一对：同样 409
    assert client.post(
        f"/drafts/{d2['draft_no']}/compare/{d1['draft_no']}"
    ).status_code == 409

    got = client.get(f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}").json()
    assert got["compared_at"] == first["compared_at"]
    assert got["mismatches"] == first["mismatches"]

    # 只有一张单，不会对出两张
    listed = client.get("/sessions/C1/comparisons").json()["comparisons"]
    assert len(listed) == 1


def test_get_comparison_that_was_never_made_is_404(client):
    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    d2 = issue(client, "C1")
    r = client.get(f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}")
    assert r.status_code == 404


# ------------------------------------------------------------- 两通电话不串

def test_comparisons_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲一")
    post(client, "call-A", 3, "甲三", is_last=True)
    da1 = issue(client, "call-A")
    post(client, "call-A", 2, "甲二")
    da2 = issue(client, "call-A")

    post(client, "call-B", 1, "乙一")
    post(client, "call-B", 3, "乙三", is_last=True)
    db1 = issue(client, "call-B")
    post(client, "call-B", 2, "乙二")
    db2 = issue(client, "call-B")

    compare(client, da1["draft_no"], da2["draft_no"])
    compare(client, db1["draft_no"], db2["draft_no"])

    la = client.get("/sessions/call-A/comparisons").json()["comparisons"]
    lb = client.get("/sessions/call-B/comparisons").json()["comparisons"]
    assert [s["call_id"] for s in la] == ["call-A"]
    assert [s["call_id"] for s in lb] == ["call-B"]
    assert la[0]["mismatches"][0]["b"]["text"] == "甲二"
    assert lb[0]["mismatches"][0]["b"]["text"] == "乙二"

    # 按稿列：只列涉及这一稿的对照，另一通的列不出来
    involving = client.get(f"/drafts/{da1['draft_no']}/comparisons").json()
    assert [s["draft_no_a"] for s in involving["comparisons"]] == [da1["draft_no"]]
    assert client.get(f"/drafts/{db2['draft_no']}/comparisons").json()[
        "comparisons"
    ][0]["call_id"] == "call-B"

    # 该通话/稿没有对照时是空列表
    post(client, "call-C", 1, "丙一", is_last=True)
    dc1 = issue(client, "call-C")
    assert client.get("/sessions/call-C/comparisons").json()["comparisons"] == []
    assert client.get(f"/drafts/{dc1['draft_no']}/comparisons").json()[
        "comparisons"
    ] == []


def test_comparisons_unknown_call_or_draft_is_404(client):
    assert client.get("/sessions/nope/comparisons").status_code == 404
    assert client.get("/drafts/nope-D0001/comparisons").status_code == 404


# ------------------------------------------------------------- 重启不丢

def test_comparisons_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "那就这样", is_last=True)
        d1 = issue(c1, "C1")
        post(c1, "C1", 2, "听得到吗")
        d2 = issue(c1, "C1")
        first = compare(c1, d1["draft_no"], d2["draft_no"])

    with TestClient(create_app(db)) as c2:                  # 同一数据库重新拉起
        # 对照过的还在，内容一个字不变
        got = c2.get(f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}").json()
        assert got["mismatches"] == first["mismatches"]
        assert got["compared_at"] == first["compared_at"]
        assert got["draft_no_a"] == d1["draft_no"]
        assert got["draft_no_b"] == d2["draft_no"]

        # 同一对重启后仍不能对第二次
        assert c2.post(
            f"/drafts/{d1['draft_no']}/compare/{d2['draft_no']}"
        ).status_code == 409

        # 按通话列、按稿列也都还在
        assert len(c2.get("/sessions/C1/comparisons").json()["comparisons"]) == 1
        assert len(c2.get(f"/drafts/{d1['draft_no']}/comparisons").json()[
            "comparisons"]) == 1

        # 那两稿当时的正文和缺口仍是原样
        assert get_draft(c2, d1["draft_no"])["gaps"] == [[2, 2]]
        assert get_draft(c2, d2["draft_no"])["gaps"] == []
