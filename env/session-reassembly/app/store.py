"""SQLite 持久化层。

所有会话状态都落在 SQLite（WAL + synchronous=FULL）里，读取时由 fragments
表现算视图，因此服务重启后正在拼接的会话原样还在，不会散。

三张表：
- fragments  : 已收到的片段，(call_id, seq) 主键 —— 重传天然去重
- calls      : 每个 call_id 的元信息（结束序号、完成时间、冲突计数）
- gap_events : 缺口历史，按**区间**记录（seq_lo..seq_hi）。缺口出现/扩大时
               写新行，补齐时填 filled_at，永不删除。区间存储保证序号空一
               大截时也只有几行、几次运算，不会逐号展开卡死。
               用来回答“这条会话曾经缺过吗”。
"""

from __future__ import annotations

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

            # 完整与否只看“到过的序号”：实际到达已连成一片、且越过结束序号，
            # 才算完整。正文上界（实际最大序号）里只要还夹着缺口，绝不置完成时间。
            status = R.status_of(seqs, last_seq)
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
            return {
                "call_id": call_id,
                "status": R.status_of(seqs, last_seq),
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
                out.append(
                    {
                        "call_id": c["call_id"],
                        "status": R.status_of(seqs, c["last_seq"]),
                        "fragment_count": len(seqs),
                        "open_gaps": open_gaps,
                        "was_incomplete": n_gap_events > 0,
                        "conflicts": c["conflicts"],
                        "first_seen_at": c["first_seen_at"],
                        "last_activity_at": c["last_activity_at"],
                        "completed_at": c["completed_at"],
                    }
                )
            return out
