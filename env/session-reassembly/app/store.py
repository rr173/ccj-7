"""SQLite 持久化层。

所有会话状态都落在 SQLite（WAL + synchronous=FULL）里，读取时由 fragments
表现算视图，因此服务重启后正在拼接的会话原样还在，不会散。

四张表：
- fragments  : 已收到的片段，(call_id, seq) 主键 —— 重传天然去重
- calls      : 每个 call_id 的元信息（结束序号、完成时间、冲突计数）
- gap_events : 缺口历史，按**区间**记录（seq_lo..seq_hi）。缺口出现/扩大时
               写新行，补齐时填 filled_at，永不删除。区间存储保证序号空一
               大截时也只有几行、几次运算，不会逐号展开卡死。
               用来回答“这条会话曾经缺过吗”。
- drafts     : 已对外给出的稿，**INSERT-only**。发稿那一刻把正文、缺口、
               状态整体快照进来并分配稿号，之后任何补段、重传、再发新稿
               都不改这一行 —— 拿稿号查到的永远是当时那一稿。新稿用
               supersedes 指向它订正的上一稿，并记下上一稿当时的缺口，
               “订正的是哪一稿、曾经缺过”直接可查。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import reassembly as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS fragments (
    call_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    text            TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    retransmissions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (call_id, seq)
);
CREATE TABLE IF NOT EXISTS calls (
    call_id          TEXT PRIMARY KEY,
    first_seen_at    TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    last_seq         INTEGER,
    completed_at     TEXT,
    conflicts        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gap_events (
    call_id     TEXT NOT NULL,
    seq_lo      INTEGER NOT NULL,
    seq_hi      INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    filled_at   TEXT,
    PRIMARY KEY (call_id, seq_lo, seq_hi)
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_no              TEXT PRIMARY KEY,  -- 稿号：{call_id}-D{序号}，全局唯一
    call_id               TEXT NOT NULL,     -- 这一稿属于哪通电话，永不可改
    draft_seq             INTEGER NOT NULL,  -- 该通话内第几稿，从 1 递增
    status                TEXT NOT NULL,     -- 发稿那一刻的会话状态
    content               TEXT NOT NULL,     -- 钉住的正文（含当时的缺口标记）
    parts_json            TEXT NOT NULL,     -- 拼装单元快照
    gaps_json             TEXT NOT NULL,     -- 发稿那一刻的缺口区间
    gap_history_json      TEXT NOT NULL,     -- 截至发稿的缺口历史（当时的样子）
    was_incomplete        INTEGER NOT NULL,
    fragment_count        INTEGER NOT NULL,
    version               INTEGER NOT NULL,  -- 发稿时活视图的版本号
    supersedes            TEXT,              -- 本稿订正的上一稿稿号（首稿为 NULL）
    predecessor_had_gaps  INTEGER NOT NULL DEFAULT 0,  -- 上一稿当时是否带缺口
    predecessor_gaps_json TEXT NOT NULL DEFAULT '[]',  -- 上一稿当时的缺口区间
    issued_at             TEXT NOT NULL,
    UNIQUE (call_id, draft_seq)
);
"""

