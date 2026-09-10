"""已发稿（draft）的测试：

- 给出时发一个稿号，拿号永远查到当时的正文和缺口（钉住）；
- 后来补段、重传都不改已发出去的稿；
- 缺口补上后出新稿，能看出订正的是哪一稿、曾经缺过；
- 两通不同电话的稿不串；
- 重启后已经发出去的稿还在。
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


def get(client, call_id):
    r = client.get(f"/sessions/{call_id}")
    assert r.status_code == 200
    return r.json()


def issue(client, call_id):
    r = client.post(f"/sessions/{call_id}/drafts")
    assert r.status_code == 201, r.text
    return r.json()


def get_draft(client, draft_no):
    r = client.get(f"/drafts/{draft_no}")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 钉住

def test_issued_draft_is_pinned_with_its_gaps(client):
    # 缺第 3 段时对外给出 → 拿到稿号，正文里带着当时的缺口标记
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)
    d1 = issue(client, "C1")

    assert d1["draft_no"] == "C1-D0001"          # 给出时发到的稿号
    assert d1["call_id"] == "C1"
    assert d1["draft_seq"] == 1
    assert d1["status"] == "incomplete"
    assert d1["gaps"] == [[3, 3]]                 # 当时的缺口
    assert "[缺口:片段3]" in d1["content"]        # 当时的正文
    assert d1["supersedes"] is None               # 首稿，没有订正对象
    assert d1["predecessor_had_gaps"] is False
    assert d1["predecessor_gaps"] == []

    # 后来：缺口补上、旧片段重传 —— 拿稿号查到的必须还是当时那一稿
    post(client, "C1", 3, "信号不太好")
    post(client, "C1", 2, "听得到吗")
    assert get(client, "C1")["status"] == "complete"   # 活视图已经变了

    frozen = get_draft(client, "C1-D0001")
    assert frozen == d1                            # 一字不差，还是当时那稿
    assert frozen["status"] == "incomplete"
    assert frozen["gaps"] == [[3, 3]]
    assert "[缺口:片段3]" in frozen["content"]
    # 快照里的缺口历史也是当时的样子（open），不随后续补齐而关闭
    assert frozen["gap_history"][0]["filled_at"] is None


def test_retransmission_and_conflicts_do_not_touch_issued_draft(client):
    post(client, "C1", 1, "原始内容")
    post(client, "C1", 2, "第二句", is_last=True)
    d1 = issue(client, "C1")

    post(client, "C1", 1, "原始内容")              # 重传
    post(client, "C1", 1, "被篡改的内容")          # 同号不同文 → 冲突
    assert get(client, "C1")["retransmissions"] == 2

    frozen = get_draft(client, d1["draft_no"])
    assert frozen == d1
    assert frozen["content"] == "原始内容\n第二句"


# ------------------------------------------------------------- 订正链

def test_new_draft_after_gap_fill_shows_what_it_corrects(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")                          # 带着缺口给出

    post(client, "C1", 2, "听得到吗")                 # 缺口补上
    d2 = issue(client, "C1")                          # 出新稿

    assert d2["draft_no"] == "C1-D0002"
    assert d2["status"] == "complete"
    assert d2["content"] == "你好\n听得到吗\n那就这样"
    assert d2["gaps"] == []
    # 看得出订正的是哪一稿、那一稿曾经缺过（缺在哪）
    assert d2["supersedes"] == "C1-D0001"
    assert d2["predecessor_had_gaps"] is True
    assert d2["predecessor_gaps"] == [[2, 2]]
    assert d2["was_incomplete"] is True

    # 被订正的旧稿原样还在，仍然查得到当时的缺口
    frozen = get_draft(client, "C1-D0001")
    assert frozen["status"] == "incomplete"
    assert frozen["gaps"] == [[2, 2]]
    assert "[缺口:片段2]" in frozen["content"]

    # 该通话的稿按顺序列出，订正链完整
    drafts = client.get("/sessions/C1/drafts").json()["drafts"]
    assert [d["draft_no"] for d in drafts] == ["C1-D0001", "C1-D0002"]
    assert drafts[1]["supersedes"] == "C1-D0001"


def test_correction_chain_extends_across_multiple_drafts(client):
    post(client, "C1", 2, "二")
    post(client, "C1", 4, "四", is_last=True)         # 缺 1、3
    issue(client, "C1")                               # D0001: 缺 [1,1],[3,3]
    post(client, "C1", 1, "一")                       # 补上 1，还缺 3
    d2 = issue(client, "C1")                          # D0002: 缺 [3,3]
    post(client, "C1", 3, "三")                       # 补齐
    d3 = issue(client, "C1")                          # D0003: 完整

    assert d2["supersedes"] == "C1-D0001"
    assert d2["predecessor_gaps"] == [[1, 1], [3, 3]]
    assert d2["gaps"] == [[3, 3]]
    assert d3["supersedes"] == "C1-D0002"
    assert d3["predecessor_had_gaps"] is True
    assert d3["predecessor_gaps"] == [[3, 3]]
    assert d3["status"] == "complete"


# ------------------------------------------------------------- 两通电话不串

def test_drafts_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 2, "甲第二句", is_last=True)
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 3, "乙第三句", is_last=True)  # 乙缺第 2 段

    da = issue(client, "call-A")
    db = issue(client, "call-B")
    # 两通电话各自从 D0001 起编号，但稿号带着 call_id，全局不相交
    assert da["draft_no"] == "call-A-D0001"
    assert db["draft_no"] == "call-B-D0001"

    # 按通话查：各回各的稿，正文互不混入
    drafts_a = client.get("/sessions/call-A/drafts").json()["drafts"]
    drafts_b = client.get("/sessions/call-B/drafts").json()["drafts"]
    assert [d["draft_no"] for d in drafts_a] == ["call-A-D0001"]
    assert [d["draft_no"] for d in drafts_b] == ["call-B-D0001"]
    assert "乙" not in drafts_a[0]["content"]
    assert "甲" not in drafts_b[0]["content"]

    # scoped 取稿：通话 + 第几稿，结构上拿不到别家的
    scoped = client.get("/sessions/call-B/drafts/1").json()
    assert scoped["draft_no"] == "call-B-D0001"
    assert scoped["gaps"] == [[2, 2]]
    # 按稿号取：也不会串
    assert get_draft(client, "call-A-D0001")["content"] == "甲第一句\n甲第二句"

    # 甲再发一稿，不影响乙的编号和稿
    da2 = issue(client, "call-A")
    assert da2["draft_no"] == "call-A-D0002"
    assert client.get("/sessions/call-B/drafts").json()["drafts"] == [db]


# ------------------------------------------------------------- 重启不丢

def test_issued_drafts_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
        d1 = issue(c1, "C1")                          # 带缺口给出
        post(c1, "C2", 1, "另一通电话")
        issue(c1, "C2")

    with TestClient(create_app(db)) as c2:            # 同一数据库重新拉起
        frozen = get_draft(c2, d1["draft_no"])        # 已发出的稿还在
        assert frozen == d1
        assert frozen["gaps"] == [[2, 2]]
        assert "[缺口:片段2]" in frozen["content"]

        # 重启后缺口补上、出新稿：旧稿仍钉着，新稿订正链接得上
        post(c2, "C1", 2, "听得到吗")
        d2 = issue(c2, "C1")
        assert d2["draft_no"] == "C1-D0002"
        assert d2["supersedes"] == "C1-D0001"
        assert d2["predecessor_gaps"] == [[2, 2]]
        assert get_draft(c2, "C1-D0001") == d1        # 旧稿一字未动

        drafts_c2 = c2.get("/sessions/C2/drafts").json()["drafts"]
        assert [d["draft_no"] for d in drafts_c2] == ["C2-D0001"]


# ------------------------------------------------------------- 该出新稿的信号

def test_session_view_flags_when_live_view_moved_since_latest_draft(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    assert get(client, "C1")["latest_draft"] is None   # 还没发过稿

    d1 = issue(client, "C1")
    s = get(client, "C1")
    assert s["latest_draft"]["draft_no"] == d1["draft_no"]
    assert s["latest_draft"]["changed_since"] is False  # 刚发，活视图与稿一致

    post(client, "C1", 3, "那就这样", is_last=True)     # 内容相同的重传
    assert get(client, "C1")["latest_draft"]["changed_since"] is False  # 不算改动

    post(client, "C1", 2, "听得到吗")                   # 缺口补上，活视图变了
    s = get(client, "C1")
    assert s["latest_draft"]["changed_since"] is True   # 该出新稿了

    d2 = issue(client, "C1")                            # 出新稿
    s = get(client, "C1")
    assert s["latest_draft"]["draft_no"] == d2["draft_no"]
    assert s["latest_draft"]["changed_since"] is False

    listed = [x for x in client.get("/sessions").json()["sessions"]
              if x["call_id"] == "C1"][0]
    assert listed["drafts_issued"] == 2


# ------------------------------------------------------------- 杂项

def test_unknown_draft_and_call_return_404(client):
    assert client.get("/drafts/nope-D0001").status_code == 404
    assert client.get("/sessions/nope/drafts").status_code == 404
    assert client.post("/sessions/nope/drafts").status_code == 404

    post(client, "C1", 1, "你好")
    assert client.get("/sessions/C1/drafts/1").status_code == 404   # 还没发过稿
    issue(client, "C1")
    assert client.get("/sessions/C1/drafts/2").status_code == 404   # 没有第二稿
    assert client.get("/sessions/C1/drafts/1").status_code == 200


def test_draft_snapshot_is_self_contained(client):
    # 稿里带着当时完整的拼装单元与缺口历史，不依赖活表就能还原当时视图
    post(client, "C1", 1, "一")
    post(client, "C1", 4, "四", is_last=True)          # 缺 [2,3]
    d1 = issue(client, "C1")

    assert [p.get("seq") for p in d1["parts"] if "seq" in p] == [1, 4]
    assert d1["parts"][1]["gap"] == [2, 3]
    assert d1["gap_history"][0]["range"] == [2, 3]
    assert d1["fragment_count"] == 2
    assert d1["issued_at"]

    # 补齐后活表已变，稿里的快照仍是当时那一份
    post(client, "C1", 2, "二")
    post(client, "C1", 3, "三")
    frozen = get_draft(client, d1["draft_no"])
    assert frozen["parts"] == d1["parts"]
    assert frozen["gap_history"] == d1["gap_history"]
