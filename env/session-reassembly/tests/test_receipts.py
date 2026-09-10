"""签收（receipt）的测试：

- 稿一发出即进入待签，按稿号签收，签完看得出签的是哪一稿；
- 待签期间补段、重传、会话变新，取出的待签仍是当时的正文和缺口；
- 两通电话的待签和签收不串：拿一通的号签不到另一通的稿；
- 同一份不能签两次；
- 没签时出了新稿，旧待签仍是旧那份，不会变成新稿；
- 重启后没签完的还在；老库已有的稿自动补出待签。
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


def get_receipt(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/receipt")
    assert r.status_code == 200, r.text
    return r.json()


def sign(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/receipt")
    assert r.status_code == 201, r.text
    return r.json()


def receipts(client, call_id):
    r = client.get(f"/sessions/{call_id}/receipts")
    assert r.status_code == 200, r.text
    return r.json()["receipts"]


# ------------------------------------------------------------- 待签

def test_issued_draft_enters_pending_receipt_with_pinned_content(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")

    r = get_receipt(client, d1["draft_no"])
    assert r["status"] == "pending"                    # 发稿即待签
    assert r["signed_at"] is None
    assert r["draft_no"] == "C1-D0001"
    assert r["call_id"] == "C1"
    assert r["draft_seq"] == 1
    assert r["issued_at"] == d1["issued_at"]
    assert r["draft"] == d1                            # 当时那一稿，一字不差
    assert r["draft"]["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in r["draft"]["content"]

    listed = receipts(client, "C1")
    assert [x["draft_no"] for x in listed] == ["C1-D0001"]
    assert listed[0]["status"] == "pending"


# ------------------------------------------------------------- 签收

def test_sign_by_draft_no_and_receipt_shows_which_draft_was_signed(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")

    r = sign(client, d1["draft_no"])                   # 按稿号签
    # 签完看得出签的是哪一稿
    assert r["status"] == "signed"
    assert r["draft_no"] == "C1-D0001"
    assert r["call_id"] == "C1"
    assert r["draft_seq"] == 1
    assert r["signed_at"] is not None
    assert r["draft"] == d1                            # 签的就是当时那一稿

    again = get_receipt(client, d1["draft_no"])        # 之后随时查得到
    assert again == r
    assert receipts(client, "C1")[0]["status"] == "signed"


def test_same_draft_cannot_be_signed_twice(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")

    first = sign(client, d1["draft_no"])
    r = client.post(f"/drafts/{d1['draft_no']}/receipt")
    assert r.status_code == 409                        # 同一份不能签两次

    after = get_receipt(client, d1["draft_no"])
    assert after["signed_at"] == first["signed_at"]    # 首次签收时间不动
    assert after == first


def test_sign_unknown_draft_or_call_returns_404(client):
    assert client.post("/drafts/nope-D0001/receipt").status_code == 404
    assert client.get("/drafts/nope-D0001/receipt").status_code == 404
    assert client.get("/sessions/nope/receipts").status_code == 404


# ------------------------------------------------------------- 待签不被后来改动

def test_pending_receipt_untouched_by_late_fragments_retransmits_and_new_view(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段时给出
    d1 = issue(client, "C1")

    # 后来：缺口补上、旧片段重传、同号不同文冲突 —— 会话活视图一路变新
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 1, "你好")
    post(client, "C1", 1, "被篡改的内容")
    assert client.get("/sessions/C1").json()["status"] == "complete"

    r = get_receipt(client, d1["draft_no"])            # 取出的待签仍是当时那份
    assert r["status"] == "pending"
    assert r["draft"] == d1
    assert r["draft"]["status"] == "incomplete"
    assert r["draft"]["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in r["draft"]["content"]


def test_new_draft_does_not_replace_pending_old_one(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")                           # D0001 待签

    post(client, "C1", 2, "听得到吗")                  # 缺口补上
    d2 = issue(client, "C1")                           # 又出了新稿 D0002

    old = get_receipt(client, d1["draft_no"])
    assert old["status"] == "pending"                  # 旧待签仍是旧那份
    assert old["draft"] == d1                          # 不会变成新稿
    assert old["draft"]["gaps"] == [[2, 2]]

    listed = receipts(client, "C1")                    # 两份各自独立待签
    assert [(x["draft_no"], x["status"]) for x in listed] == [
        ("C1-D0001", "pending"),
        ("C1-D0002", "pending"),
    ]

    sign(client, d2["draft_no"])                       # 签新稿 ≠ 签旧稿
    assert get_receipt(client, d1["draft_no"])["status"] == "pending"
    assert get_receipt(client, d2["draft_no"])["status"] == "signed"


# ------------------------------------------------------------- 两通电话不串

def test_two_calls_receipts_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 2, "甲第二句", is_last=True)
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 3, "乙第三句", is_last=True)  # 乙缺第 2 段
    da = issue(client, "call-A")
    db = issue(client, "call-B")

    signed_a = sign(client, da["draft_no"])            # 签甲的
    assert signed_a["call_id"] == "call-A"
    assert signed_a["draft"]["content"] == "甲第一句\n甲第二句"

    # 乙的待签原样还在，没被甲的签收碰到
    rb = get_receipt(client, db["draft_no"])
    assert rb["status"] == "pending"
    assert rb["draft"]["gaps"] == [[2, 2]]
    assert "甲" not in rb["draft"]["content"]

    # 各回各的签收单列表
    assert [x["draft_no"] for x in receipts(client, "call-A")] == ["call-A-D0001"]
    assert [x["draft_no"] for x in receipts(client, "call-B")] == ["call-B-D0001"]

    # 拿乙的号签，签到的只会是乙那一稿，碰不到甲的
    signed_b = sign(client, db["draft_no"])
    assert signed_b["call_id"] == "call-B"
    assert signed_b["draft"] == db
    assert get_receipt(client, da["draft_no"])["signed_at"] == signed_a["signed_at"]


# ------------------------------------------------------------- 重启不丢

def test_unsigned_receipts_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
        d1 = issue(c1, "C1")                          # 待签
        post(c1, "C1", 2, "听得到吗")
        d2 = issue(c1, "C1")
        signed = sign(c1, d2["draft_no"])             # 新稿签掉，旧稿留着

    with TestClient(create_app(db)) as c2:            # 同一数据库文件重新拉起
        old = get_receipt(c2, d1["draft_no"])         # 没签完的还在
        assert old["status"] == "pending"
        assert old["draft"] == d1
        assert old["draft"]["gaps"] == [[2, 2]]

        done = get_receipt(c2, d2["draft_no"])        # 已签的也没丢
        assert done["status"] == "signed"
        assert done["signed_at"] == signed["signed_at"]

        r = sign(c2, d1["draft_no"])                  # 重启后旧待签还能正常签掉
        assert r["status"] == "signed"
        assert r["draft"] == d1


def test_receipts_backfilled_for_drafts_issued_before_upgrade(tmp_path):
    # 老库：drafts 表里有稿，但还没有 receipts 表（升级前的库）
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE fragments(call_id TEXT, seq INTEGER, text TEXT,
            first_seen_at TEXT, retransmissions INTEGER DEFAULT 0,
            PRIMARY KEY(call_id, seq));
        CREATE TABLE calls(call_id TEXT PRIMARY KEY, first_seen_at TEXT,
            last_activity_at TEXT, last_seq INTEGER, completed_at TEXT,
            conflicts INTEGER DEFAULT 0);
        CREATE TABLE gap_events(call_id TEXT, seq_lo INTEGER, seq_hi INTEGER,
            detected_at TEXT, filled_at TEXT, PRIMARY KEY(call_id, seq_lo, seq_hi));
        CREATE TABLE drafts(draft_no TEXT PRIMARY KEY, call_id TEXT NOT NULL,
            draft_seq INTEGER NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL,
            parts_json TEXT NOT NULL, gaps_json TEXT NOT NULL,
            gap_history_json TEXT NOT NULL, was_incomplete INTEGER NOT NULL,
            fragment_count INTEGER NOT NULL, version INTEGER NOT NULL,
            supersedes TEXT, predecessor_had_gaps INTEGER NOT NULL DEFAULT 0,
            predecessor_gaps_json TEXT NOT NULL DEFAULT '[]',
            issued_at TEXT NOT NULL, UNIQUE(call_id, draft_seq));
        INSERT INTO fragments VALUES ('C1',1,'你好','t',0);
        INSERT INTO calls VALUES ('C1','t','t',NULL,NULL,0);
        INSERT INTO drafts VALUES ('C1-D0001','C1',1,'assembling','你好',
            '[{"seq":1,"text":"你好"}]','[]','[]',0,1,1,NULL,0,'[]','t0');
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db)) as c:
        r = get_receipt(c, "C1-D0001")                # 老稿自动有了待签
        assert r["status"] == "pending"
        assert r["draft"]["content"] == "你好"
        assert r["issued_at"] == "t0"
        signed = sign(c, "C1-D0001")                  # 也能正常签收
        assert signed["status"] == "signed"
