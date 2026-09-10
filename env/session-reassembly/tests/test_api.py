"""端到端 API 测试：乱序、重传、两次通话隔离、缺口标记与补齐、重启不丢。"""

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


def get(client, call_id):
    r = client.get(f"/sessions/{call_id}")
    assert r.status_code == 200
    return r.json()


# ------------------------------------------------------------- 基本拼装

def test_out_of_order_fragments_are_reordered(client):
    post(client, "A", 2, "第二句")
    post(client, "A", 3, "第三句", is_last=True)
    post(client, "A", 1, "第一句")
    s = get(client, "A")
    assert s["status"] == "complete"
    assert s["content"] == "第一句\n第二句\n第三句"
    assert s["gaps"] == []


def test_two_calls_are_never_interleaved(client):
    # 两次通话的片段在链路上交替到达
    post(client, "call-A", 1, "A1")
    post(client, "call-B", 1, "B1")
    post(client, "call-A", 2, "A2", is_last=True)
    post(client, "call-B", 2, "B2")
    post(client, "call-B", 3, "B3", is_last=True)

    a = get(client, "call-A")
    b = get(client, "call-B")
    assert a["status"] == "complete" and a["content"] == "A1\nA2"
    assert b["status"] == "complete" and b["content"] == "B1\nB2\nB3"
    assert "B1" not in a["content"] and "A1" not in b["content"]


# ------------------------------------------------------------- 重传去重

def test_retransmission_does_not_duplicate_sentences(client):
    post(client, "A", 1, "第一句")
    post(client, "A", 1, "第一句")          # 重传
    post(client, "A", 2, "第二句", is_last=True)
    r = post(client, "A", 2, "第二句", is_last=True)  # 重传

    assert r["ingest"]["duplicate"] is True
    s = get(client, "A")
    assert s["status"] == "complete"
    assert s["content"] == "第一句\n第二句"   # 没有重复句子
    assert s["fragment_count"] == 2
    assert s["retransmissions"] == 2


def test_conflicting_retransmission_keeps_first_and_is_flagged(client):
    post(client, "A", 1, "原始内容")
    r = post(client, "A", 1, "被篡改的内容")  # 同号不同文
    assert r["ingest"]["conflict"] is True
    s = get(client, "A")
    assert s["content"] == "原始内容"
    assert s["conflicts"] == 1


# ------------------------------------------------------------- 缺口语义

def test_gap_is_marked_never_silently_complete(client):
    post(client, "A", 1, "第一句")
    post(client, "A", 2, "第二句")
    post(client, "A", 4, "第四句", is_last=True)  # 第 3 段缺失

    s = get(client, "A")
    assert s["status"] == "incomplete"          # 绝不假装完整
    assert s["gaps"] == [[3, 3]]
    assert "[缺口:片段3]" in s["content"]        # 内容里显式标出缺口
    assert s["completed_at"] is None


def test_gap_filled_later_session_becomes_complete_but_remembers(client):
    post(client, "A", 1, "第一句")
    post(client, "A", 3, "第三句", is_last=True)
    before = get(client, "A")
    assert before["status"] == "incomplete"
    v_before = before["version"]

    post(client, "A", 2, "第二句")              # 缺口补上
    after = get(client, "A")

    assert after["status"] == "complete"        # 同一条会话变成了完整
    assert after["content"] == "第一句\n第二句\n第三句"
    assert "[缺口" not in after["content"]
    assert after["version"] > v_before          # 客户端能发现视图变了
    assert after["was_incomplete"] is True      # 能看出曾经缺过
    assert after["gap_history"][0]["range"] == [2, 2]
    assert after["gap_history"][0]["filled_at"] is not None
    assert after["completed_at"] is not None


def test_gap_filled_without_ever_seen_end_marker_becomes_complete(client):
    # 整条会话从没带过 is_last（结束标记丢了/上游没发）：
    # 对外给出去时是 incomplete，缺段补齐后必须变成 complete，不许停在拼接中
    post(client, "A", 1, "第一句")
    post(client, "A", 3, "第三句")
    assert get(client, "A")["status"] == "incomplete"

    r = post(client, "A", 2, "第二句")
    s = r["session"]
    assert s["status"] == "complete"
    assert s["content"] == "第一句\n第二句\n第三句"
    assert s["gaps"] == []
    assert s["was_incomplete"] is True            # 看得出曾经缺过
    assert s["completed_at"] is not None
    assert all(h["filled_at"] for h in s["gap_history"])

    listed = [x for x in client.get("/sessions").json()["sessions"]
              if x["call_id"] == "A"][0]
    assert listed["status"] == "complete"


def test_never_complete_while_content_contains_gap_after_last_seq(client):
    # is_last 早早声明总数，之后越界片段陆续到达、中间还缺号：
    # 哪怕结束标记早就见过，正文夹着缺口就绝不能对外说 complete
    post(client, "A", 1, "第一句", is_last=True)
    post(client, "A", 2, "第二句")              # 越过 is_last 声明的序号
    r = post(client, "A", 4, "第四句")          # 第 3 段缺失
    s = r["session"]
    assert s["status"] == "incomplete"
    assert s["gaps"] == [[3, 3]]
    assert "[缺口:片段3]" in s["content"]
    assert s["completed_at"] is None
    assert s["conflicts"] >= 1                  # 越界片段仍计入冲突，不被静默吞掉

    # 缺口补上后才完整，但要看得出曾经缺过
    post(client, "A", 3, "第三句")
    after = get(client, "A")
    assert after["status"] == "complete"
    assert after["content"] == "第一句\n第二句\n第三句\n第四句"
    assert after["was_incomplete"] is True
    assert after["completed_at"] is not None
    assert after["gap_history"][0]["range"] == [3, 3]


