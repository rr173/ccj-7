"""两通电话之间的桥（bridge）测试：

- 两通不同的电话可以搭一座桥，桥上按序号一对一对齐；
- 一对里两边都到了才算对齐；一边缺了这一对就是缺口（标明缺哪一边），
  绝不拿另一边的字凑上；
- 拆掉以后两通还是各自独立的会话，这座桥当时对齐到哪一对留得下来；
- 同一通电话不能同时待在两座桥里（拆了之后可以再搭，历史桥全留）；
- 空的通话不能拿来搭；同一通电话不能跟自己搭；
- 桥只新增 bridges 表，不写不改两通各自的片段/会话；
- 服务重启后没拆的桥还在，对齐仍按到过的序号对得上。
"""

import pytest
from fastapi.testclient import TestClient

from app import reassembly as R
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


def bridge(client, left, right, expected=201):
    r = client.post("/bridges", json={"left_call_id": left, "right_call_id": right})
    assert r.status_code == expected, r.text
    return r.json()


def dismantle(client, bridge_no, expected=201):
    r = client.post(f"/bridges/{bridge_no}/dismantle")
    assert r.status_code == expected, r.text
    return r.json()


def feed(client, call_id, texts, start=1):
    for i, t in enumerate(texts):
        post(client, call_id, start + i, t)


# ------------------------------------------------------------- 纯函数

def test_pure_pairs_align_only_when_both_sides_present():
    pairs = R.build_bridge_pairs({1: "a", 2: "b", 4: "d"}, {1: "甲", 2: "乙", 3: "丙"})
    kinds = [(p["kind"], p.get("seq"), p.get("missing")) for p in pairs]
    assert kinds == [
        ("aligned", 1, None),
        ("aligned", 2, None),
        ("gap", 3, "left"),       # 只有右边到：缺口，不拿右边的字顶左边
        ("gap", 4, "right"),      # 只有左边到：缺口，不拿左边的字顶右边
    ]
    # 缺口这一对，到的那边的字原样带着，但缺的那边明确是 null
    g3, g4 = pairs[2], pairs[3]
    assert g3["left"] is None and g3["right"] == {"seq": 3, "text": "丙"}
    assert g4["left"] == {"seq": 4, "text": "d"} and g4["right"] is None
    # 对齐的一对两边的字各自带着，不互织
    assert pairs[0]["left"] == {"seq": 1, "text": "a"}
    assert pairs[0]["right"] == {"seq": 1, "text": "甲"}


def test_pure_summary_tracks_aligned_up_to():
    pairs = R.build_bridge_pairs({1: "a", 2: "b", 4: "d"}, {1: "甲", 2: "乙", 4: "丁"})
    s = R.bridge_alignment_summary(pairs)
    assert s["aligned_count"] == 3
    assert s["total_pairs"] == 4
    assert s["gap_count"] == 1
    assert s["gaps"] == [[3, 3]]
    # 前两对连续对齐、第 3 对缺、第 4 对又对齐：对齐前缀只到第 2 对
    assert s["aligned_up_to"] == 2


def test_pure_aligned_prefix_zero_when_first_pair_missing():
    # 左边从第 2 号才开始到：第一对就缺左，对齐前缀为 0
    pairs = R.build_bridge_pairs({2: "b"}, {1: "甲", 2: "乙"})
    s = R.bridge_alignment_summary(pairs)
    assert s["aligned_up_to"] == 0
    assert s["aligned_count"] == 1
    assert pairs[0]["kind"] == "gap" and pairs[0]["missing"] == "left"


