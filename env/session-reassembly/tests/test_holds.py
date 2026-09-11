"""定时压稿（hold / embargo）的测试：

- 压上后没到点：拿稿只能知道“还压着、何时解”，正文和缺口一律看不见；
- 到点后不用任何操作，再拿就是发稿当时的正文和缺口；
- 解禁时刻不能早于发稿时刻，写上去就不能改、也不能提前解开，一稿只能压一次；
- 同一通电话里，后发出的稿不能比先发出的更早解；
- 压着期间签收单/认领/投递/勘误/回放/撤回等视图都不泄露正文，相关“办事”
  接口按规矩拦住；到点后同样的调用自动放行；
- 两通电话的压稿不串；
- 服务重启后，没到点的仍看不见，到了点的不用再压一次。
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

UTC = timezone.utc


class Clock:
    """可拨的 UTC 时钟：发稿、压稿、解禁判定全用它，便于跨过解禁时刻。"""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def env(tmp_path):
    clock = Clock(datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC))
    app = create_app(str(tmp_path / "test.db"), now_fn=clock)
    with TestClient(app) as c:
        yield c, clock


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


def iso(dt: datetime) -> str:
    return dt.isoformat()


CONTENT_FIELDS = [
    "content", "parts", "gaps", "gap_history", "status", "was_incomplete",
    "fragment_count", "version", "supersedes", "predecessor_had_gaps",
    "predecessor_gaps",
]


# ------------------------------------------------------------- 压着看不见

def test_held_draft_shows_only_hold_status_until_release(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)   # 缺第 3 段
    d = issue(client, "C1")
    release = clock.now + timedelta(hours=2)

    r = client.post(f"/drafts/{d['draft_no']}/hold", json={"release_at": iso(release)})
    assert r.status_code == 201, r.text
    held = r.json()

    # 身份信息、压稿状态可见；正文和缺口一律看不见
    assert held["draft_no"] == "C1-D0001"
    assert held["call_id"] == "C1"
    assert held["draft_seq"] == 1
    assert held["issued_at"]
    assert held["held_at"]
    assert held["release_at"] == iso(release)
    assert held["is_held"] is True
    assert held["released"] is False
    assert held["visibility"] == "held"
    for key in CONTENT_FIELDS:
        assert held[key] is None, key

    # 到点前再拿：仍只知道还压着、何时解
    clock.now += timedelta(hours=1)
    g = client.get("/drafts/C1-D0001")
    assert g.status_code == 200
    again = g.json()
    assert again["is_held"] is True and again["visibility"] == "held"
    for key in CONTENT_FIELDS:
        assert again[key] is None, key

    # 压稿信息接口：同样只给状态
    h = client.get("/drafts/C1-D0001/hold").json()
    assert h["is_held"] is True
    assert h["release_at"] == iso(release)
    assert h["draft"]["content"] is None
    assert h["draft"]["gaps"] is None


def test_draft_becomes_visible_at_release_without_any_action(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗")
    post(client, "C1", 4, "那就这样", is_last=True)
    d = issue(client, "C1")
    release = clock.now + timedelta(minutes=30)
    client.post(f"/drafts/{d['draft_no']}/hold", json={"release_at": iso(release)})

    # 跨过解禁时刻，不做任何“解开”操作，再拿就是当时那份正文和缺口
    clock.now = release                       # 恰好到点：now >= release_at
    visible = client.get("/drafts/C1-D0001").json()
    assert visible["is_held"] is False
    assert visible["released"] is True
    assert visible["visibility"] == "visible"
    assert visible["release_at"] == iso(release)
    assert visible["status"] == "incomplete"
    assert visible["gaps"] == [[3, 3]]
    assert "[缺口:片段3]" in visible["content"]

    # 压稿信息接口也自动翻转，但记录仍在（看得出压过、何时解的）
    h = client.get("/drafts/C1-D0001/hold").json()
    assert h["is_held"] is False and h["released"] is True
    assert h["draft"]["content"] == d["content"]
    assert h["draft"]["gaps"] == [[3, 3]]


def test_held_draft_listing_and_session_summary_stay_redacted(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    issue(client, "C1")
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})

    # 按通话列稿：列得出有这稿、何时解，正文看不见
    lst = client.get("/sessions/C1/drafts").json()["drafts"]
    assert lst[0]["release_at"] == iso(release)
    assert lst[0]["is_held"] is True
    assert lst[0]["content"] is None and lst[0]["gaps"] is None

    # scoped 取稿同样遮罩
    scoped = client.get("/sessions/C1/drafts/1")
    assert scoped.json()["content"] is None

    # 会话视图的最近一稿摘要：带压稿状态，本来就不含正文
    s = client.get("/sessions/C1").json()
    assert s["latest_draft"]["is_held"] is True
    assert s["latest_draft"]["release_at"] == iso(release)
    assert s["latest_draft"]["visibility"] == "held"

    # 会话列表摘要：数得出还压着一稿
    row = [x for x in client.get("/sessions").json()["sessions"]
           if x["call_id"] == "C1"][0]
    assert row["drafts_held"] == 1

    # 到点后摘要自动翻正
    clock.now = release
    assert client.get("/sessions/C1").json()["latest_draft"]["is_held"] is False
    row = [x for x in client.get("/sessions").json()["sessions"]
           if x["call_id"] == "C1"][0]
    assert row["drafts_held"] == 0


# ------------------------------------------------------------- 规矩

def test_release_before_issue_is_rejected_and_not_stored(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    issue(client, "C1")
    past = clock.now - timedelta(seconds=1)
    r = client.post("/drafts/C1-D0001/hold", json={"release_at": iso(past)})
    assert r.status_code == 422, r.text
    # 没落压稿记录：之后仍能用合法时刻压一次
    assert client.get("/drafts/C1-D0001/hold").status_code == 404
    future = clock.now + timedelta(minutes=5)
    assert client.post(
        "/drafts/C1-D0001/hold", json={"release_at": iso(future)}
    ).status_code == 201


def test_hold_is_once_immutable_and_cannot_be_released_early(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    issue(client, "C1")
    t1 = clock.now + timedelta(hours=2)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(t1)})

    # 一稿只能压一次：换时刻重压（想改/想提前/想延后）一律 409
    for t in (clock.now + timedelta(minutes=1),
              clock.now + timedelta(hours=5)):
        r = client.post("/drafts/C1-D0001/hold", json={"release_at": iso(t)})
        assert r.status_code == 409
    # 解禁时刻纹丝不动
    h = client.get("/drafts/C1-D0001/hold").json()
    assert h["release_at"] == iso(t1)
    assert h["held_at"]

    # 已到点自动解禁过的稿，也不能再压一次
    clock.now = t1
    client.get("/drafts/C1-D0001")  # 到点可见
    r = client.post(
        "/drafts/C1-D0001/hold",
        json={"release_at": iso(clock.now + timedelta(hours=1))},
    )
    assert r.status_code == 409


def test_unknown_draft_hold_is_404(env):
    client, _ = env
    assert client.post(
        "/drafts/nope-D0001/hold",
        json={"release_at": iso(datetime(2030, 1, 1, tzinfo=UTC))},
    ).status_code == 404
    assert client.get("/drafts/nope-D0001/hold").status_code == 404


def test_later_draft_cannot_release_earlier_than_earlier_draft(env):
    client, clock = env
    post(client, "C1", 1, "一")
    post(client, "C1", 3, "三", is_last=True)
    d1 = issue(client, "C1")
    d2 = issue(client, "C1")                       # 后发出的第二稿

    t1 = clock.now + timedelta(hours=3)
    assert client.post(
        f"/drafts/{d2['draft_no']}/hold", json={"release_at": iso(t1)}
    ).status_code == 201

    # 先发的 D0001 想压到比 D0002 更晚解 → 违反发稿顺序，409（含早发晚压）
    too_late = t1 + timedelta(hours=1)
    r = client.post(f"/drafts/{d1['draft_no']}/hold",
                    json={"release_at": iso(too_late)})
    assert r.status_code == 409

    # 同一时刻（不早于）允许：后发稿并不比先发稿“更早”解
    assert client.post(
        f"/drafts/{d1['draft_no']}/hold", json={"release_at": iso(t1)}
    ).status_code == 201

    # 之后第三稿想压到 t1 之前解，也不行
    d3 = issue(client, "C1")
    r = client.post(
        f"/drafts/{d3['draft_no']}/hold",
        json={"release_at": iso(clock.now + timedelta(hours=1))},
    )
    assert r.status_code == 409
    # 不早于 t1 就可以
    assert client.post(
        f"/drafts/{d3['draft_no']}/hold", json={"release_at": iso(t1)}
    ).status_code == 201


def test_release_order_check_is_scoped_per_call(env):
    client, clock = env
    post(client, "call-A", 1, "甲")
    post(client, "call-B", 1, "乙")
    issue(client, "call-A")
    issue(client, "call-B")
    ta = clock.now + timedelta(hours=3)
    client.post("/drafts/call-A-D0001/hold", json={"release_at": iso(ta)})
    # 另一通电话更早解不受 A 约束
    tb = clock.now + timedelta(hours=1)
    r = client.post("/drafts/call-B-D0001/hold", json={"release_at": iso(tb)})
    assert r.status_code == 201


def test_holds_listing_is_per_call_and_unknown_call_404(env):
    client, clock = env
    post(client, "call-A", 1, "甲")
    post(client, "call-B", 1, "乙")
    issue(client, "call-A")
    issue(client, "call-B")
    t = clock.now + timedelta(hours=1)
    client.post("/drafts/call-A-D0001/hold", json={"release_at": iso(t)})
    client.post("/drafts/call-B-D0001/hold", json={"release_at": iso(t)})

    a = client.get("/sessions/call-A/holds").json()["holds"]
    assert [x["draft_no"] for x in a] == ["call-A-D0001"]
    assert all(x["call_id"] == "call-A" for x in a)
    assert client.get("/sessions/nope/holds").status_code == 404


# ----------------------------------------------------- 压着期间各视图不漏

def claim(client, draft_no, by="张三"):
    return client.post(f"/drafts/{draft_no}/claim", json={"claimed_by": by})


def test_receipt_and_claim_views_redact_while_held(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    issue(client, "C1")
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})

    # 认领是元数据动作：压着也能认；但认领视图里嵌的稿仍遮罩
    assert claim(client, "C1-D0001").status_code == 201
    cv = client.get("/drafts/C1-D0001/claim").json()
    assert cv["draft"]["content"] is None and cv["draft"]["gaps"] is None

    # 签收单：压着时状态 held，嵌的稿没有正文；签收这个动作本身允许
    receipt = client.get("/drafts/C1-D0001/receipt").json()
    assert receipt["status"] == "held"
    assert receipt["is_held"] is True
    assert receipt["release_at"] == iso(release)
    assert receipt["draft"]["content"] is None

    signed = client.post("/drafts/C1-D0001/receipt")
    assert signed.status_code == 201, signed.text
    assert signed.json()["draft"]["content"] is None   # 签了也仍看不见

    # 按通话列签收单：同样遮罩
    rs = client.get("/sessions/C1/receipts").json()["receipts"]
    assert rs[0]["draft"]["content"] is None

    # 到点：签收单自动带出当时正文
    clock.now = release
    receipt = client.get("/drafts/C1-D0001/receipt").json()
    assert receipt["status"] == "signed"
    assert receipt["draft"]["content"] == "你好\n听得到吗"


def test_playback_cannot_start_or_leak_while_held(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 3, "那就这样", is_last=True)
    issue(client, "C1")
    claim(client, "C1-D0001")
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})

    # 压着不能开始回放
    assert client.post("/drafts/C1-D0001/playback").status_code == 409
    # 没开始过，取进度也是 held 语义（409），正文/单元数不会借错误体泄露
    gp = client.get("/drafts/C1-D0001/playback")
    assert gp.status_code == 409
    assert "held" in gp.json()["detail"]
    assert set(gp.json()) == {"detail"}

    # 撤回仍允许（认领人主动处置），撤回记录里的稿同样遮罩
    w = client.post("/drafts/C1-D0001/withdrawal")
    assert w.status_code == 201, w.text
    assert w.json()["draft"]["content"] is None


def test_playback_started_then_held_freezes_without_leaking(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    issue(client, "C1")
    claim(client, "C1-D0001")
    client.post("/drafts/C1-D0001/playback")
    client.post("/drafts/C1-D0001/playback/advance")   # 听到第 1 段

    # 听过之后才压：推进停住，next/heard 为空，位置保留，不泄露下一段
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})
    adv = client.post("/drafts/C1-D0001/playback/advance")
    assert adv.status_code == 200
    body = adv.json()
    assert body["status"] == "held"
    assert body["position"] == 1
    assert body["next"] is None and body["heard"] is None
    assert body["draft"]["content"] is None

    # 到点后同一回放从原位继续，无需重开、不重头
    clock.now = release
    adv = client.post("/drafts/C1-D0001/playback/advance")
    assert adv.status_code == 200
    body = adv.json()
    assert body["status"] == "finished"
    assert body["position"] == 2
    assert body["heard"] == {"seq": 2, "text": "听得到吗"}


def test_delivery_and_errata_blocked_while_held(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    issue(client, "C1")
    claim(client, "C1-D0001")
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})

    # 压着不能往下游投
    assert client.post("/drafts/C1-D0001/delivery").status_code == 409
    # 投递总览仍可查，但嵌的稿遮罩
    dv = client.get("/drafts/C1-D0001/delivery").json()
    assert dv["status"] == "none" and dv["draft"]["content"] is None

    # 压着不能出勘误（否则 old_text 就把正文带出去了）
    r = client.post(
        "/drafts/C1-D0001/errata", json={"seq": 1, "new_text": "嗨"}
    )
    assert r.status_code == 409

    # 到点：投递、勘误同一稿号自动放行
    clock.now = release
    assert client.post("/drafts/C1-D0001/delivery").status_code == 201
    e = client.post(
        "/drafts/C1-D0001/errata", json={"seq": 1, "new_text": "嗨"}
    )
    assert e.status_code == 201, e.text
    assert e.json()["old_text"] == "你好"


def test_errata_history_redacted_while_held(env):
    client, clock = env
    post(client, "C1", 1, "你好")
    post(client, "C1", 2, "听得到吗", is_last=True)
    issue(client, "C1")
    claim(client, "C1-D0001")
    # 先出勘误，后压
    client.post("/drafts/C1-D0001/errata", json={"seq": 2, "new_text": "听得见"})
    release = clock.now + timedelta(hours=1)
    client.post("/drafts/C1-D0001/hold", json={"release_at": iso(release)})

    got = client.get("/drafts/C1-D0001/errata").json()
    item = got["errata"][0]
    assert item["seq"] == 2                 # 段号是身份信息，看得出勘了哪段
    assert item["old_text"] is None         # 原文/改成什么属于正文，抹掉
    assert item["new_text"] is None
    assert got["draft"]["content"] is None

    listed = client.get("/sessions/C1/errata").json()["errata"]
    assert listed[0]["old_text"] is None and listed[0]["new_text"] is None


# ------------------------------------------------------------- 重启

def test_hold_survives_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    t0 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    release = t0 + timedelta(hours=2)

    clock1 = Clock(t0)
    with TestClient(create_app(db, now_fn=clock1)) as c1:
        post(c1, "C1", 1, "你好")
        post(c1, "C1", 3, "那就这样", is_last=True)
        d = issue(c1, "C1")
        r = c1.post(f"/drafts/{d['draft_no']}/hold",
                    json={"release_at": iso(release)})
        assert r.status_code == 201

    # 重启时仍没到点：压稿状态还在，正文缺口看不见，也不用/不能再压一次
    clock2 = Clock(t0 + timedelta(hours=1))
    with TestClient(create_app(db, now_fn=clock2)) as c2:
        g = c2.get("/drafts/C1-D0001").json()
        assert g["is_held"] is True and g["content"] is None and g["gaps"] is None
        assert c2.post(
            "/drafts/C1-D0001/hold",
            json={"release_at": iso(clock2.now + timedelta(minutes=5))},
        ).status_code == 409

    # 再重启时已到点：无需再压一次，直接就是当时的正文和缺口
    clock3 = Clock(release)
    with TestClient(create_app(db, now_fn=clock3)) as c3:
        g = c3.get("/drafts/C1-D0001").json()
        assert g["is_held"] is False
        assert g["gaps"] == [[2, 2]]
        assert "[缺口:片段2]" in g["content"]
