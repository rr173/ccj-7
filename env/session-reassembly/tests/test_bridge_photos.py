"""桥拍照（bridge photo）测试：

- 桥还搭着时，能把**此刻**两边对齐到哪一对拍下来（逐对、缺口、连续对齐前缀）；
- 拍完之后两边再来新段，旧照片一个字不变，桥的现行对齐仍按现在的段现算；
- 同一座桥可以拍好几次：每张看得出是第几张（photo_seq）、当时对到哪；
- 已经拆掉的桥不能再拍（拆前拍过的照片拆后照样可查）；
- 拍照不写不改两通各自的会话，也不动桥上此刻还在算的对齐；
- 照片落 SQLite：服务再起来，拍过的还在，现行对齐仍按两边现在的段来算；
- 换边之后旧照片仍钉着拍照当时的旧两边和旧对齐，不被新两边盖掉。
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


def photo(client, bridge_no, expected=201):
    r = client.post(f"/bridges/{bridge_no}/photos")
    assert r.status_code == expected, r.text
    return r.json()


def dismantle(client, bridge_no, expected=201):
    r = client.post(f"/bridges/{bridge_no}/dismantle")
    assert r.status_code == expected, r.text
    return r.json()


# ------------------------------------------------------------- 拍照内容

def test_photo_freezes_alignment_pair_by_pair(client):
    feed(client, "A", ["a1", "a2", "a3"])
    feed(client, "B", ["b1", "b2"])
    b = bridge(client, "A", "B")

    p = photo(client, b["bridge_no"])
    assert p["bridge_no"] == "B0001"
    assert p["photo_seq"] == 1                 # 第一张
    assert p["taken_at"]
    assert p["left_call_id"] == "A" and p["right_call_id"] == "B"
    # 拍时的对齐进度：前两对对齐，第 3 对缺右
    assert p["aligned_count"] == 2
    assert p["total_pairs"] == 3
    assert p["gap_count"] == 1
    assert p["aligned_up_to"] == 2
    assert p["gaps"] == [[3, 3]]
    aligned = [x for x in p["pairs"] if x["kind"] == "aligned"]
    gaps = [x for x in p["pairs"] if x["kind"] == "gap"]
    assert len(aligned) == 2 and len(gaps) == 1
    assert aligned[0]["left"]["text"] == "a1" and aligned[0]["right"]["text"] == "b1"
    g = gaps[0]
    assert g["seq"] == 3 and g["missing"] == "right"
    assert g["left"] == {"seq": 3, "text": "a3"} and g["right"] is None
    # 拍时两边各自的摘要也钉住（段数、最大序号、自身缺口）
    assert p["left"]["fragment_count"] == 3
    assert p["right"]["fragment_count"] == 2


def test_same_bridge_can_be_photographed_multiple_times_each_numbered(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")

    p1 = photo(client, b["bridge_no"])
    p2 = photo(client, b["bridge_no"])
    p3 = photo(client, b["bridge_no"])
    assert [p["photo_seq"] for p in (p1, p2, p3)] == [1, 2, 3]
    assert p1["taken_at"] <= p2["taken_at"] <= p3["taken_at"]

    # 列表按拍照先后，每张都看得出第几张、何时拍
    listed = client.get(f"/bridges/{b['bridge_no']}/photos").json()
    assert listed["photo_count"] == 3
    assert [x["photo_seq"] for x in listed["photos"]] == [1, 2, 3]
    assert [x["taken_at"] for x in listed["photos"]] == [
        p1["taken_at"], p2["taken_at"], p3["taken_at"]
    ]

    # 桥视图里也带得出拍过几张、每张对到哪
    view = client.get(f"/bridges/{b['bridge_no']}").json()
    assert view["photo_count"] == 3
    assert [x["photo_seq"] for x in view["photos"]] == [1, 2, 3]

    # 按第几张单取
    got = client.get(f"/bridges/{b['bridge_no']}/photos/2").json()
    assert got["photo_seq"] == 2 and got["aligned_up_to"] == 1


# ------------------------------------------------------------- 拍完后再来段

def test_photo_is_immutable_while_live_alignment_keeps_advancing(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    # 第一张：只对上第 1 对，第 2 对缺右
    p1 = photo(client, b["bridge_no"])
    assert p1["aligned_up_to"] == 1 and p1["gaps"] == [[2, 2]]

    # 拍完之后缺的那边补段
    post(client, "B", 2, "b2")
    post(client, "A", 3, "a3")

    # 活动桥的现行对齐仍按两边现在的段现算：对上 2 对，又新添第 3 对缺口
    live = client.get(f"/bridges/{b['bridge_no']}").json()
    assert live["aligned_count"] == 2 and live["aligned_up_to"] == 2
    assert live["gaps"] == [[3, 3]]

    # 旧照片一个字不变：仍是拍时只对上 1 对、缺在第 2 对
    old = client.get(f"/bridges/{b['bridge_no']}/photos/1").json()
    assert old["aligned_count"] == 1 and old["aligned_up_to"] == 1
    assert old["gap_count"] == 1 and old["total_pairs"] == 2
    assert old["gaps"] == [[2, 2]]
    assert old["right"]["fragment_count"] == 1     # 拍时 B 只有 1 段
    assert old["pairs"][-1]["missing"] == "right"
    assert old["pairs"][-1]["right"] is None
    assert old["taken_at"] == p1["taken_at"]

    # 再拍一张，记的是此刻的样子，跟第一张对得上差异
    p2 = photo(client, b["bridge_no"])
    assert p2["photo_seq"] == 2
    assert p2["aligned_up_to"] == 2 and p2["gaps"] == [[3, 3]]
    assert p2["right"]["fragment_count"] == 2


def test_taking_photo_does_not_modify_either_session_nor_live_alignment(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1", "b2", "b3"])
    b = bridge(client, "A", "B")
    before_a = client.get("/sessions/A").json()
    before_b = client.get("/sessions/B").json()
    before_bridge = client.get(f"/bridges/{b['bridge_no']}").json()

    photo(client, b["bridge_no"])
    photo(client, b["bridge_no"])

    after_a = client.get("/sessions/A").json()
    after_b = client.get("/sessions/B").json()
    after_bridge = client.get(f"/bridges/{b['bridge_no']}").json()
    # 两通各自的会话一个字不变（除了专门表达桥关系的 active_bridge）
    for key in ("content", "status", "gaps", "fragment_count", "version",
                "gap_history", "retransmissions", "conflicts"):
        assert before_a[key] == after_a[key]
        assert before_b[key] == after_b[key]
    # 桥上此刻还在算的对齐也不因拍照改变（照片只是多了留痕）
    for key in ("aligned_count", "gap_count", "total_pairs", "aligned_up_to",
                "gaps", "pairs"):
        assert before_bridge[key] == after_bridge[key]


# ------------------------------------------------------------- 拆掉的桥

def test_dismantled_bridge_cannot_be_photographed_but_old_photos_remain(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    p1 = photo(client, b["bridge_no"])
    dismantle(client, b["bridge_no"])

    # 已经拆掉的桥不能再拍
    r = client.post(f"/bridges/{b['bridge_no']}/photos")
    assert r.status_code == 409
    assert "dismantled" in r.json()["detail"]

    # 拆前拍过的照片拆后照样可查，仍是拍时那份对齐（不是拆桥快照）
    old = client.get(f"/bridges/{b['bridge_no']}/photos/1").json()
    assert old["photo_seq"] == 1
    assert old["aligned_up_to"] == p1["aligned_up_to"]
    assert old["gaps"] == [[2, 2]]
    listed = client.get(f"/bridges/{b['bridge_no']}/photos").json()
    assert listed["photo_count"] == 1
    # 已拆桥的视图里也还带得出拍过的照片
    view = client.get(f"/bridges/{b['bridge_no']}").json()
    assert view["status"] == "dismantled" and view["photo_count"] == 1
    assert view["photos"][0]["photo_seq"] == 1


def test_photo_kept_when_bridge_later_dismantled_with_advanced_alignment(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    photo(client, b["bridge_no"])                 # 第 1 张：对上 1 对
    post(client, "A", 2, "a2")                    # 现行对齐变成第 2 对缺右（不拍照）
    snap = dismantle(client, b["bridge_no"])      # 拆桥快照钉的是拆时（2 对范围）
    assert snap["total_pairs"] == 2
    # 第 1 张照片仍是拍照那一刻（1 对），既不跟现行跑、也不被拆桥快照盖掉
    old = client.get(f"/bridges/{b['bridge_no']}/photos/1").json()
    assert old["total_pairs"] == 1 and old["aligned_up_to"] == 1
    assert old["gaps"] == []


# ------------------------------------------------------------- 换边

def test_photo_kept_through_swap_with_old_sides_identity(client):
    feed(client, "A", ["a1", "a2"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1", "c2", "c3"])
    b = bridge(client, "A", "B")
    # 换边前拍一张：旧两边 A-B，只对上第 1 对，第 2 对缺右
    before = photo(client, b["bridge_no"])
    assert before["left_call_id"] == "A" and before["right_call_id"] == "B"
    assert before["aligned_up_to"] == 1

    # 左边换成 C：桥号不变，现行对齐按 C-B 重新现算
    r = client.post(
        f"/bridges/{b['bridge_no']}/swap",
        json={"side": "left", "call_id": "C"},
    )
    assert r.status_code == 201, r.text
    live = r.json()
    assert live["left_call_id"] == "C"
    assert live["aligned_up_to"] == 1 and live["gaps"] == [[2, 2], [3, 3]]

    # 旧照片不被新两边盖掉：仍是 A-B、拍时那份对齐
    old = client.get(f"/bridges/{b['bridge_no']}/photos/1").json()
    assert old["left_call_id"] == "A" and old["right_call_id"] == "B"
    assert old["left"]["call_id"] == "A" and old["right"]["call_id"] == "B"
    assert old["gaps"] == [[2, 2]]
    assert old["pairs"][-1]["left"]["text"] == "a2"

    # 换边后还能就着同一座桥继续拍，第 2 张记的是新两边 C-B
    p2 = photo(client, b["bridge_no"])
    assert p2["photo_seq"] == 2
    assert p2["left_call_id"] == "C" and p2["right_call_id"] == "B"
    assert p2["gaps"] == [[2, 2], [3, 3]]


# ------------------------------------------------------------- 未知/序号

def test_photo_endpoints_unknown_bridge_404(client):
    assert client.post("/bridges/B9999/photos").status_code == 404
    assert client.get("/bridges/B9999/photos").status_code == 404
    assert client.get("/bridges/B9999/photos/1").status_code == 404


def test_get_photo_seq_out_of_range_404(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    b = bridge(client, "A", "B")
    photo(client, b["bridge_no"])
    assert client.get(f"/bridges/{b['bridge_no']}/photos/2").status_code == 404
    assert client.get(f"/bridges/{b['bridge_no']}/photos/0").status_code == 404


def test_photo_numbering_is_per_bridge(client):
    feed(client, "A", ["a1"])
    feed(client, "B", ["b1"])
    feed(client, "C", ["c1"])
    feed(client, "D", ["d1"])
    b1 = bridge(client, "A", "B")
    dismantle(client, b1["bridge_no"])
    b2 = bridge(client, "C", "D")
    # 两座桥各自从第 1 张数起，不串
    p = photo(client, b2["bridge_no"])
    assert p["bridge_no"] == "B0002" and p["photo_seq"] == 1


# ------------------------------------------------------------- 重启

def test_photos_survive_restart_and_live_alignment_recomputes(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1", "a2"])
        feed(c1, "B", ["b1"])
        b = bridge(c1, "A", "B")
        p1 = photo(c1, b["bridge_no"])           # 第 1 张：对上 1 对、缺第 2 对
        post(c1, "B", 2, "b2")                   # 现行对齐推进到 2 对
        p2 = photo(c1, b["bridge_no"])           # 第 2 张：对上 2 对
        assert p2["aligned_up_to"] == 2

    # 服务重新拉起：拍过的还在，现行对齐仍按两边现在的段算
    with TestClient(create_app(db)) as c2:
        listed = c2.get("/bridges/B0001/photos").json()
        assert listed["photo_count"] == 2
        old1 = listed["photos"][0]
        old2 = listed["photos"][1]
        assert old1["photo_seq"] == 1 and old1["aligned_up_to"] == 1
        assert old1["gaps"] == [[2, 2]]
        assert old1["right"]["fragment_count"] == 1
        assert old2["photo_seq"] == 2 and old2["aligned_up_to"] == 2
        assert old1["taken_at"] == p1["taken_at"]
        assert old2["taken_at"] == p2["taken_at"]

        # 单取同样是冻结快照
        got = c2.get("/bridges/B0001/photos/1").json()
        assert got["pairs"][-1]["missing"] == "right"

        # 活动桥现行对齐不受照片影响：仍是两段对两段
        live = c2.get("/bridges/B0001").json()
        assert live["status"] == "active"
        assert live["aligned_up_to"] == 2 and live["gaps"] == []

        # 重启后还能接着拍第 3 张
        post(c2, "A", 3, "a3")
        p3 = photo(c2, "B0001")
        assert p3["photo_seq"] == 3 and p3["gaps"] == [[3, 3]]

        # 两通各自的会话重启后照常独立、没被拍照改动
        assert c2.get("/sessions/A").json()["fragment_count"] == 3
        assert c2.get("/sessions/B").json()["fragment_count"] == 2


def test_photos_of_dismantled_bridge_survive_restart(tmp_path):
    db = str(tmp_path / "restart2.db")
    with TestClient(create_app(db)) as c1:
        feed(c1, "A", ["a1", "a2"])
        feed(c1, "B", ["b1"])
        bridge(c1, "A", "B")
        photo(c1, "B0001")
        dismantle(c1, "B0001")

    with TestClient(create_app(db)) as c2:
        # 已拆桥不能再拍
        assert c2.post("/bridges/B0001/photos").status_code == 409
        # 但拆前拍的照片重启后仍在、仍是拍时那份
        old = c2.get("/bridges/B0001/photos/1").json()
        assert old["photo_seq"] == 1 and old["aligned_up_to"] == 1
        assert old["gaps"] == [[2, 2]]
        view = c2.get("/bridges/B0001").json()
        assert view["status"] == "dismantled" and view["photo_count"] == 1