def test_pure_huge_void_collapses_both_missing_range():
    # 两边都只到了 1 和 1000000000：中间两边皆缺，区间表示只有 3 个单元，
    # 绝不逐号展开出十亿个对
    pairs = R.build_bridge_pairs(
        {1: "a", 1_000_000_000: "z"}, {1: "甲", 1_000_000_000: "亥"}
    )
    assert len(pairs) == 3
    assert pairs[1]["kind"] == "gap"
    assert pairs[1]["missing"] == "both"
    assert pairs[1]["gap"] == [2, 999_999_999]
    s = R.bridge_alignment_summary(pairs)
    assert s["gaps"] == [[2, 999_999_999]]
    assert s["total_pairs"] == 1_000_000_000
    assert s["gap_count"] == 999_999_998
    assert s["aligned_up_to"] == 1


def test_pure_empty_inputs():
    assert R.build_bridge_pairs({}, {}) == []
    s = R.bridge_alignment_summary([])
    assert s == {
        "aligned_count": 0, "gap_count": 0, "total_pairs": 0,
        "aligned_up_to": 0, "gaps": [],
    }


# ------------------------------------------------------------- 搭与对齐

def test_create_bridge_and_align_pair_by_pair(client):
    feed(client, "A", ["喂", "你好", "明天见"])
    feed(client, "B", ["喂？", "你好呀"])
    b = bridge(client, "A", "B")

    assert b["bridge_no"] == "B0001"
    assert b["status"] == "active"
    assert b["dismantled_at"] is None
    assert b["left_call_id"] == "A" and b["right_call_id"] == "B"
    assert b["left"]["fragment_count"] == 3
    assert b["right"]["fragment_count"] == 2
    assert b["aligned_count"] == 2
    assert b["total_pairs"] == 3
    assert b["gap_count"] == 1
    assert b["aligned_up_to"] == 2
    assert b["gaps"] == [[3, 3]]

    aligned = [p for p in b["pairs"] if p["kind"] == "aligned"]
    gaps = [p for p in b["pairs"] if p["kind"] == "gap"]
    assert len(aligned) == 2 and len(gaps) == 1
    assert aligned[0]["left"]["text"] == "喂" and aligned[0]["right"]["text"] == "喂？"
    # 第 3 对只有左边到了：缺口标缺右，左边的字带着，右边是 null —— 不顶替
    g = gaps[0]
    assert g["seq"] == 3 and g["missing"] == "right"
    assert g["left"] == {"seq": 3, "text": "明天见"} and g["right"] is None
    assert "缺右" in g["marker"]