# 旧版 gap_events 按“每个缺失序号一行”（call_id, seq）存储。一次性迁移成区间表。
LEGACY_GAP_EVENTS_DDL = (
    "CREATE TABLE gap_events ("
    "call_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "detected_at TEXT NOT NULL, filled_at TEXT, "
    "PRIMARY KEY (call_id, seq))"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._migrate_legacy_gap_events()
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def _migrate_legacy_gap_events(self) -> None:
        """旧库（gap_events 只有 seq 一列）迁移为区间表：
        按 (call_id, filled_at) 把相邻序号合并成区间，detected_at 取最早、
        filled_at 保留（区间里只要还有没补齐的就视为未补齐，逐行对账会再校正）。
        """
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(gap_events)").fetchall()
        }
        if not cols or {"seq_lo", "seq_hi"} <= cols:
            return  # 没有旧表，或已经是新结构

        legacy = self._conn.execute(
            "SELECT call_id, seq, detected_at, filled_at FROM gap_events ORDER BY call_id, seq"
        ).fetchall()
        self._conn.execute("DROP TABLE gap_events")
        self._conn.execute(
            "CREATE TABLE gap_events ("
            "call_id TEXT NOT NULL, seq_lo INTEGER NOT NULL, seq_hi INTEGER NOT NULL, "
            "detected_at TEXT NOT NULL, filled_at TEXT, "
            "PRIMARY KEY (call_id, seq_lo, seq_hi))"
        )
        groups: dict = {}
        for r in legacy:
            groups.setdefault((r["call_id"], r["filled_at"]), []).append(r)
        for (call_id, filled_at), rows in groups.items():
            nums = [r["seq"] for r in rows]
            detected = min(r["detected_at"] for r in rows)
            for lo, hi in R.to_ranges(nums):
                self._conn.execute(
                    "INSERT INTO gap_events(call_id, seq_lo, seq_hi, detected_at, filled_at)"
                    " VALUES(?,?,?,?,?)",
                    (call_id, lo, hi, detected, filled_at),
                )

    # ------------------------------------------------------------------ 写入

    def ingest(self, call_id: str, seq: int, text: str, is_last: bool = False) -> dict:
        """接收一个片段（可乱序、可重传），返回本次接收结果。"""
        now = _now()
        with self._lock, self._conn:  # 单事务，要么全落盘要么不落
            duplicate = False
            conflict = False

            row = self._conn.execute(
                "SELECT text FROM fragments WHERE call_id=? AND seq=?",
                (call_id, seq),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO fragments(call_id, seq, text, first_seen_at)"
                    " VALUES(?,?,?,?)",
                    (call_id, seq, text, now),
                )
            else:
                # 重传：幂等去重，不重复进内容；内容不一致记冲突、留先到者
                duplicate = True
                self._conn.execute(
                    "UPDATE fragments SET retransmissions=retransmissions+1"
                    " WHERE call_id=? AND seq=?",
                    (call_id, seq),
                )
                if row["text"] != text:
                    conflict = True

            self._conn.execute(
                "INSERT INTO calls(call_id, first_seen_at, last_activity_at)"
                " VALUES(?,?,?)"
                " ON CONFLICT(call_id) DO UPDATE SET"
                "   last_activity_at=excluded.last_activity_at",
                (call_id, now, now),
            )

            if is_last:
                cur = self._conn.execute(
                    "SELECT last_seq FROM calls WHERE call_id=?", (call_id,)
                ).fetchone()["last_seq"]
                if cur is None:
                    self._conn.execute(
                        "UPDATE calls SET last_seq=? WHERE call_id=?",
                        (seq, call_id),
                    )
                elif cur != seq:
                    conflict = True  # 结束标记自相矛盾，保留先声明的

            last_seq = self._conn.execute(
                "SELECT last_seq FROM calls WHERE call_id=?", (call_id,)
            ).fetchone()["last_seq"]
            if last_seq is not None and seq > last_seq:
                conflict = True  # 越过已知末尾的片段，存下来但标记异常

            if conflict:
                self._conn.execute(
                    "UPDATE calls SET conflicts=conflicts+1 WHERE call_id=?",
                    (call_id,),
                )

            seqs = self._seqs(call_id)
            self._update_gap_events(call_id, seqs, now)
            had_gap = self._had_gap(call_id)

            # 完整与否只看“到过的序号”：实际到达已连成一片，且（见过结束标记
            # 越过其序号，或曾经缺过现已补齐）才算完整。正文上界（实际最大序号）
            # 里只要还夹着缺口，绝不置完成时间。
            status = R.status_of(seqs, last_seq, had_gap)
            if status == R.COMPLETE:
                # COALESCE：已经正经完成过，后续重传/越界片段不得挪动完成时间
                self._conn.execute(
                    "UPDATE calls SET completed_at=COALESCE(completed_at, ?)"
                    " WHERE call_id=?",
                    (now, call_id),
                )
            else:
                # 曾被过早置上完成时间、后来暴露出缺口（越界片段到达）→ 撤回，
                # 绝不能对外挂着“已完成”的时间戳、正文里却夹着缺口
                self._conn.execute(
                    "UPDATE calls SET completed_at=NULL WHERE call_id=?",
                    (call_id,),
                )

            return {"stored": not duplicate, "duplicate": duplicate, "conflict": conflict}

    def _seqs(self, call_id: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT seq FROM fragments WHERE call_id=?", (call_id,)
        ).fetchall()
        return {r["seq"] for r in rows}

    def _open_gap_ranges(self, call_id: str) -> list[tuple[int, int, str]]:
        return [
            (r["seq_lo"], r["seq_hi"], r["detected_at"])
            for r in self._conn.execute(
                "SELECT seq_lo, seq_hi, detected_at FROM gap_events"
                " WHERE call_id=? AND filled_at IS NULL ORDER BY seq_lo",
                (call_id,),
            )
        ]

    def _had_gap(self, call_id: str) -> bool:
        """这条会话是否曾经出现过缺口（无论后来是否补齐）。"""
        return self._conn.execute(
            "SELECT 1 FROM gap_events WHERE call_id=? LIMIT 1", (call_id,)
        ).fetchone() is not None

    def _update_gap_events(self, call_id: str, seqs: set[int], now: str) -> None:
        """当前缺口与 gap_events 对账，全程区间运算：

        - 旧缺口整段仍在：不动；
        - 旧缺口被整体补齐：历史行填 filled_at 关闭；
        - 旧缺口只补齐一部分（缺口缩小/被切成几段）：旧历史行关闭，仍缺的
          残留段另开一行并**继承原来的 detected_at**（这段确实从那时起就缺）；
        - 新出现/扩大连通出来的缺口段：插新行，detected_at=now。
        无论怎么变化，曾经缺过多大、何时发现、何时补齐，永远查得到。
        """
        if not seqs:
            return
        current = R.missing_ranges(seqs, max(seqs))   # 确凿缺口（更大的号已到）
        open_rows = self._open_gap_ranges(call_id)
        old_merged = R.merge_ranges([[lo, hi] for lo, hi, _ in open_rows])

        for lo, hi, detected in open_rows:
            still = R.intersect_ranges([[lo, hi]], current)
            if still != [[lo, hi]]:
                # 整段或部分已补齐 → 关闭旧历史行
                self._conn.execute(
                    "UPDATE gap_events SET filled_at=? "
                    "WHERE call_id=? AND seq_lo=? AND seq_hi=? AND filled_at IS NULL",
                    (now, call_id, lo, hi),
                )
            for slo, shi in still:
                if (slo, shi) != (lo, hi):
                    # 残留段继承原发现时间另开一行
                    self._conn.execute(
                        "INSERT OR IGNORE INTO gap_events"
                        "(call_id, seq_lo, seq_hi, detected_at)"
                        " VALUES(?,?,?,?)",
                        (call_id, slo, shi, detected),
                    )

        # 与任何旧行都不重叠的部分 = 这次新出现/新扩出来的缺口
        for lo, hi in R.subtract_ranges(current, old_merged):
            self._conn.execute(
                "INSERT OR IGNORE INTO gap_events"
                "(call_id, seq_lo, seq_hi, detected_at)"
                " VALUES(?,?,?,?)",
                (call_id, lo, hi, now),
            )

    # ------------------------------------------------------------------ 发稿

    def issue_draft(self, call_id: str) -> dict | None:
        """把当前视图钉成一稿：分配稿号、落一份**永不修改**的快照。

        稿号形如 `{call_id}-D0001`：稿号本身带着通话标识，不同通话的稿号
        空间天然不相交，两通电话的稿不可能串。新稿记录它订正的上一稿
        （supersedes）以及上一稿当时的缺口（predecessor_gaps）——缺口
        补上后出的新稿，一眼能看出订正的是哪一稿、曾经缺过。
        未知 call_id 返回 None。
        """
        with self._lock, self._conn:  # 取视图 + 写快照一个事务，不落半截
            view = self.get_session(call_id)
            if view is None:
                return None
            prev = self._conn.execute(
                "SELECT draft_no, draft_seq, gaps_json FROM drafts"
                " WHERE call_id=? ORDER BY draft_seq DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            seq = 1 if prev is None else prev["draft_seq"] + 1
            draft_no = f"{call_id}-D{seq:04d}"
            self._conn.execute(
                "INSERT INTO drafts(draft_no, call_id, draft_seq, status, content,"
                " parts_json, gaps_json, gap_history_json, was_incomplete,"
                " fragment_count, version, supersedes, predecessor_had_gaps,"
                " predecessor_gaps_json, issued_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft_no, call_id, seq, view["status"], view["content"],
                    json.dumps(view["parts"], ensure_ascii=False),
                    json.dumps(view["gaps"]),
                    json.dumps(view["gap_history"], ensure_ascii=False),
                    int(view["was_incomplete"]),
                    view["fragment_count"], view["version"],
                    None if prev is None else prev["draft_no"],
                    0 if prev is None else int(bool(json.loads(prev["gaps_json"]))),
                    "[]" if prev is None else prev["gaps_json"],
                    _now(),
                ),
            )
            return self.get_draft(draft_no)

    @staticmethod
    def _row_to_draft(r: sqlite3.Row) -> dict:
        return {
            "draft_no": r["draft_no"],
            "call_id": r["call_id"],
            "draft_seq": r["draft_seq"],
            "status": r["status"],
            "content": r["content"],
            "parts": json.loads(r["parts_json"]),
            "gaps": json.loads(r["gaps_json"]),
            "gap_history": json.loads(r["gap_history_json"]),
            "was_incomplete": bool(r["was_incomplete"]),
            "fragment_count": r["fragment_count"],
            "version": r["version"],
            "supersedes": r["supersedes"],
            "predecessor_had_gaps": bool(r["predecessor_had_gaps"]),
            "predecessor_gaps": json.loads(r["predecessor_gaps_json"]),
            "issued_at": r["issued_at"],
        }

    def get_draft(self, draft_no: str) -> dict | None:
        """按稿号取已发稿。只读 drafts 表 —— 这一稿在发出那一刻就钉死了，
        后来的补段、重传、新稿都不会改变这里返回的正文和缺口。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM drafts WHERE draft_no=?", (draft_no,)
            ).fetchone()
            return None if r is None else self._row_to_draft(r)

    def get_draft_by_seq(self, call_id: str, draft_seq: int) -> dict | None:
        """按“通话 + 第几稿”取稿：查询本身就带着 call_id，结构上不可能
        拿到另一通电话的稿。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM drafts WHERE call_id=? AND draft_seq=?",
                (call_id, draft_seq),
            ).fetchone()
            return None if r is None else self._row_to_draft(r)

    def list_drafts(self, call_id: str) -> list[dict] | None:
        """某通电话已发出的全部稿（按发稿顺序）。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM drafts WHERE call_id=? ORDER BY draft_seq",
                (call_id,),
            ).fetchall()
            return [self._row_to_draft(r) for r in rows]

    def _latest_draft_info(self, call_id: str, view: dict) -> dict | None:
        """会话视图里带的“最近一稿”摘要：稿号、发稿时间，以及活视图相对
        该稿是否已有变化（正文/状态/缺口任一不同）。变了就意味着对外给的
        那稿已经过时、该出新稿了；内容相同的重传不算变化。"""
        r = self._conn.execute(
            "SELECT draft_no, issued_at, status, content, gaps_json FROM drafts"
            " WHERE call_id=? ORDER BY draft_seq DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        if r is None:
            return None
        changed = (
            r["status"] != view["status"]
            or r["content"] != view["content"]
            or json.loads(r["gaps_json"]) != view["gaps"]
        )
        return {
            "draft_no": r["draft_no"],
            "issued_at": r["issued_at"],
            "changed_since": changed,
        }

    # ------------------------------------------------------------------ 读取

    def get_session(self, call_id: str) -> dict | None:
        """拼装单个会话的完整视图；未知 call_id 返回 None。"""
        with self._lock:
            call = self._conn.execute(
                "SELECT * FROM calls WHERE call_id=?", (call_id,)
            ).fetchone()
            if call is None:
                return None
            frags = self._conn.execute(
                "SELECT seq, text, retransmissions FROM fragments"
                " WHERE call_id=? ORDER BY seq",
                (call_id,),
            ).fetchall()
            seq_to_text = {f["seq"]: f["text"] for f in frags}
            seqs = set(seq_to_text)
            last_seq = call["last_seq"]
            top = max(seqs, default=0)

            # 正文与缺口只覆盖“到过的范围”：更大的号已到、中间缺的才是缺口；
            # 结尾片段还在路上不属于缺口，不编造标记。
            parts = R.build_parts(seq_to_text, top)
            gaps = R.missing_ranges(seqs, top) if top else []
            gap_history = [
                {
                    "range": [r["seq_lo"], r["seq_hi"]],
                    "detected_at": r["detected_at"],
                    "filled_at": r["filled_at"],
                }
                for r in self._conn.execute(
                    "SELECT seq_lo, seq_hi, detected_at, filled_at FROM gap_events"
                    " WHERE call_id=? ORDER BY seq_lo, seq_hi",
                    (call_id,),
                )
            ]
            view = {
                "call_id": call_id,
                "status": R.status_of(seqs, last_seq, bool(gap_history)),
                "version": len(frags),  # 单调递增，客户端可据此发现视图变了
                "fragment_count": len(frags),
                "last_seq": last_seq,
                "gaps": gaps,
                "was_incomplete": len(gap_history) > 0,
                "gap_history": gap_history,
                "content": R.join_content(parts),
                "parts": parts,
                "retransmissions": sum(f["retransmissions"] for f in frags),
                "conflicts": call["conflicts"],
                "first_seen_at": call["first_seen_at"],
                "last_activity_at": call["last_activity_at"],
                "completed_at": call["completed_at"],
            }
            # 最近发出的一稿 + 活视图相对它是否已变（该出新稿的信号）；
            # 没发过稿为 None
            view["latest_draft"] = self._latest_draft_info(call_id, view)
            return view

    def list_sessions(self) -> list[dict]:
        """所有会话的摘要列表（含拼接中、含缺口、已完成）。"""
        with self._lock:
            calls = self._conn.execute(
                "SELECT * FROM calls ORDER BY last_activity_at DESC"
            ).fetchall()
            out = []
            for c in calls:
                seqs = self._seqs(c["call_id"])
                top = max(seqs, default=0)
                open_gaps = R.missing_ranges(seqs, top) if top else []
                n_gap_events = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM gap_events WHERE call_id=?",
                    (c["call_id"],),
                ).fetchone()["n"]
                n_drafts = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM drafts WHERE call_id=?",
                    (c["call_id"],),
                ).fetchone()["n"]
                out.append(
                    {
                        "call_id": c["call_id"],
                        "status": R.status_of(seqs, c["last_seq"], n_gap_events > 0),
                        "fragment_count": len(seqs),
                        "open_gaps": open_gaps,
                        "was_incomplete": n_gap_events > 0,
                        "conflicts": c["conflicts"],
                        "drafts_issued": n_drafts,
                        "first_seen_at": c["first_seen_at"],
                        "last_activity_at": c["last_activity_at"],
                        "completed_at": c["completed_at"],
                    }
                )
            return out
