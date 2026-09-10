"""SQLite 持久化层。

所有会话状态都落在 SQLite（WAL + synchronous=FULL）里，读取时由 fragments
表现算视图，因此服务重启后正在拼接的会话原样还在，不会散。

七张表：
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
- receipts   : 签收单，一稿一张，与稿在同一事务里出生（signed_at 为 NULL
               即待签）。按稿号签收只把 signed_at 写上一次（UPDATE 带
               IS NULL 条件，重复签收影响 0 行），同一份签不了两次。
               签收单本身不复制正文 —— 它引用 drafts 里那份永不修改的
               快照，所以待签期间取出，看到的仍是发稿那一刻的正文和缺口。
- playbacks  : 已发稿的回放进度，一稿一条，按**稿号**开始。位置 position
               是“已听过的拼装单元数”，下一个要听的单元即 parts[position]。
               回放只读 drafts 里发稿那一刻钉死的 parts —— 后来补段、重传、
               出新稿都碰不到它：稿里当时是缺口的地方，回放轮到就停住
               （blocked），推进无效、位置不动，绝不跳过去当成听完；后来补
               上的段也不会让这条旧稿的回放突然变完整（要听补齐后的内容得
               另发新稿、另开一条回放）。本表只记录进度，永不写 drafts /
               receipts —— 回放既不改稿，也不会把待签签掉。
- late_fragments : 迟到片段记录 —— 某通电话**已发出至少一稿之后**才新入库
               的片段（内容相同的重传不算，它没带来新东西）。每笔记上片段
               本身和“到达时最新的一稿”，看得出补在哪一稿后面。只往本表
               插行，绝不碰 drafts / receipts / playbacks —— 迟到的段改
               不了已发的稿、动不了待签，也没法让停在缺口上的回放突然听完。
               记录按 call_id 归组、落在 SQLite 里：两通电话的迟到记录不
               串，重启后还在。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import reassembly as R

# 回放状态
PB_PLAYING = "playing"          # 正在顺序收听，下一个单元是正常片段
PB_BLOCKED = "blocked"          # 下一个单元是发稿当时的缺口，停住等
PB_FINISHED = "finished"        # 这一稿快照里的拼装单元已按序听完
PB_NOT_STARTED = "not_started"  # 稿在，但还没拿稿号开始过回放

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
CREATE TABLE IF NOT EXISTS receipts (
    draft_no   TEXT PRIMARY KEY REFERENCES drafts(draft_no),  -- 一稿一张签收单
    call_id    TEXT NOT NULL,     -- 这稿属于哪通电话（随稿号，永不可改）
    created_at TEXT NOT NULL,     -- 进入待签的时刻（= 发稿时间）
    signed_at  TEXT               -- NULL = 待签；签收时刻，写下后不再改
);
CREATE TABLE IF NOT EXISTS playbacks (
    draft_no   TEXT PRIMARY KEY REFERENCES drafts(draft_no),  -- 一稿一条回放
    call_id    TEXT NOT NULL,     -- 冗余自稿号所属通话，按通话查/隔离都带着它
    position   INTEGER NOT NULL DEFAULT 0,  -- 已听过的拼装单元数（0=从头开始）
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,     -- 最后一次成功推进的时刻；停在缺口时不动
    finished_at TEXT              -- 听到快照末尾的时刻；停在缺口时为 NULL
);
CREATE INDEX IF NOT EXISTS idx_playbacks_call ON playbacks(call_id);
CREATE TABLE IF NOT EXISTS late_fragments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- 到达顺序
    call_id         TEXT NOT NULL,      -- 属于哪通电话，按通话查/隔离都带着它
    seq             INTEGER NOT NULL,   -- 片段序号
    text            TEXT NOT NULL,      -- 片段内容（到达时的原文）
    after_draft_no  TEXT NOT NULL REFERENCES drafts(draft_no),  -- 补在哪一稿后面
    after_draft_seq INTEGER NOT NULL,   -- 该通话第几稿
    arrived_at      TEXT NOT NULL       -- 到达时刻（晚于那一稿的 issued_at）
);
CREATE INDEX IF NOT EXISTS idx_late_call ON late_fragments(call_id);
CREATE INDEX IF NOT EXISTS idx_late_draft ON late_fragments(after_draft_no);
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
        self._backfill_receipts()
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def _backfill_receipts(self) -> None:
        """老库升级：receipts 表是后加的，已有的稿可能还没有签收单。
        逐稿补出待签记录（已存在的不动）——“发出去的稿都要签收”对老稿
        同样成立，重启后没签完的一张不丢。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO receipts(draft_no, call_id, created_at)"
            " SELECT draft_no, call_id, issued_at FROM drafts"
        )
        self._conn.commit()

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

            # 这通电话已经对外发过稿：新入库的片段是“迟到”的 —— 单独记一笔，
            # 挂上到达时最新的一稿（看得出补在哪一稿后面）。只往
            # late_fragments 插行：已发的稿、待签的签收单、停在缺口上的
            # 回放都碰不到这笔记录，也不会被它改动。内容相同的重传不算
            # 迟到 —— 它没带来任何新东西。
            late = False
            if not duplicate:
                latest = self._conn.execute(
                    "SELECT draft_no, draft_seq FROM drafts WHERE call_id=?"
                    " ORDER BY draft_seq DESC LIMIT 1",
                    (call_id,),
                ).fetchone()
                if latest is not None:
                    late = True
                    self._conn.execute(
                        "INSERT INTO late_fragments(call_id, seq, text,"
                        " after_draft_no, after_draft_seq, arrived_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (call_id, seq, text, latest["draft_no"],
                         latest["draft_seq"], now),
                    )

            return {"stored": not duplicate, "duplicate": duplicate,
                    "conflict": conflict, "late": late}

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
            now = _now()
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
                    now,
                ),
            )
            # 稿一发出即进入待签：签收单与稿同一事务出生，不存在“发出去了
            # 却没进入待签”的中间态。这一张跟着这一稿，之后出新稿也不顶掉它
            self._conn.execute(
                "INSERT INTO receipts(draft_no, call_id, created_at) VALUES(?,?,?)",
                (draft_no, call_id, now),
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

    # ------------------------------------------------------------------ 签收

    def sign_draft(self, draft_no: str) -> tuple[dict | None, bool]:
        """按稿号签收一稿，返回 (签收单, 是否本次新签)。

        - 未知稿号 → (None, False)；
        - 已签过 → (签收单, False)：同一份不能签两次，首次签收时间不动；
        - 首次签收 → (签收单, True)。
        UPDATE 带 signed_at IS NULL 条件并以影响行数判胜负，并发下也只有
        一方能签成。稿号本身带着 call_id、一稿一号，拿一通的号永远签不到
        另一通的稿。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE receipts SET signed_at=?"
                " WHERE draft_no=? AND signed_at IS NULL",
                (_now(), draft_no),
            )
            receipt = self.get_receipt(draft_no)
            if receipt is None:
                return None, False
            return receipt, cur.rowcount > 0

    @staticmethod
    def _receipt_view(r: sqlite3.Row, draft: dict) -> dict:
        return {
            "draft_no": draft["draft_no"],
            "call_id": draft["call_id"],
            "draft_seq": draft["draft_seq"],
            "status": "signed" if r["signed_at"] else "pending",
            "issued_at": draft["issued_at"],
            "signed_at": r["signed_at"],
            # 当时那一稿的完整快照：正文、缺口……发出即冻结。待签期间后来
            # 补段、重传、会话变新都碰不到它；签完也看得出签的是哪一稿
            "draft": draft,
        }

    def get_receipt(self, draft_no: str) -> dict | None:
        """按稿号取签收单（待签或已签）。未知稿号返回 None。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT draft_no, signed_at FROM receipts WHERE draft_no=?",
                (draft_no,),
            ).fetchone()
            if r is None:
                return None
            return self._receipt_view(r, self.get_draft(r["draft_no"]))

    def list_receipts(self, call_id: str) -> list[dict] | None:
        """某通电话的全部签收单（按发稿顺序，待签已签都在）。查询本身就
        带着 call_id，结构上列不出别家的单。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT r.draft_no, r.signed_at FROM receipts r"
                " JOIN drafts d ON d.draft_no = r.draft_no"
                " WHERE r.call_id=? ORDER BY d.draft_seq",
                (call_id,),
            ).fetchall()
            return [self._receipt_view(r, self.get_draft(r["draft_no"])) for r in rows]

    # ------------------------------------------------------------------ 回放

    def start_playback(self, draft_no: str) -> tuple[dict | None, bool]:
        """拿稿号开始（或回到）一条回放，返回 (回放视图, 是否本次新开始)。

        位置只在**第一次**开始时建立（position=0）：之后再调只是取出当前
        进度，绝不会把“听到哪了”拨回开头。未知稿号返回 (None, False)。
        """
        with self._lock, self._conn:
            draft = self.get_draft(draft_no)
            if draft is None:
                return None, False
            now = _now()
            cur = self._conn.execute(
                "INSERT INTO playbacks(draft_no, call_id, position, started_at, updated_at)"
                " VALUES(?,?,0,?,?)"
                " ON CONFLICT(draft_no) DO NOTHING",
                (draft_no, draft["call_id"], now, now),
            )
            started = cur.rowcount > 0
            return self.get_playback(draft_no), started

    def advance_playback(self, draft_no: str) -> dict | None:
        """按顺序往下听一个拼装单元。

        - 下一个单元是正常片段：position 前进一格，返回该片段（heard）；
        - 下一个单元是**发稿当时的缺口**：停住 —— 不前进、不跳过，状态
          blocked，position 原封不动，updated_at 也不动；
        - 已到快照末尾：finished，重复推进无效；
        - 还没开始 / 稿号未知：None（由 API 区分 409 / 404）。
        单元列表取自 drafts 表那份永不修改的快照，所以“后来补上的段”
        永远进不到这条回放里 —— 缺口前不会突然变完整。
        """
        with self._lock, self._conn:
            pb = self._conn.execute(
                "SELECT position FROM playbacks WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if pb is None:
                return None
            draft = self.get_draft(draft_no)  # 只读快照；本方法不写 drafts
            parts = draft["parts"]
            pos = pb["position"]
            heard = None
            if pos < len(parts) and "text" in parts[pos]:
                pos += 1
                heard = parts[pos - 1]
                now = _now()
                if pos >= len(parts):
                    self._conn.execute(
                        "UPDATE playbacks SET position=?, updated_at=?,"
                        " finished_at=COALESCE(finished_at, ?) WHERE draft_no=?",
                        (pos, now, now, draft_no),
                    )
                else:
                    self._conn.execute(
                        "UPDATE playbacks SET position=?, updated_at=? WHERE draft_no=?",
                        (pos, now, draft_no),
                    )
            # 缺口（"gap" in part）或已到末尾：条件不成立，什么都不写。
            return self._playback_view(self._get_playback_row(draft_no), draft, heard)

    @staticmethod
    def _playback_state(parts: list[dict], position: int) -> str:
        if position >= len(parts):
            return PB_FINISHED
        return PB_BLOCKED if "gap" in parts[position] else PB_PLAYING

    def _playback_view(
        self, pb: sqlite3.Row | None, draft: dict | None = None, heard: dict | None = None
    ) -> dict | None:
        """把 playbacks 行 + drafts 快照装成回放视图。

        稿号未知返回 None；稿在但还没开始（playbacks 无行）由调用方决定如何
        表达 —— 本方法只服务已经开始的回放。视图里嵌整份稿快照，是同一份
        只读数据的呈现，回放推进不改它一个字。
        """
        if pb is None:
            return None
        if draft is None:
            draft = self.get_draft(pb["draft_no"])
        parts = draft["parts"]
        pos = pb["position"]
        state = self._playback_state(parts, pos)
        view = {
            "draft_no": draft["draft_no"],
            "call_id": draft["call_id"],
            "draft_seq": draft["draft_seq"],
            "status": state,
            "position": pos,              # 已听到第几个单元（下次从这里继续）
            "total_units": len(parts),
            "started_at": pb["started_at"],
            "updated_at": pb["updated_at"],
            "finished_at": pb["finished_at"],
            "next": None,                 # 下一个要听的单元；末尾为 null
            "heard": heard,               # 本次推进刚听到的片段（仅推进响应）
            "draft": draft,               # 发稿那一刻的快照，永不被回放改动
        }
        if pos < len(parts):
            unit = parts[pos]
            if "gap" in unit:
                # 轮到当时的缺口：明确告诉调用方卡在哪、为什么过不去
                view["next"] = {"gap": unit["gap"], "marker": unit["marker"]}
            else:
                view["next"] = {"seq": unit["seq"], "text": unit["text"]}
        return view

    def _get_playback_row(self, draft_no: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM playbacks WHERE draft_no=?", (draft_no,)
        ).fetchone()

    def get_playback(self, draft_no: str) -> dict | None:
        """按稿号取一条**已开始**的回放及当前进度。

        稿号未知 → None（404）；稿在但还没开始 → status=not_started 的视图
        （409 语义：还没拿稿号开始过）。"""
        with self._lock:
            draft = self.get_draft(draft_no)
            if draft is None:
                return None
            pb = self._get_playback_row(draft_no)
            if pb is None:
                return {
                    "draft_no": draft["draft_no"],
                    "call_id": draft["call_id"],
                    "draft_seq": draft["draft_seq"],
                    "status": PB_NOT_STARTED,
                    "position": 0,
                    "total_units": len(draft["parts"]),
                    "started_at": None,
                    "updated_at": None,
                    "finished_at": None,
                    "next": None,
                    "heard": None,
                    "draft": draft,
                }
            return self._playback_view(pb, draft)

    def list_playbacks(self, call_id: str) -> list[dict] | None:
        """某通电话上**已经开始**的全部回放（按发稿顺序）。

        查询本身带着 call_id，结构上列不出另一通电话的回放；没开始过的稿
        不占行（要开始得拿稿号）。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT p.* FROM playbacks p JOIN drafts d ON d.draft_no = p.draft_no"
                " WHERE p.call_id=? ORDER BY d.draft_seq",
                (call_id,),
            ).fetchall()
            return [self._playback_view(r, self.get_draft(r["draft_no"])) for r in rows]

    # ------------------------------------------------------------------ 迟到片段

    @staticmethod
    def _row_to_late(r: sqlite3.Row) -> dict:
        return {
            "call_id": r["call_id"],
            "seq": r["seq"],
            "text": r["text"],
            # 补在哪一稿后面：到达那一刻该通话最新的一稿（稿号自带 call_id）
            "after_draft_no": r["after_draft_no"],
            "after_draft_seq": r["after_draft_seq"],
            "arrived_at": r["arrived_at"],
        }

    def list_late_fragments(self, call_id: str) -> list[dict] | None:
        """某通电话的全部迟到片段（按到达顺序）。

        查询本身带着 call_id，结构上列不出另一通电话的迟到记录 —— 两通
        电话的迟到记录物理上不串。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM late_fragments WHERE call_id=? ORDER BY id",
                (call_id,),
            ).fetchall()
            return [self._row_to_late(r) for r in rows]

    def list_late_fragments_for_draft(self, draft_no: str) -> list[dict] | None:
        """补在某一稿后面的全部迟到片段（按到达顺序）：那一稿发出之后、
        下一稿发出之前新到的片段。只读 late_fragments 表 —— 稿、待签、
        回放都不受影响。未知稿号返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM drafts WHERE draft_no=?", (draft_no,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM late_fragments WHERE after_draft_no=? ORDER BY id",
                (draft_no,),
            ).fetchall()
            return [self._row_to_late(r) for r in rows]

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