def test_gap_history_tracks_range_as_it_shrinks_and_fills(client):
    # 1,5 到达 → 缺口 [2,4]；补 3 → 缺口裂成 [2,2],[4,4]；再补齐
    post(client, "A", 1, "一")
    post(client, "A", 5, "五", is_last=True)
    assert get(client, "A")["gaps"] == [[2, 4]]

    post(client, "A", 3, "三")
    mid = get(client, "A")
    assert mid["status"] == "incomplete"
    assert mid["gaps"] == [[2, 2], [4, 4]]
    # 残留缺口段继承原发现时间：查得到 [2,4] 曾经整段缺过
    ranges_open = [h["range"] for h in mid["gap_history"] if h["filled_at"] is None]
    assert ranges_open == [[2, 2], [4, 4]]

    post(client, "A", 2, "二")
    post(client, "A", 4, "四")
    done = get(client, "A")
    assert done["status"] == "complete"
    assert done["gaps"] == []
    assert done["was_incomplete"] is True
    # 历史行全部关闭，且最早的 [2,4] 整段记录仍在、已带补齐时间
    assert all(h["filled_at"] for h in done["gap_history"])
    assert [2, 4] in [h["range"] for h in done["gap_history"]]


def test_huge_seq_gap_is_queryable_instantly(client):
    # 序号空将近一亿个：写入与各查询都必须立即返回，且马上看得出缺着
    post(client, "BIG", 1, "开头")
    r = post(client, "BIG", 1_000_000_000, "很远的结尾", is_last=True)
    s = r["session"]
    assert s["status"] == "incomplete"
    assert s["gaps"] == [[2, 999_999_999]]       # 一个区间，没有逐号展开
    assert "[缺口:片段2-999999999]" in s["content"]
    assert s["completed_at"] is None

    assert get(client, "BIG")["gaps"] == [[2, 999_999_999]]
    listed = [x for x in client.get("/sessions").json()["sessions"]
              if x["call_id"] == "BIG"][0]
    assert listed["status"] == "incomplete"
    assert listed["open_gaps"] == [[2, 999_999_999]]
    # 历史也只有一行（区间），而不是几亿行
    assert len(get(client, "BIG")["gap_history"]) == 1


def test_legacy_per_seq_gap_table_is_migrated(tmp_path):
    # 旧版 gap_events 按 (call_id, seq) 逐行存，启动时应自动迁成区间表
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
        CREATE TABLE gap_events(call_id TEXT, seq INTEGER, detected_at TEXT,
            filled_at TEXT, PRIMARY KEY(call_id, seq));
        INSERT INTO fragments VALUES ('A',1,'一','t',0),('A',4,'四','t',0);
        INSERT INTO calls VALUES ('A','t','t',4,NULL,0);
        INSERT INTO gap_events VALUES ('A',2,'d',NULL),('A',3,'d',NULL);
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db)) as c:
        s = get(c, "A")
        assert s["status"] == "incomplete"
        assert s["gaps"] == [[2, 3]]
        assert s["gap_history"][0]["range"] == [2, 3]
        # 迁移后继续拼接：补齐缺口 → 完整，历史关闭
        post(c, "A", 2, "二")
        post(c, "A", 3, "三")
        done = get(c, "A")
        assert done["status"] == "complete"
        assert done["was_incomplete"] is True
        assert all(h["filled_at"] for h in done["gap_history"])


def test_assembling_when_tail_not_yet_arrived(client):
    post(client, "A", 1, "第一句")
    post(client, "A", 2, "第二句")
    s = get(client, "A")
    assert s["status"] == "assembling"          # 连续但未见结束标记
    assert s["gaps"] == []


# ------------------------------------------------------------- 重启持久化

def test_in_flight_sessions_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:      # “重启前”的服务
        post(c1, "A", 1, "第一句")
        post(c1, "A", 3, "第三句", is_last=True)  # 缺第 2 段
        post(c1, "B", 1, "另一通电话第一句")

    with TestClient(create_app(db)) as c2:      # 同一数据库文件重新拉起
        a = get(c2, "A")
        assert a["status"] == "incomplete"      # 拼接中的会话没有散
        assert a["gaps"] == [[2, 2]]
        assert "[缺口:片段2]" in a["content"]

        post(c2, "A", 2, "第二句")              # 重启后继续拼
        a = get(c2, "A")
        assert a["status"] == "complete"
        assert a["content"] == "第一句\n第二句\n第三句"
        assert a["was_incomplete"] is True

        sessions = c2.get("/sessions").json()["sessions"]
        assert {s["call_id"] for s in sessions} == {"A", "B"}


# ------------------------------------------------------------- 杂项

def test_unknown_session_returns_404(client):
    assert client.get("/sessions/nope").status_code == 404


def test_invalid_fragment_rejected(client):
    r = client.post("/fragments", json={"call_id": "A", "seq": 0, "text": "x"})
    assert r.status_code == 422
    r = client.post("/fragments", json={"call_id": "", "seq": 1, "text": "x"})
    assert r.status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