def test_active_bridge_alignment_advances_as_fragments_arrive(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    assert b["aligned_count"] == 1 and b["aligned_up_to"] == 1
    assert b["pairs"][-1]["missing"] == "right"

    # 缺的那边补上第 2 段：活动桥同一调用再看，自动多对上一对
    post(client, "B", 2, "b2")
    b2 = client.get(f"/bridges/{b['bridge_no']}").json()
    assert b2["aligned_count"] == 2
    assert b2["aligned_up_to"] == 2
    assert b2["gaps"] == []
    assert all(p["kind"] == "aligned" for p in b2["pairs"])
    # 桥号不变
    assert b2["bridge_no"] == "B0001"


def test_late_arrival_beyond_current_top_extends_alignment(client):
    # 后来的片段越过当前最大序号：对齐范围跟着实际到过的序号扩大
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    assert b["total_pairs"] == 1
    post(client, "A", 3, "a3")
    b2 = client.get(f"/bridges/{b['bridge_no']}").json()
    assert b2["total_pairs"] == 3
    assert [
        ("aligned",) if p["kind"] == "aligned" else (p["kind"], p["missing"])
        for p in b2["pairs"]
    ] == [
        ("aligned",), ("gap", "both"), ("gap", "right"),
    ]
    # 两边皆缺的区间合成一行，不逐号展开
    both = b2["pairs"][1]
    assert both["gap"] == [2, 2]


# ------------------------------------------------------------- 建桥约束

def test_bridge_requires_known_calls(client):
    feed(client, "A", ["a1"])
    r = client.post("/bridges", json={"left_call_id": "A", "right_call_id": "NOPE"})
    assert r.status_code == 404
    r = client.post("/bridges", json={"left_call_id": "NOPE", "right_call_id": "A"})
    assert r.status_code == 404


def test_empty_call_cannot_bridge(client):
    feed(client, "A", ["a1"])
    # C 一通空的通话不能凭空造出来：先让它以“已知但空”的样子存在
    # （越界/占位不产生正文的路径不存在，故空通话直接按未知处理 → 404）
    r = client.post("/bridges", json={"left_call_id": "A", "right_call_id": "EMPTY"})
    assert r.status_code == 404

    # 存储层直接验证：calls 里有行、但 fragments 一行没有的空通话不能拿来搭
    store = client.app.state.store
    store._conn.execute(
        "INSERT INTO calls(call_id, first_seen_at, last_activity_at) VALUES('E', 't', 't')"
    )
    store._conn.commit()
    _, result = store.create_bridge("A", "E")
    assert result == "empty"


def test_call_cannot_bridge_with_itself(client):
    feed(client, "A", ["a1", "a2"])
    r = client.post("/bridges", json={"left_call_id": "A", "right_call_id": "A"})
    assert r.status_code == 409
    assert "different calls" in r.json()["detail"]


def test_call_cannot_be_in_two_active_bridges(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    bridge(client, "A", "B")
    # A 已在活动桥里：跟 C 再搭被拒（无论在左在右）
    bridge(client, "A", "C", expected=409)
    bridge(client, "C", "A", expected=409)
    # B 同样被占着
    bridge(client, "B", "C", expected=409)
    # C 没在任何桥里，自己也不能凭空搭；A-B 仍是唯一一座活动桥
    active = [x for x in client.get("/bridges").json()["bridges"]
              if x["status"] == "active"]
    assert [(x["left_call_id"], x["right_call_id"]) for x in active] == [("A", "B")]


def test_can_rebridge_after_dismantling_history_kept(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    b1 = bridge(client, "A", "B")
    dismantle(client, b1["bridge_no"])
    # 拆了之后两通恢复自由：A 可以跟 C 搭新桥；B 也可以
    b2 = bridge(client, "A", "C")
    assert b2["bridge_no"] == "B0002"
    bridge(client, "B", "A", expected=409)  # A 此刻在 B0002 里，仍不能一女二嫁

    all_bridges = client.get("/bridges").json()["bridges"]
    assert [x["bridge_no"] for x in all_bridges] == ["B0001", "B0002"]
    assert all_bridges[0]["status"] == "dismantled"
    assert all_bridges[1]["status"] == "active"


# ------------------------------------------------------------- 拆桥留痕

def test_dismantle_freezes_alignment_but_calls_remain_independent(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    assert b["aligned_up_to"] == 1 and b["gap_count"] == 1

    snap = dismantle(client, b["bridge_no"])
    assert snap["status"] == "dismantled"
    assert snap["dismantled_at"] is not None
    assert snap["aligned_up_to"] == 1
    assert snap["gap_count"] == 1
    assert snap["pairs"][-1]["missing"] == "right"

    # 拆掉以后两通还是各自的会话，片段照常各归各
    post(client, "B", 2, "b2")
    post(client, "A", 3, "a3", is_last=True)
    a = client.get("/sessions/A").json()
    bview = client.get("/sessions/B").json()
    assert a["fragment_count"] == 3 and a["status"] == "complete"
    assert bview["fragment_count"] == 2
    # 两通内容不互织
    assert all(t in a["content"] for t in ("a1", "a2", "a3"))
    assert "b2" not in a["content"]

    # 拆掉的桥再看，仍是拆桥那一刻的对齐 —— 后来补的段碰不到这份留痕
    frozen = client.get(f"/bridges/{b['bridge_no']}").json()
    assert frozen["status"] == "dismantled"
    assert frozen["aligned_count"] == 1
    assert frozen["right"]["fragment_count"] == 1   # 拆时 B 只有 1 段
    assert frozen["pairs"][-1]["kind"] == "gap"


def test_dismantle_is_terminal_and_idempotent_conflict(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    dismantle(client, b["bridge_no"])
    # 重复拆 → 409，原快照时间不动（错误体只有原因；桥照 GET 拿）
    r = client.post(f"/bridges/{b['bridge_no']}/dismantle")
    assert r.status_code == 409
    assert client.post(f"/bridges/{b['bridge_no']}/dismantle").status_code == 409
    first = client.get(f"/bridges/{b['bridge_no']}").json()
    assert first["dismantled_at"] is not None
    # 再取仍是同一份快照、同一个拆除时刻
    again = client.get(f"/bridges/{b['bridge_no']}").json()
    assert again["dismantled_at"] == first["dismantled_at"]
    assert again["aligned_up_to"] == first["aligned_up_to"]


def test_session_view_points_to_active_bridge_and_clears_after(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    assert client.get("/sessions/A").json()["active_bridge"] is None
    b = bridge(client, "A", "B")
    va = client.get("/sessions/A").json()
    assert va["active_bridge"] == {
        "bridge_no": "B0001", "side": "left", "other_call_id": "B",
        "created_at": b["created_at"],
    }
    vb = client.get("/sessions/B").json()
    assert vb["active_bridge"]["side"] == "right"
    assert vb["active_bridge"]["other_call_id"] == "A"
    dismantle(client, b["bridge_no"])
    assert client.get("/sessions/A").json()["active_bridge"] is None
    assert client.get("/sessions/B").json()["active_bridge"] is None


# ------------------------------------------------------------- 不串、不改

def test_bridge_does_not_modify_the_two_sessions(client):
    feed(client, "A", ["a1", "a2"], )
    feed(client, "B", ["b1", "b2", "b3"])
    before_a = client.get("/sessions/A").json()
    before_b = client.get("/sessions/B").json()
    bridge(client, "A", "B")
    dismantle(client, "B0001")
    after_a = client.get("/sessions/A").json()
    after_b = client.get("/sessions/B").json()
    # 搭与拆都不碰两通各自的视图（除了专门表达桥关系的 active_bridge）
    for key in ("content", "status", "gaps", "fragment_count", "version",
                "gap_history", "retransmissions", "conflicts"):
        assert before_a[key] == after_a[key]
        assert before_b[key] == after_b[key]
    # 桥的存在不影响继续发稿/签收等既有流程
    d = client.post("/sessions/A/drafts").json()
    assert d["draft_no"] == "A-D0001" and d["content"] == "a1\na2"


def test_bridges_listed_per_call_with_side(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    b1 = bridge(client, "A", "B")
    dismantle(client, b1["bridge_no"])
    bridge(client, "B", "C")

    a_list = client.get("/sessions/A/bridges").json()["bridges"]
    b_list = client.get("/sessions/B/bridges").json()["bridges"]
    c_list = client.get("/sessions/C/bridges").json()["bridges"]
    assert [x["bridge_no"] for x in a_list] == ["B0001"]
    assert a_list[0]["side"] == "left"
    assert [x["bridge_no"] for x in b_list] == ["B0001", "B0002"]
    assert [x["side"] for x in b_list] == ["right", "left"]
    assert c_list[0]["side"] == "right"

    # 按通话列桥带 call_id 过滤；未知通话 404
    assert client.get("/sessions/NOPE/bridges").status_code == 404


def test_unknown_bridge_number_404(client):
    assert client.get("/bridges/B9999").status_code == 404
    assert client.post("/bridges/B9999/dismantle").status_code == 404


def test_bridge_numbers_are_global_and_leave_draft_namespace_alone(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    b1 = bridge(client, "A", "B")
    dismantle(client, b1["bridge_no"])
    b2 = bridge(client, "C", "D")
    assert b1["bridge_no"] == "B0001" and b2["bridge_no"] == "B0002"
    # 桥号空间与稿号、通话号都不相交
    assert client.get("/drafts/B0001").status_code == 404


def test_database_unique_index_blocks_two_active_bridges_per_call(client):
    """绕过应用层检查时，部分唯一索引仍兜底：同一通电话不能同时待在两座
    活动桥里（左右各一条）；拆桥后索引放行，历史桥行不占名额。"""
    import sqlite3

    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    b1 = bridge(client, "A", "B")

    conn = client.app.state.store._conn
    now = "2026-09-11T00:00:00+00:00"
    # A 已在 B0001 左边：再插一座 A-C 的活动桥，左索引拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bridges(bridge_no, left_call_id, right_call_id,"
            " status, created_at) VALUES('B9001','A','C','active',?)",
            (now,),
        )
    conn.rollback()
    # A 出现在右边同样被右索引拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bridges(bridge_no, left_call_id, right_call_id,"
            " status, created_at) VALUES('B9002','C','A','active',?)",
            (now,),
        )
    conn.rollback()
    # 拆了 B0001 后 A 恢复自由，应用层即可再搭
    dismantle(client, b1["bridge_no"])
    assert bridge(client, "A", "C")["bridge_no"] == "B0002"


def test_request_aliases_for_sides(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    r = client.post("/bridges", json={"a": "A", "b": "B"})
    assert r.status_code == 201, r.text
    assert r.json()["left_call_id"] == "A"


# ------------------------------------------------------------- 重启

def test_bridge_survives_restart_active_and_dismantled(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1", "a2"])
        feed(c1, "B", ["b1"])
        feed(c1, "C", ["c1"])
        feed(c1, "D", ["d1", "d2"])
        b1 = bridge(c1, "A", "B")
        dismantle(c1, b1["bridge_no"])
        bridge(c1, "C", "D")

    # 服务重新拉起：没拆的桥还在、对齐还对得上；拆过的桥仍是拆时快照
    with TestClient(create_app(db)) as c2:
        frozen = c2.get("/bridges/B0001").json()
        assert frozen["status"] == "dismantled"
        assert frozen["aligned_count"] == 1
        assert frozen["aligned_up_to"] == 1
        assert frozen["right"]["fragment_count"] == 1

        active = c2.get("/bridges/B0002").json()
        assert active["status"] == "active"
        # C 只有 1 段、D 有 2 段：只对上第 1 对，第 2 对缺左
        assert active["aligned_count"] == 1
        assert active["aligned_up_to"] == 1
        assert active["gaps"] == [[2, 2]]

        # 活动桥约束在重启后仍然有效：C/D 还在 B0002 里
        post(c2, "E", 1, "e1")
        r = c2.post("/bridges", json={"left_call_id": "C", "right_call_id": "E"})
        assert r.status_code == 409

        # 重启后继续往活动桥补段，对齐按到过的序号照常推进：
        # C 补第 3 段（仍缺第 2 段）→ 第 2 对缺左、第 3 对缺右，共两个缺口
        post(c2, "B", 2, "b2")  # B 已自由（B0001 拆了），不影响 B0002
        post(c2, "C", 3, "c3")
        again = c2.get("/bridges/B0002").json()
        assert again["aligned_count"] == 1
        assert again["gap_count"] == 2
        assert again["gaps"] == [[2, 2], [3, 3]]
        assert again["pairs"][-1]["missing"] == "right"
        missing = {p["seq"]: p["missing"] for p in again["pairs"] if p["kind"] == "gap"}
        assert missing == {2: "left", 3: "right"}

        # 重启后仍能拆活动桥，两通会话不受影响（C 到过 seq1、seq3 两段）
        dismantle(c2, "B0002")
        assert c2.get("/sessions/C").json()["fragment_count"] == 2
        assert c2.get("/sessions/C").json()["active_bridge"] is None
