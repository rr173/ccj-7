"""已发稿回放（playback）的测试：

- 拿稿号开始听，从稿的第 1 段按顺序往后，重复开始不重头；
- 轮到**发稿当时的缺口**必须停住：不前进、不跳过、不当成听完；
- 后来补上的段不会让这条旧稿回放突然变完整（要听补齐内容得另发新稿、
  另开回放，两条回放互不干扰）；
- 两通电话的回放不串：按通话列出各回各家，稿号带着通话标识；
- 听到哪了落 SQLite，重启后还停在那儿；
- 回放只读稿快照：不把稿改掉，也不把待签签掉。
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


def start(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/playback")
    assert r.status_code in (200, 201), r.text
    return r


def advance(client, draft_no):
    r = client.post(f"/drafts/{draft_no}/playback/advance")
    assert r.status_code == 200, r.text
    return r.json()


def where(client, draft_no):
    r = client.get(f"/drafts/{draft_no}/playback")
    assert r.status_code == 200, r.text
    return r.json()


def playbacks(client, call_id):
    r = client.get(f"/sessions/{call_id}/playbacks")
    assert r.status_code == 200, r.text
    return r.json()["playbacks"]


# ------------------------------------------------------------- 顺序收听

def test_playback_starts_from_first_unit_and_advances_in_order(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")

    r = start(client, d1["draft_no"])
    assert r.status_code == 201                  # 首次开始 201
    pb = r.json()
    assert pb["status"] == "playing"
    assert pb["position"] == 0                   # 还没听，从头开始
    assert pb["total_units"] == 2
    assert pb["next"] == {"seq": 1, "text": "你好"}
    assert pb["draft"] == d1                     # 回放基于当时那一稿

    first = advance(client, d1["draft_no"])
    assert first["heard"] == {"seq": 1, "text": "你好"}
    assert first["position"] == 1
    assert first["next"] == {"seq": 2, "text": "听得到吗"}

    second = advance(client, d1["draft_no"])
    assert second["heard"] == {"seq": 2, "text": "听得到吗"}
    assert second["position"] == 2
    assert second["status"] == "finished"        # 听完
    assert second["next"] is None
    assert second["finished_at"] is not None

    again = advance(client, d1["draft_no"])      # 听完后再推无效
    assert again["status"] == "finished"
    assert again["position"] == 2
    assert again["heard"] is None


def test_starting_again_keeps_position_and_does_not_restart(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    d1 = issue(client, "C1")

    start(client, d1["draft_no"])
    advance(client, d1["draft_no"])              # 听到第 1 段后

    r = start(client, d1["draft_no"])            # 又“开始”一次
    assert r.status_code == 200                  # 已存在 → 200，不重头
    pb = r.json()
    assert pb["position"] == 1                   # 还停在听到的地方
    assert pb["next"] == {"seq": 2, "text": "听得到吗"}


def test_get_or_advance_before_start_is_409_unknown_draft_404(client):
    assert client.get("/drafts/nope-D0001/playback").status_code == 404
    assert client.post("/drafts/nope-D0001/playback").status_code == 404
    assert client.post("/drafts/nope-D0001/playback/advance").status_code == 404
    assert client.get("/sessions/nope/playbacks").status_code == 404

    post(client, "C1", 1, "你好", is_last=True)
    d1 = issue(client, "C1")
    # 稿在但没开始过
    assert client.get(f"/drafts/{d1['draft_no']}/playback").status_code == 409
    assert client.post(f"/drafts/{d1['draft_no']}/playback/advance").status_code == 409


# ------------------------------------------------------------- 缺口停住

def test_playback_stops_at_the_gap_and_cannot_skip_past_it(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)   # 缺第 3 段时发稿
    d1 = issue(client, "C1")
    assert d1["gaps"] == [[3, 3]]

    start(client, d1["draft_no"])
    advance(client, d1["draft_no"])                   # 片段 1
    advance(client, d1["draft_no"])                   # 片段 2

    blocked = advance(client, d1["draft_no"])        # 轮到当时的缺口
    assert blocked["status"] == "blocked"
    assert blocked["position"] == 2                   # 位置没动
    assert blocked["heard"] is None                   # 没“听到”任何东西
    assert blocked["next"] == {"gap": [3, 3], "marker": "[缺口:片段3]"}
    assert blocked["finished_at"] is None

    again = advance(client, d1["draft_no"])          # 不能跳过去当听完
    assert again["status"] == "blocked"
    assert again["position"] == 2
    assert again["next"]["gap"] == [3, 3]

    assert where(client, d1["draft_no"])["status"] == "blocked"


def test_late_filled_fragment_does_not_complete_old_playback(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")
    start(client, d1["draft_no"])
    advance(client, d1["draft_no"])                   # 听到片段 1
    blocked = advance(client, d1["draft_no"])        # 停在缺口 [2,2]
    assert blocked["status"] == "blocked"

    # 缺口后来补上了，活视图已经完整、可以发新稿 —— 但这份旧回放不许突然变完整
    post(client, "C1", 2, "听得到吗")
    assert client.get("/sessions/C1").json()["status"] == "complete"

    still = advance(client, d1["draft_no"])
    assert still["status"] == "blocked"              # 仍停在当时的缺口
    assert still["position"] == 1
    assert where(client, d1["draft_no"])["next"]["gap"] == [2, 2]
    # 旧稿快照本身也没变
    assert client.get(f"/drafts/{d1['draft_no']}").json() == d1


def test_new_draft_has_its_own_playback_old_one_remains_blocked(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    d1 = issue(client, "C1")                          # 旧稿带缺口
    start(client, d1["draft_no"])
    advance(client, d1["draft_no"])
    advance(client, d1["draft_no"])                  # 旧回放停在缺口

    post(client, "C1", 2, "听得到吗")                 # 补齐
    d2 = issue(client, "C1")                          # 新稿完整
    assert d2["status"] == "complete"
    assert d2["supersedes"] == d1["draft_no"]

    r = start(client, d2["draft_no"])                 # 另开一条回放
    assert r.status_code == 201
    assert r.json()["position"] == 0                 # 从新稿第 1 段开始
    for _ in range(3):
        pb = advance(client, d2["draft_no"])
    assert pb["status"] == "finished"                # 新稿能一路听完

    # 旧回放不受影响，仍停在缺口
    old = where(client, d1["draft_no"])
    assert old["status"] == "blocked"
    assert old["position"] == 1

    listed = playbacks(client, "C1")                 # 两条都在，按发稿顺序
    assert [(x["draft_no"], x["status"]) for x in listed] == [
        (d1["draft_no"], "blocked"),
        (d2["draft_no"], "finished"),
    ]


# ------------------------------------------------------------- 两通电话不串

def test_playbacks_of_two_calls_never_get_mixed(client):
    post(client, "call-A", 1, "甲第一句")
    post(client, "call-A", 3, "甲第三句", is_last=True)   # 甲缺第 2 段
    post(client, "call-B", 1, "乙第一句")
    post(client, "call-B", 2, "乙第二句", is_last=True)
    da = issue(client, "call-A")
    db = issue(client, "call-B")

    start(client, da["draft_no"])
    start(client, db["draft_no"])
    advance(client, da["draft_no"])                 # 甲听到片段 1 → 停在缺口
    assert advance(client, da["draft_no"])["status"] == "blocked"
    advance(client, db["draft_no"])                 # 乙听到片段 1

    pa = where(client, da["draft_no"])
    pb = where(client, db["draft_no"])
    assert pa["call_id"] == "call-A" and pa["position"] == 1
    assert pa["next"]["gap"] == [2, 2]
    assert "乙" not in pa["draft"]["content"]
    assert pb["call_id"] == "call-B" and pb["position"] == 1
    assert pb["next"] == {"seq": 2, "text": "乙第二句"}

    # 按通话列出，结构上不可能列出别家的回放
    assert [x["draft_no"] for x in playbacks(client, "call-A")] == ["call-A-D0001"]
    assert [x["draft_no"] for x in playbacks(client, "call-B")] == ["call-B-D0001"]
    # 乙一路听完，完全碰不到甲那条仍停着的回放
    assert advance(client, db["draft_no"])["status"] == "finished"
    assert where(client, da["draft_no"])["status"] == "blocked"


# ------------------------------------------------------------- 重启停在原处

def test_playback_position_survives_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    with TestClient(create_app(db)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 2, "听得到吗")
        post(c1, "C1", 4, "那就这样", is_last=True)   # 缺第 3 段
        d1 = issue(c1, "C1")
        start(c1, d1["draft_no"])
        advance(c1, d1["draft_no"])
        advance(c1, d1["draft_no"])                  # 停在缺口
        assert where(c1, d1["draft_no"])["status"] == "blocked"

    with TestClient(create_app(db)) as c2:           # 同一数据库重新拉起
        pb = where(c2, d1["draft_no"])               # 还停在那儿
        assert pb["status"] == "blocked"
        assert pb["position"] == 2
        assert pb["next"] == {"gap": [3, 3], "marker": "[缺口:片段3]"}

        stuck = advance(c2, d1["draft_no"])          # 缺口不会因重启过去
        assert stuck["status"] == "blocked"
        assert stuck["position"] == 2

        # 重启后重复开始也不重头
        r = start(c2, d1["draft_no"])
        assert r.status_code == 200
        assert r.json()["position"] == 2


# ------------------------------------------------------------- 不改稿不签待签

def test_playback_changes_neither_draft_nor_receipt(client):
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)   # 缺第 2 段
    d1 = issue(client, "C1")
    start(client, d1["draft_no"])
    advance(client, d1["draft_no"])
    advance(client, d1["draft_no"])                  # 停在缺口

    # 稿一个字没改
    assert client.get(f"/drafts/{d1['draft_no']}").json() == d1

    # 待签没有被回放“签掉”
    receipt = client.get(f"/drafts/{d1['draft_no']}/receipt")
    assert receipt.status_code == 200
    body = receipt.json()
    assert body["status"] == "pending"
    assert body["signed_at"] is None
    assert body["draft"] == d1
