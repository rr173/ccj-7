"""活动桥的换边（swap side）测试：

- 桥还搭着时，可以把其中一边换成另一通**已有片段**的电话：桥号不变，
  换完还是这座桥，现行对齐按新的两边重新对上；
- 被换下去的那通恢复自由（active_bridge 清空，可以再搭新桥、可以日后再被
  换回来），它的片段一个不丢不改；
- 换上来的那通不能已经在别的活动桥里，空的不能换上来，也不能就是桥上现有
  两边中的一通；已经拆掉的桥不能再换边；
- 每次换边当时旧的两边是谁、对到哪一对，单独留痕，再换边、拆桥都不被新的
  两边盖掉；
- 换边记录与现行桥行都落 SQLite：服务再起来，换过边的桥还在，现行对齐按
  新的两边对得上，历次换边还查得到；
- 触发器/部分唯一索引在数据库层兜底“同一通不能同时待在两座活动桥里”。
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


def swap(client, bridge_no, side, call_id, expected=201, **payload):
    body = {"side": side, "call_id": call_id, **payload}
    r = client.post(f"/bridges/{bridge_no}/swap", json=body)
    assert r.status_code == expected, r.text
    return r.json()


def dismantle(client, bridge_no, expected=201):
    r = client.post(f"/bridges/{bridge_no}/dismantle")
    assert r.status_code == expected, r.text
    return r.json()


def swaps_of(client, bridge_no):
    r = client.get(f"/bridges/{bridge_no}/swaps")
    assert r.status_code == 200, r.text
    return r.json()["swaps"]


# ------------------------------------------------------------- 基本换边

def test_swap_side_keeps_bridge_and_realigns_to_new_sides(client):
    # A 两段齐、B 只有 1 段：A-B 桥对齐前缀到第 1 对、第 2 对缺右
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    # C 有三段：换上来后对齐范围按 C vs B 重新算
    feed(client, "C", ["c1", "c2", "c3"])
    b = bridge(client, "A", "B")
    assert b["aligned_up_to"] == 1 and b["gap_count"] == 1
    assert b["swap_count"] == 0 and b["swaps"] == []

    out = swap(client, b["bridge_no"], "left", "C")
    # 还是这座桥：桥号不变、仍是 active
    assert out["bridge_no"] == "B0001"
    assert out["status"] == "active"
    assert out["left_call_id"] == "C" and out["right_call_id"] == "B"
    # 现行对齐按新两边 C-B 现算：只对上第 1 对，第 2、3 对缺右（单边缺口逐对给）
    assert out["aligned_count"] == 1
    assert out["aligned_up_to"] == 1
    assert out["gaps"] == [[2, 2], [3, 3]]
    missing = {p["seq"]: p["missing"] for p in out["pairs"] if p["kind"] == "gap"}
    assert missing == {2: "right", 3: "right"}
    assert out["pairs"][0]["left"] == {"seq": 1, "text": "c1"}
    # 换边留痕已挂上桥视图
    assert out["swap_count"] == 1
    assert out["swaps"][0]["old_call_id"] == "A"
    assert out["swaps"][0]["new_call_id"] == "C"

    # 再取同一座桥：现行两边就是新两边
    again = client.get("/bridges/B0001").json()
    assert again["left_call_id"] == "C"
    assert again["aligned_count"] == 1


def test_swapped_out_call_is_free_and_keeps_its_fragments(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")

    # 被换下去的 A 恢复自由：会话视图不再指向任何活动桥
    assert client.get("/sessions/A").json()["active_bridge"] is None
    # 换上来的 C 此刻在桥里（左边），B 仍在右边
    assert client.get("/sessions/C").json()["active_bridge"] == {
        "bridge_no": "B0001", "side": "left", "other_call_id": "B",
        "created_at": client.get("/bridges/B0001").json()["created_at"],
    }
    assert client.get("/sessions/B").json()["active_bridge"]["other_call_id"] == "C"

    # A 的片段一个不丢不改；它现在可以再跟 D 搭一座新桥
    a = client.get("/sessions/A").json()
    assert a["fragment_count"] == 2 and a["content"] == "a1\na2"
    b2 = bridge(client, "A", "D")
    assert b2["bridge_no"] == "B0002"

    # C 仍在 B0001 里，不能一女二嫁
    bridge(client, "C", "D", expected=409)


def test_swap_right_side_and_other_side_stays(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    out = swap(client, "B0001", "right", "C")
    assert out["left_call_id"] == "A" and out["right_call_id"] == "C"
    rec = swaps_of(client, "B0001")[0]
    assert rec["side"] == "right"
    assert rec["old_call_id"] == "B"
    assert rec["new_call_id"] == "C"
    assert rec["other_call_id"] == "A"   # 原地不动的另一边
    # B 恢复自由
    assert client.get("/sessions/B").json()["active_bridge"] is None


def test_freed_call_can_be_brought_back_later(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")   # A 下、C 上：桥变成 C-B
    # A 恢复自由后，可以日后再被换回这座桥（此时它不是桥上现有两边）
    out = swap(client, "B0001", "left", "A")
    assert out["left_call_id"] == "A" and out["right_call_id"] == "B"
    assert out["aligned_count"] == 1     # A-B 的对齐重新现算
    # C 又下去了：留痕两笔都在
    seq = {(s["swap_seq"], s["old_call_id"], s["new_call_id"])
           for s in swaps_of(client, "B0001")}
    assert seq == {(1, "A", "C"), (2, "C", "A")}
    assert client.get("/sessions/C").json()["active_bridge"] is None


def test_active_alignment_after_swap_tracks_new_sides_only(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")

    # 给被换下去的 A 补段：碰不到这座桥的现行对齐
    post(client, "A", 2, "a2")
    v = client.get("/bridges/B0001").json()
    assert v["left_call_id"] == "C"
    assert v["total_pairs"] == 1 and v["aligned_count"] == 1

    # 给新上来的 C 与 B 补段：同一座桥继续对上
    post(client, "C", 2, "c2")
    post(client, "B", 2, "b2")
    v = client.get("/bridges/B0001").json()
    assert v["aligned_count"] == 2 and v["aligned_up_to"] == 2 and v["gaps"] == []


# ------------------------------------------------------------- 约束

def test_swap_rejects_unknown_and_empty_calls(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    bridge(client, "A", "B")

    # 换上来的通话根本不存在 → 404
    r = client.post("/bridges/B0001/swap", json={"side": "left", "call_id": "NOPE"})
    assert r.status_code == 404

    # 已知但空的通话 → 409（空的不能换上来）
    store = client.app.state.store
    store._conn.execute(
        "INSERT INTO calls(call_id, first_seen_at, last_activity_at) VALUES('E', 't', 't')"
    )
    store._conn.commit()
    r = client.post("/bridges/B0001/swap", json={"side": "left", "call_id": "E"})
    assert r.status_code == 409
    assert "no fragments" in r.json()["detail"]
    # 桥原样没动
    v = client.get("/bridges/B0001").json()
    assert v["left_call_id"] == "A" and v["swap_count"] == 0


def test_swap_rejects_call_already_in_another_bridge(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    bridge(client, "A", "B")
    bridge(client, "C", "D")
    # C 已在 B0002 里，不能换进 B0001
    r = client.post("/bridges/B0001/swap", json={"side": "left", "call_id": "C"})
    assert r.status_code == 409
    assert "active bridge" in r.json()["detail"]
    # 左右都查
    r = client.post("/bridges/B0001/swap", json={"side": "right", "call_id": "D"})
    assert r.status_code == 409
    # 没换成
    v = client.get("/bridges/B0001").json()
    assert v["left_call_id"] == "A" and v["right_call_id"] == "B"


def test_swap_rejects_calls_currently_on_same_bridge(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    bridge(client, "A", "B")
    # 换上来的就是原地这一边（等于没换）→ 409
    r = client.post("/bridges/B0001/swap", json={"side": "left", "call_id": "A"})
    assert r.status_code == 409
    # 换上来的是桥的另一边（会变成自己跟自己）→ 409
    r = client.post("/bridges/B0001/swap", json={"side": "left", "call_id": "B"})
    assert r.status_code == 409
    r = client.post("/bridges/B0001/swap", json={"side": "right", "call_id": "A"})
    assert r.status_code == 409
    assert swaps_of(client, "B0001") == []


def test_swap_rejects_dismantled_and_unknown_bridge(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    b = bridge(client, "A", "B")
    dismantle(client, b["bridge_no"])
    # 已经拆掉的桥不能再换边
    r = client.post(f"/bridges/{b['bridge_no']}/swap",
                    json={"side": "left", "call_id": "C"})
    assert r.status_code == 409
    assert "dismantled" in r.json()["detail"]
    # 拆桥快照仍是旧两边，没被换边请求改动
    frozen = client.get(f"/bridges/{b['bridge_no']}").json()
    assert frozen["left_call_id"] == "A"
    assert swaps_of(client, b["bridge_no"]) == []

    assert client.post("/bridges/B9999/swap",
                       json={"side": "left", "call_id": "C"}).status_code == 404
    assert client.get("/bridges/B9999/swaps").status_code == 404


def test_swap_validates_side_value(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    r = client.post("/bridges/B0001/swap", json={"side": "middle", "call_id": "C"})
    assert r.status_code == 422


def test_swap_request_aliases(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    r = client.post("/bridges/B0001/swap", json={"which": "right", "with": "C"})
    assert r.status_code == 201, r.text
    assert r.json()["right_call_id"] == "C"


# ------------------------------------------------------------- 留痕不被盖掉

def test_swap_history_keeps_old_sides_and_old_alignment(client):
    # A 两段、B 一段：换边前对齐前缀 1、第 2 对缺右
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    # C 三段：换上来后对齐变成另一个样子
    feed(client, "C", ["c1", "c2", "c3"])
    # D 两段齐：再把右边从 B 换成 D，对齐又变
    feed(client, "D", ["d1", "d2"])
    bridge(client, "A", "B")

    swap(client, "B0001", "left", "C")
    # 换边后旧两边还在收段：A 补到 3 段、B 补到 3 段 —— 这些都不能改写留痕
    post(client, "A", 3, "a3")
    post(client, "B", 2, "b2")
    post(client, "B", 3, "b3")
    swap(client, "B0001", "right", "D")

    history = swaps_of(client, "B0001")
    assert [h["swap_seq"] for h in history] == [1, 2]

    first, second = history
    # 第一笔：旧两边是 A、B，快照钉在第一次换边当时
    assert first["side"] == "left"
    assert first["old_call_id"] == "A" and first["new_call_id"] == "C"
    assert first["other_call_id"] == "B"
    before1 = first["before"]
    assert before1["left"]["call_id"] == "A"
    assert before1["right"]["call_id"] == "B"
    # 当时 A 两段、B 一段：对齐 1 对、第 2 对缺右 —— 与后来的补段无关
    assert before1["left"]["fragment_count"] == 2
    assert before1["right"]["fragment_count"] == 1
    assert before1["aligned_up_to"] == 1
    assert before1["gaps"] == [[2, 2]]
    last_pair = before1["pairs"][-1]
    assert last_pair["missing"] == "right"
    assert last_pair["left"] == {"seq": 2, "text": "a2"} and last_pair["right"] is None

    # 第二笔：旧两边是 C、B（不是最初的 A、B，也不是新的 C、D）
    assert second["side"] == "right"
    assert second["old_call_id"] == "B" and second["new_call_id"] == "D"
    assert second["other_call_id"] == "C"
    before2 = second["before"]
    assert before2["left"]["call_id"] == "C"
    assert before2["right"]["call_id"] == "B"
    # 第二次换边当时：C 三段、B 已补到三段，对齐 3 对全齐
    assert before2["aligned_count"] == 3 and before2["gaps"] == []

    # 现行视图按新两边 C-D：两段全对齐；旧对齐只能去 swaps 里查
    cur = client.get("/bridges/B0001").json()
    assert cur["left_call_id"] == "C" and cur["right_call_id"] == "D"
    assert cur["aligned_count"] == 2 and cur["swap_count"] == 2
    assert {h["old_call_id"] for h in cur["swaps"]} == {"A", "B"}


def test_swap_history_survives_dismantle(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")
    # 拆桥快照是现行两边 C-B；换边留痕照样可查、不被拆桥覆盖
    frozen = dismantle(client, "B0001")
    assert frozen["left_call_id"] == "C"
    assert frozen["swap_count"] == 1
    history = swaps_of(client, "B0001")
    assert history[0]["old_call_id"] == "A"
    assert history[0]["before"]["left"]["call_id"] == "A"
    assert client.get("/bridges/B0001").json()["swaps"][0]["new_call_id"] == "C"


# ------------------------------------------------------------- 按通话查

def test_bridges_per_call_include_swapped_out_history(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")   # A 下 C 上
    bridge(client, "A", "D")             # 自由的 A 另搭 B0002

    # 被换下去的 A：B0001（已不在桥上，最后在左）+ B0002（当前在左）
    a_list = client.get("/sessions/A/bridges").json()["bridges"]
    [b1] = [x for x in a_list if x["bridge_no"] == "B0001"]
    assert b1["side"] == "left" and b1["currently_on_bridge"] is False
    [b2] = [x for x in a_list if x["bridge_no"] == "B0002"]
    assert b2["side"] == "left" and b2["currently_on_bridge"] is True

    # 新上来的 C 在 B0001 左边、当前在桥
    c_list = client.get("/sessions/C/bridges").json()["bridges"]
    assert len(c_list) == 1
    assert c_list[0]["bridge_no"] == "B0001"
    assert c_list[0]["side"] == "left" and c_list[0]["currently_on_bridge"] is True

    # 一直在右边的 B：当前在桥
    b_list = client.get("/sessions/B/bridges").json()["bridges"]
    assert len(b_list) == 1
    assert b_list[0]["side"] == "right" and b_list[0]["currently_on_bridge"] is True


# ------------------------------------------------------------- 不改片段

def test_swap_does_not_modify_any_session(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    before_a = client.get("/sessions/A").json()
    before_b = client.get("/sessions/B").json()
    before_c = client.get("/sessions/C").json()
    bridge(client, "A", "B")
    swap(client, "B0001", "left", "C")
    for cid, before in (("A", before_a), ("B", before_b), ("C", before_c)):
        after = client.get(f"/sessions/{cid}").json()
        for key in ("content", "status", "gaps", "fragment_count", "version",
                    "gap_history", "retransmissions", "conflicts"):
            assert before[key] == after[key], (cid, key)


# ------------------------------------------------------------- DB 兜底

def test_database_trigger_blocks_swap_into_occupied_call(client):
    """绕过应用层直接 UPDATE 桥行换边时，BEFORE UPDATE 触发器仍兜底：
    换上来的通话已在另一座活动桥里 → ABORT，桥行不动。"""
    import sqlite3

    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    bridge(client, "A", "B")
    bridge(client, "C", "D")

    conn = client.app.state.store._conn
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE bridges SET left_call_id='C' WHERE bridge_no='B0001'"
        )
    conn.rollback()
    # B0001 两边原样
    v = client.get("/bridges/B0001").json()
    assert v["left_call_id"] == "A" and v["right_call_id"] == "B"


# ------------------------------------------------------------- 并发竞态

def test_lost_swap_race_leaves_no_history(tmp_path):
    """两座活动桥同一时刻把同一通电话换上来：赢家换成并留痕；输家被
    触发器兜底拦下（already_bridged），两边不动 —— 也绝不能留下
    “换过”的记录，重启后同样没有。"""
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

        # 竞态窗口：s2 做“E 是否在别的活动桥”检查时，s1 的换边还没提交，
        # 看到的是 E 仍自由；等 s2 落笔 UPDATE 时由数据库触发器兜底
        with patch.object(s2, "_active_bridge_row", lambda call_id: None):
            b1, r1 = s1.swap_bridge_side("B0001", "left", "E")
            assert r1 == "swapped"
            b2, r2 = s2.swap_bridge_side("B0002", "left", "E")
        assert r2 == "already_bridged" and b2 is None

        # 输家桥上两边还是原来的人
        v2 = s2.get_bridge("B0002")
        assert v2["left_call_id"] == "C" and v2["right_call_id"] == "D"
        # 没换成，历史里就不能有这笔
        assert v2["swap_count"] == 0 and v2["swaps"] == []
        assert s2.get_bridge_swaps("B0002")["swap_count"] == 0
        # E 只上成了 B0001，不在 B0002 的“上过的桥”里
        e_bridges = {b["bridge_no"]: b for b in s2.list_bridges_for_call("E")}
        assert e_bridges["B0001"]["currently_on_bridge"] is True
        assert "B0002" not in e_bridges
        # C 也没被记成“换下去过”
        c_bridges = {b["bridge_no"]: b for b in s2.list_bridges_for_call("C")}
        assert c_bridges["B0002"]["currently_on_bridge"] is True
    finally:
        s1.close()
        s2.close()

    # 重启之后：换成的那座记录还在；没换成的那座依然一笔不多
    s3 = Store(db)
    try:
        v1 = s3.get_bridge("B0001")
        assert v1["left_call_id"] == "E" and v1["swap_count"] == 1
        assert v1["swaps"][0]["old_call_id"] == "A"
        assert v1["swaps"][0]["new_call_id"] == "E"
        v2 = s3.get_bridge("B0002")
        assert v2["left_call_id"] == "C" and v2["right_call_id"] == "D"
        assert v2["swap_count"] == 0 and v2["swaps"] == []
        assert s3.get_bridge_swaps("B0002")["swaps"] == []
    finally:
        s3.close()


# ------------------------------------------------------------- 重启

def test_swapped_bridge_survives_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1", "a2"])
        feed(c1, "B", ["b1"])
        feed(c1, "C", ["c1", "c2", "c3"])
        feed(c1, "D", ["d1"])
        bridge(c1, "A", "B")
        swap(c1, "B0001", "left", "C")

    # 服务再起来：换过边的桥还在，现行对齐按新两边 C-B 对得上
    with TestClient(create_app(db)) as c2:
        cur = c2.get("/bridges/B0001").json()
        assert cur["status"] == "active"
        assert cur["left_call_id"] == "C" and cur["right_call_id"] == "B"
        assert cur["aligned_count"] == 1 and cur["gaps"] == [[2, 2], [3, 3]]

        # 换边当时旧两边是谁、对到哪，照样查得到，没被新两边盖掉
        history = c2.get("/bridges/B0001/swaps").json()["swaps"]
        assert len(history) == 1
        h = history[0]
        assert h["old_call_id"] == "A" and h["new_call_id"] == "C"
        assert h["before"]["left"]["call_id"] == "A"
        assert h["before"]["aligned_up_to"] == 1

        # 被换下去的 A 仍自由：重启后可直接搭新桥
        b2 = bridge(c2, "A", "D")
        assert b2["bridge_no"] == "B0002"

        # 活动桥唯一约束重启后继续生效：C 还在 B0001 里
        feed(c2, "E", ["e1"])
        r = c2.post("/bridges", json={"left_call_id": "C", "right_call_id": "E"})
        assert r.status_code == 409

        # 重启后现行对齐继续按新两边推进：B 补齐 2、3 段 → 全对上
        post(c2, "B", 2, "b2")
        post(c2, "B", 3, "b3")
        again = c2.get("/bridges/B0001").json()
        assert again["aligned_count"] == 3 and again["aligned_up_to"] == 3
        assert again["gaps"] == []

        # 旧留痕一个字没变（first before 仍是 A 两段、B 一段时的样子）
        h2 = c2.get("/bridges/B0001/swaps").json()["swaps"][0]
        assert h2["before"]["right"]["fragment_count"] == 1
        assert h2["swapped_at"] == h["swapped_at"]
