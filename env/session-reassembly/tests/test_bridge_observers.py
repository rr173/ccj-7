"""桥旁观（observer）测试：

- 桥还搭着时，第三通**已经有片段**的电话可以来旁观；旁观者不是桥的一边，
  桥上对齐仍按原来两边现算，两边再来新段，旁观者看到的也跟着变；
- 正在旁观的这通不能同时旁观另一座，也不能是这座桥上的一边；空的电话
  不能来旁观；已经拆掉的桥不能再让人旁观（拆桥时旁观一并结束）；
- 正旁观的时候，这通不能被换上这座桥；先不当旁观的人，才能换上来；
- 旁观不改那两通各自的会话，也不改桥上此刻的对齐；
- 旁观记录落 SQLite：服务再起来，谁在旁观还在，对齐仍按两边现在的段来算。
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


def feed(client, call_id, texts, start=1):
    for i, t in enumerate(texts):
        post(client, call_id, start + i, t)


def bridge(client, left, right, expected=201):
    r = client.post("/bridges", json={"left_call_id": left, "right_call_id": right})
    assert r.status_code == expected, r.text
    return r.json()


def observe(client, bridge_no, call_id, expected=201):
    r = client.post(f"/bridges/{bridge_no}/observers", json={"call_id": call_id})
    assert r.status_code == expected, r.text
    return r.json()


def leave(client, bridge_no, call_id, expected=200):
    r = client.post(f"/bridges/{bridge_no}/observers/{call_id}/leave")
    assert r.status_code == expected, r.text
    return r.json()


def dismantle(client, bridge_no, expected=201):
    r = client.post(f"/bridges/{bridge_no}/dismantle")
    assert r.status_code == expected, r.text
    return r.json()


# ------------------------------------------------------------- 开始旁观

def test_observer_joins_and_sees_live_alignment(client):
    # A 三段、B 两段：桥上第 3 对缺右；C 两段，来旁观
    feed(client, "A", ["a1", "a2", "a3"])
    feed(client, "B", ["b1", "b2"])
    feed(client, "C", ["c1", "c2"])
    b = bridge(client, "A", "B")
    assert b["observer_count"] == 0 and b["observers"] == []

    obs = observe(client, b["bridge_no"], "C")
    assert obs["bridge_no"] == "B0001"
    assert obs["call_id"] == "C"
    assert obs["since"]
    # 旁观者看到的两边就是桥上现行两边，对齐按两边现算
    assert obs["left_call_id"] == "A" and obs["right_call_id"] == "B"
    assert obs["aligned_count"] == 2 and obs["aligned_up_to"] == 2
    assert obs["total_pairs"] == 3 and obs["gaps"] == [[3, 3]]
    last = obs["pairs"][-1]
    assert last["kind"] == "gap" and last["missing"] == "right"
    assert last["left"] == {"seq": 3, "text": "a3"} and last["right"] is None

    # 桥视图带得出此刻谁在旁观；旁观者不是桥的一边
    view = client.get("/bridges/B0001").json()
    assert view["left_call_id"] == "A" and view["right_call_id"] == "B"
    assert view["observer_count"] == 1
    assert view["observers"] == [
        {"bridge_no": "B0001", "call_id": "C", "since": obs["since"]}
    ]

    # 列表端点同样查得到；旁观者自己的会话视图标明它在看哪座桥
    listed = client.get("/bridges/B0001/observers").json()
    assert listed["observer_count"] == 1
    assert listed["observers"][0]["call_id"] == "C"
    assert client.get("/sessions/C").json()["observing_bridge"] == {
        "bridge_no": "B0001", "since": obs["since"]
    }
    # 两边各自的会话不挂旁观标记
    assert client.get("/sessions/A").json()["observing_bridge"] is None
    assert client.get("/sessions/B").json()["observing_bridge"] is None


def test_observer_view_follows_new_fragments_on_either_side(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    observe(client, "B0001", "C")

    seen = client.get("/bridges/B0001/observers/C").json()
    assert seen["aligned_up_to"] == 1 and seen["gaps"] == [[2, 2]]

    # 两边再来新段，旁观者看到的跟着变（与活动桥现行对齐同源）
    post(client, "B", 2, "b2")
    post(client, "A", 3, "a3")
    seen = client.get("/bridges/B0001/observers/C").json()
    assert seen["aligned_count"] == 2 and seen["aligned_up_to"] == 2
    assert seen["gaps"] == [[3, 3]]
    live = client.get("/bridges/B0001").json()
    assert seen["pairs"] == live["pairs"]

    # 旁观者自己的段不参与对齐：C 再来到 4 段，桥上对齐一个字不动
    post(client, "C", 2, "c2")
    post(client, "C", 3, "c3")
    post(client, "C", 4, "c4")
    seen = client.get("/bridges/B0001/observers/C").json()
    assert seen["total_pairs"] == 3 and seen["aligned_count"] == 2
    assert seen["gaps"] == [[3, 3]]
    assert client.get("/bridges/B0001").json()["pairs"] == live["pairs"]


def test_reobserve_same_bridge_is_idempotent(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")

    first = observe(client, "B0001", "C")
    again = observe(client, "B0001", "C", expected=200)
    # 幂等：还是同一段旁观，开始时刻不动，人数不翻倍
    assert again["since"] == first["since"]
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 1


def test_leave_and_reobserve(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    started = observe(client, "B0001", "C")

    gone = leave(client, "B0001", "C")
    assert gone["bridge_no"] == "B0001" and gone["call_id"] == "C"
    assert gone["since"] == started["since"]
    assert gone["left_at"] and gone["left_at"] >= gone["since"]

    # 离开后：不再是旁观者，看到的入口 404，会话视图标记清空
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 0
    assert client.get("/bridges/B0001/observers/C").status_code == 404
    assert client.get("/sessions/C").json()["observing_bridge"] is None

    # 不当旁观的人之后可以再来旁观（另起一段，开始时刻重记）
    again = observe(client, "B0001", "C")
    assert again["since"] >= started["since"]
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 1


# ------------------------------------------------------------- 约束

def test_observer_cannot_observe_two_bridges_at_once(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    feed(client, "E", ["e1"])
    bridge(client, "A", "B")
    bridge(client, "D", "E")
    observe(client, "B0001", "C")

    # 正在旁观 B0001 的 C 不能同时旁观 B0002
    r = client.post("/bridges/B0002/observers", json={"call_id": "C"})
    assert r.status_code == 409
    assert "another bridge" in r.json()["detail"]
    assert client.get("/bridges/B0002/observers").json()["observer_count"] == 0

    # 先不当 B0001 的旁观者，才能去旁观 B0002
    leave(client, "B0001", "C")
    obs = observe(client, "B0002", "C")
    assert obs["bridge_no"] == "B0002"
    assert client.get("/sessions/C").json()["observing_bridge"]["bridge_no"] == "B0002"


def test_side_of_this_bridge_cannot_observe(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    bridge(client, "A", "B")
    # 桥上两边（左、右都试）不能来旁观这座桥
    for cid in ("A", "B"):
        r = client.post("/bridges/B0001/observers", json={"call_id": cid})
        assert r.status_code == 409
        assert "side of this bridge" in r.json()["detail"]
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 0


def test_empty_and_unknown_call_cannot_observe(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    bridge(client, "A", "B")

    # 根本没这通电话 → 404
    r = client.post("/bridges/B0001/observers", json={"call_id": "NOPE"})
    assert r.status_code == 404

    # 已知但一个片段都没有的空通话 → 409
    store = client.app.state.store
    store._conn.execute(
        "INSERT INTO calls(call_id, first_seen_at, last_activity_at) VALUES('E', 't', 't')"
    )
    store._conn.commit()
    r = client.post("/bridges/B0001/observers", json={"call_id": "E"})
    assert r.status_code == 409
    assert "no fragments" in r.json()["detail"]
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 0


def test_observer_endpoints_unknown_bridge_404(client):
    feed(client, "C", ["c1"])
    assert client.post(
        "/bridges/B9999/observers", json={"call_id": "C"}
    ).status_code == 404
    assert client.post("/bridges/B9999/observers/C/leave").status_code == 404
    assert client.get("/bridges/B9999/observers").status_code == 404
    assert client.get("/bridges/B9999/observers/C").status_code == 404


def test_leave_requires_observing(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    # 没在旁观就离开 → 409；离开两次第二次也是 409
    assert client.post("/bridges/B0001/observers/C/leave").status_code == 409
    observe(client, "B0001", "C")
    leave(client, "B0001", "C")
    r = client.post("/bridges/B0001/observers/C/leave")
    assert r.status_code == 409
    assert "not observing" in r.json()["detail"]


def test_observe_request_aliases(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    r = client.post("/bridges/B0001/observers", json={"observer": "C"})
    assert r.status_code == 201, r.text
    assert r.json()["call_id"] == "C"


def test_observing_is_not_being_on_the_bridge(client):
    # 旁观不算“上过”这座桥：按通话列桥列不出旁观的桥
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    observe(client, "B0001", "C")
    assert client.get("/sessions/C/bridges").json()["bridges"] == []
    # 旁观者也不占活动桥名额：C 仍自由，可另搭一座
    feed(client, "D", ["d1"])
    b2 = bridge(client, "C", "D")
    assert b2["bridge_no"] == "B0002"
    # 在另一座桥上当一边，不妨碍继续旁观这座桥
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 1
    assert client.get("/sessions/C").json()["observing_bridge"]["bridge_no"] == "B0001"


# ------------------------------------------------------------- 拆掉的桥

def test_dismantled_bridge_cannot_be_observed_and_observation_ends(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    bridge(client, "A", "B")
    observe(client, "B0001", "C")
    dismantle(client, "B0001")

    # 已经拆掉的桥不能再让人旁观
    r = client.post("/bridges/B0001/observers", json={"call_id": "D"})
    assert r.status_code == 409
    assert "dismantled" in r.json()["detail"]

    # 拆桥时正在旁观的 C 一并结束旁观
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 0
    assert client.get("/bridges/B0001/observers/C").status_code == 404
    assert client.get("/sessions/C").json()["observing_bridge"] is None
    view = client.get("/bridges/B0001").json()
    assert view["status"] == "dismantled" and view["observer_count"] == 0

    # C 恢复自由，可以去旁观别的桥
    feed(client, "E", ["e1"])
    bridge(client, "D", "E")
    obs = observe(client, "B0002", "C")
    assert obs["bridge_no"] == "B0002"


# ------------------------------------------------------------- 旁观与换边

def test_observer_cannot_be_swapped_in_until_it_leaves(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1", "c2"])
    bridge(client, "A", "B")
    observe(client, "B0001", "C")

    # 正旁观这座桥的不能被换上来（左右两边都试）
    for side in ("left", "right"):
        r = client.post("/bridges/B0001/swap", json={"side": side, "call_id": "C"})
        assert r.status_code == 409
        assert "observing" in r.json()["detail"]
    # 桥原样没动：两边没换、没有换边留痕、旁观照旧
    view = client.get("/bridges/B0001").json()
    assert view["left_call_id"] == "A" and view["right_call_id"] == "B"
    assert view["swap_count"] == 0 and view["observer_count"] == 1

    # 先不当旁观的人，才能换上来
    leave(client, "B0001", "C")
    out = client.post(
        "/bridges/B0001/swap", json={"side": "left", "call_id": "C"}
    ).json()
    assert out["left_call_id"] == "C" and out["swap_count"] == 1
    assert client.get("/sessions/C").json()["observing_bridge"] is None
    assert client.get("/sessions/C").json()["active_bridge"]["side"] == "left"


def test_observing_another_bridge_does_not_block_swap(client):
    # 只旁观别的桥，不妨碍被换上这座桥
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    feed(client, "E", ["e1"])
    bridge(client, "A", "B")
    bridge(client, "D", "E")
    observe(client, "B0001", "C")

    r = client.post("/bridges/B0002/swap", json={"side": "left", "call_id": "C"})
    assert r.status_code == 201, r.text
    assert r.json()["left_call_id"] == "C"
    # 换上 B0002 之后仍在旁观 B0001（旁观者不能是“这座”桥上的一边，B0002 不算）
    assert client.get("/bridges/B0001/observers").json()["observer_count"] == 1


# ------------------------------------------------------------- 旁观不改任何东西

def test_observing_does_not_modify_sessions_or_alignment(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    before_a = client.get("/sessions/A").json()
    before_b = client.get("/sessions/B").json()
    before_c = client.get("/sessions/C").json()
    before_bridge = client.get("/bridges/B0001").json()

    observe(client, "B0001", "C")
    leave(client, "B0001", "C")
    observe(client, "B0001", "C")

    after_a = client.get("/sessions/A").json()
    after_b = client.get("/sessions/B").json()
    after_c = client.get("/sessions/C").json()
    after_bridge = client.get("/bridges/B0001").json()
    # 那两通各自的会话一个字不变；旁观者自己的会话也只有旁观标记在变
    for key in ("content", "status", "gaps", "fragment_count", "version",
                "gap_history", "retransmissions", "conflicts", "active_bridge"):
        assert before_a[key] == after_a[key]
        assert before_b[key] == after_b[key]
        assert before_c[key] == after_c[key]
    # 桥上此刻还在算的对齐不因旁观改变（只是多了旁观留痕）
    for key in ("left_call_id", "right_call_id", "aligned_count", "gap_count",
                "total_pairs", "aligned_up_to", "gaps", "pairs", "swap_count"):
        assert before_bridge[key] == after_bridge[key]
    assert after_bridge["observer_count"] == 1


# ------------------------------------------------------------- 重启

def test_observers_survive_restart_and_alignment_still_live(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1", "a2"])
        feed(c1, "B", ["b1"])
        feed(c1, "C", ["c1"])
        feed(c1, "D", ["d1"])
        bridge(c1, "A", "B")
        started = observe(c1, "B0001", "C")
        observe(c1, "B0001", "D")

    # 服务再起来：谁在旁观还在，旁观者看到的对齐仍按两边现在的段现算
    with TestClient(create_app(db)) as c2:
        listed = c2.get("/bridges/B0001/observers").json()
        assert listed["observer_count"] == 2
        assert [o["call_id"] for o in listed["observers"]] == ["C", "D"]
        assert listed["observers"][0]["since"] == started["since"]
        assert c2.get("/sessions/C").json()["observing_bridge"] == {
            "bridge_no": "B0001", "since": started["since"]
        }
        view = c2.get("/bridges/B0001").json()
        assert view["observer_count"] == 2

        # 重启后两边再来新段，旁观者看到的跟着变
        post(c2, "B", 2, "b2")
        seen = c2.get("/bridges/B0001/observers/C").json()
        assert seen["aligned_count"] == 2 and seen["aligned_up_to"] == 2
        assert seen["gaps"] == []

        # 重启后约束照旧：正旁观的不能换上来，离开后才行
        r = c2.post("/bridges/B0001/swap", json={"side": "right", "call_id": "C"})
        assert r.status_code == 409
        leave(c2, "B0001", "C")
        r = c2.post("/bridges/B0001/swap", json={"side": "right", "call_id": "C"})
        assert r.status_code == 201
        assert c2.get("/bridges/B0001/observers").json()["observer_count"] == 1


def test_observation_ended_by_dismantle_stays_ended_after_restart(tmp_path):
    db = str(tmp_path / "restart2.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1"])
        feed(c1, "B", ["b1"])
        feed(c1, "C", ["c1"])
        bridge(c1, "A", "B")
        observe(c1, "B0001", "C")
        dismantle(c1, "B0001")

    with TestClient(create_app(db)) as c2:
        # 已拆的桥重启后仍不能旁观；当时结束的旁观不会复活
        r = c2.post("/bridges/B0001/observers", json={"call_id": "C"})
        assert r.status_code == 409
        assert c2.get("/bridges/B0001/observers").json()["observer_count"] == 0
        assert c2.get("/sessions/C").json()["observing_bridge"] is None


# ------------------------------------------------------------- DB 兜底

def test_database_backstops_for_observer_rules(client):
    """绕过应用层直接写库时，唯一索引与触发器仍兜底：
    同一通不能同时旁观两座、旁观者不能是桥上两边、已拆的桥不能被旁观、
    正旁观的不能被换上来。"""
    import sqlite3

    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    feed(client, "E", ["e1"])
    bridge(client, "A", "B")
    bridge(client, "D", "E")
    observe(client, "B0001", "C")
    conn = client.app.state.store._conn

    # 同一通同时旁观两座 → 部分唯一索引 ABORT
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bridge_observers(bridge_no, call_id, since)"
            " VALUES('B0002', 'C', 't')"
        )
    conn.rollback()
    # 旁观者就是这座桥上的一边 → INSERT 触发器 ABORT
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bridge_observers(bridge_no, call_id, since)"
            " VALUES('B0001', 'A', 't')"
        )
    conn.rollback()
    # 正旁观这座桥的通话被直接换上桥 → UPDATE 触发器 ABORT
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE bridges SET left_call_id='C' WHERE bridge_no='B0001'"
        )
    conn.rollback()
    # 已拆的桥不能再让人旁观 → INSERT 触发器 ABORT
    dismantle(client, "B0002")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bridge_observers(bridge_no, call_id, since)"
            " VALUES('B0002', 'C', 't')"
        )
    conn.rollback()

    # 全都没写成：B0001 仍是 A-B、C 还在旁观；B0002 拆后无人旁观
    view = client.get("/bridges/B0001").json()
    assert view["left_call_id"] == "A" and view["right_call_id"] == "B"
    assert view["observer_count"] == 1
    assert client.get("/bridges/B0002/observers").json()["observer_count"] == 0


def test_lost_observe_race_keeps_single_observer(tmp_path):
    """两座桥同一时刻抢同一通电话来旁观：赢家落笔；输家被唯一索引兜底
    拦下（already_observing）—— 重启后这通仍只旁观一座。"""
    from unittest.mock import patch

    from app.store import Store

    db = str(tmp_path / "race.db")
    s1 = Store(db)
    s2 = Store(db)  # 同库的另一个实例（另一进程/另一连接）
    try:
        for cid in ("A", "B", "C", "D", "E"):
            s1.ingest(cid, 1, f"{cid}1")
        s1.create_bridge("A", "B")   # B0001
        s1.create_bridge("C", "D")   # B0002

        # 竞态窗口：s2 做“E 是否在旁观别的桥”检查时，s1 的旁观还没提交，
        # 看到的是 E 谁也没旁观；等 s2 落笔 INSERT 时由唯一索引兜底
        with patch.object(s2, "_observing_row_for_call", lambda call_id: None):
            v1, r1 = s1.observe_bridge("B0001", "E")
            assert r1 == "observing"
            v2, r2 = s2.observe_bridge("B0002", "E")
        assert r2 == "already_observing" and v2 is None

        # E 只旁观上了 B0001
        assert s2.list_bridge_observers("B0001")["observer_count"] == 1
        assert s2.list_bridge_observers("B0002")["observer_count"] == 0
    finally:
        s1.close()
        s2.close()

    # 重启之后：仍只有 B0001 有 E 在旁观
    s3 = Store(db)
    try:
        assert s3.list_bridge_observers("B0001")["observer_count"] == 1
        assert s3.list_bridge_observers("B0002")["observer_count"] == 0
        assert s3.get_session("E")["observing_bridge"]["bridge_no"] == "B0001"
    finally:
        s3.close()
