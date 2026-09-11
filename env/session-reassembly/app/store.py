"""SQLite 持久化层。

所有会话状态都落在 SQLite（WAL + synchronous=FULL）里，读取时由 fragments
表现算视图，因此服务重启后正在拼接的会话原样还在，不会散。

十一张表（前十张 + 勘误的 errata）：
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
               IS NULL 条件，重复签收影响 0 行），同一份签不了两次。撤回不改
               本表单：撤过的待签单仍可查，但 sign_draft 会看到 withdrawals
               并拒绝。已签的稿反过来不能撤回。
               签收单本身不复制正文 —— 它引用 drafts 里那份永不修改的
               快照，所以待签期间取出，看到的仍是发稿那一刻的正文和缺口。
- playbacks  : 已发稿的回放进度，一稿一条，按**稿号**开始。位置 position
               是“已听过的拼装单元数”，下一个要听的单元即 parts[position]。
               回放只读 drafts 里发稿那一刻钉死的 parts —— 后来补段、重传、
               出新稿都碰不到它：稿里当时是缺口的地方，回放轮到就停住
               （blocked），推进无效、位置不动，绝不跳过去当成听完；后来补
               上的段也不会让这条旧稿的回放突然变完整（要听补齐后的内容得
               另发新稿、另开一条回放）。本表只记录进度，永不写 drafts /
               receipts —— 回放既不改稿，也不会把待签签掉。稿撤回后本表进度
               原样冻结，读取状态变 withdrawn，后续推进/重开无效；没开始过的
               稿撤回后同样不能再开。
- withdrawals : 撤回记录，一稿至多一条。撤回只往本表写 withdrawn_at，并在
               读取时把稿标成 withdrawn；drafts 行本身仍 INSERT-only，撤回时
               的正文、parts、缺口快照一个字不改。已签的稿有 receipts.signed_at
               保护，不能撤回；撤回后的待签单也不能再签。正在听的稿撤回时，
               playbacks.position 保持在听到的位置，之后所有推进一律停住。
- late_fragments : 迟到片段记录 —— 某通电话**已发出至少一稿之后**才新入库
               的片段（内容相同的重传不算，它没带来新东西）。每笔记上片段
               本身和“到达时最新的一稿”，看得出补在哪一稿后面。只往本表
               插行，绝不碰 drafts / receipts / playbacks —— 迟到的段改
               不了已发的稿、动不了待签，也没法让停在缺口上的回放突然听完。
               记录按 call_id 归组、落在 SQLite 里：两通电话的迟到记录不
               串，重启后还在。
- claims     : 认领记录 —— 发出去的稿要先有人认领，没认领不能签收、不能
               回放、也不能撤回。一稿同一时刻至多一条未交出的认领
               （released_at 为 NULL 即当前持有中）：一个人认了，别人不能
               再认走；交出去（填 released_at）之后才换别人认。每次认领/
               交出只往本表插行或填 released_at，绝不碰 drafts —— 认领改
               不了那一稿当时的正文和缺口。稿号自带 call_id，拿一通的号认
               不到另一通的稿；记录落 SQLite，重启后认了谁还在。
- deliveries : 下游投递记录 —— 发出去的稿按**稿号**往下游投，投一次一行，
               每行记下这是该稿第几次投、何时投、下游是收下还是退回
               （accepted_at / returned_at 都为 NULL 即待回音）。没人认领
               不能投；上一次投出去还没回音（待回音）不能再投；退回之后才能
               再投（新起一行，attempt 递增）；收下是终态，不能退也不能再投。
               投递只往本表插行、只填自己的回音时间：drafts 里那一稿当时的
               正文、parts、缺口一个字不改。稿号自带 call_id，拿一通的号投
               不到另一通的稿；记录落 SQLite，重启后投到哪了还在。
- errata     : 勘误记录 —— 发出去的稿按**稿号**对某一段出勘误，一段至多
               一条（UNIQUE(draft_no, seq)：同一段不能出两次）。每行钉住
               对的是哪一稿、哪一段、当时的原文（old_text，取自该稿快照）
               和改成什么（new_text），看得出"哪一稿的哪一段改成什么"。
               没人认领的不能出；撤回的稿不能出；对着的段必须是该稿快照里
               真实存在的片段（缺口不是段）。勘误只往本表插行，绝不碰
               drafts —— 那一稿当时的正文和缺口一个字不改。稿号自带
               call_id，拿一通的号给另一通出不了勘误；记录落 SQLite，
               重启后出过的勘误还在。
- holds      : 压稿记录 —— 有的稿得压着，写明几点几分才能见。一稿至多一条
               （draft_no 主键：一稿只能压一次），记下解禁时刻 release_at
               （不能早于发稿时刻）。这条记录只 INSERT 不 UPDATE：写上去就
               不能改，也没有任何“提前解开”的动作 —— 解不解只由读取当时的
               时刻与 release_at 比对得出（now >= release_at 即已解），重启
               后同样按钟点判，到点的稿绝不需要“再压一次/再解一次”。没到点
               时所有取稿视图只给“还压着、何时解”（present_draft 把正文、
               parts、缺口等快照字段全抹成 null），到点后再取自然就是当时
               钉住的正文和缺口。同一通电话里后发的稿不能比先发的更早解：
               压稿时与该通话已发各稿的 release_at 对账（含早发晚压的稿），
               不满足单调则拒绝。压稿记录只往本表插行，绝不碰 drafts ——
               压不压、解没解都不改那一稿当时的正文和缺口。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from . import reassembly as R

# 回放状态
PB_PLAYING = "playing"          # 正在顺序收听，下一个单元是正常片段
PB_BLOCKED = "blocked"          # 下一个单元是发稿当时的缺口，停住等
PB_FINISHED = "finished"        # 这一稿快照里的拼装单元已按序听完
PB_WITHDRAWN = "withdrawn"      # 稿已撤回：进度停在原处，不能再往下听
PB_NOT_STARTED = "not_started"  # 稿在，但还没拿稿号开始过回放
PB_HELD = "held"                # 稿还压着：正文看不见，回放不开始也不推进

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
CREATE TABLE IF NOT EXISTS withdrawals (
    draft_no     TEXT PRIMARY KEY REFERENCES drafts(draft_no),  -- 一稿至多撤回一次
    call_id      TEXT NOT NULL,     -- 随稿号所属通话，按通话隔离都带着它
    withdrawn_at TEXT NOT NULL      -- 撤回时刻；只新增，不回写 drafts 快照
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_call ON withdrawals(call_id);
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
CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 认领顺序
    draft_no    TEXT NOT NULL REFERENCES drafts(draft_no),  -- 认的是哪一稿
    call_id     TEXT NOT NULL,      -- 随稿号所属通话，按通话隔离都带着它
    claimed_by  TEXT NOT NULL,      -- 谁认的
    claimed_at  TEXT NOT NULL,      -- 认领时刻
    released_at TEXT                -- NULL = 当前持有中；交出去的时刻
);
CREATE INDEX IF NOT EXISTS idx_claims_draft ON claims(draft_no);
CREATE INDEX IF NOT EXISTS idx_claims_call ON claims(call_id);
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 投递顺序（一稿多次投递可排）
    draft_no    TEXT NOT NULL REFERENCES drafts(draft_no),  -- 按稿号投，投的是哪一稿
    call_id     TEXT NOT NULL,      -- 随稿号所属通话，按通话查/隔离都带着它
    attempt     INTEGER NOT NULL,   -- 这一稿第几次投（退回后再投 +1）
    delivered_at TEXT NOT NULL,     -- 投出去的时刻
    accepted_at TEXT,               -- NULL = 没收下；下游收下的时刻（终态）
    returned_at TEXT,               -- NULL = 没退回；下游退回的时刻（之后才能再投）
    UNIQUE (draft_no, attempt)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_draft ON deliveries(draft_no);
CREATE INDEX IF NOT EXISTS idx_deliveries_call ON deliveries(call_id);
CREATE TABLE IF NOT EXISTS errata (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 勘误顺序
    draft_no    TEXT NOT NULL REFERENCES drafts(draft_no),  -- 对哪一稿出的
    call_id     TEXT NOT NULL,      -- 随稿号所属通话，按通话查/隔离都带着它
    seq         INTEGER NOT NULL,   -- 对着哪一段（该稿快照里的片段序号）
    old_text    TEXT NOT NULL,      -- 那一稿当时该段的原文（钉住，看得出对着什么改）
    new_text    TEXT NOT NULL,      -- 改成什么
    issued_at   TEXT NOT NULL,      -- 出勘误的时刻
    UNIQUE (draft_no, seq)          -- 同一段不能出两次
);
CREATE INDEX IF NOT EXISTS idx_errata_draft ON errata(draft_no);
CREATE INDEX IF NOT EXISTS idx_errata_call ON errata(call_id);
CREATE TABLE IF NOT EXISTS holds (
    draft_no    TEXT PRIMARY KEY REFERENCES drafts(draft_no),  -- 一稿至多压一次
    call_id     TEXT NOT NULL,      -- 随稿号所属通话，按通话隔离/单调对账都带着它
    release_at  TEXT NOT NULL,      -- 解禁时刻（UTC ISO-8601），不能早于发稿时刻
    held_at     TEXT NOT NULL       -- 压稿时刻；只新增，永不修改、不提前解
);
CREATE INDEX IF NOT EXISTS idx_holds_call ON holds(call_id);
"""

# 旧版 gap_events 按“每个缺失序号一行”（call_id, seq）存储。一次性迁移成区间表。
LEGACY_GAP_EVENTS_DDL = (
    "CREATE TABLE gap_events ("
    "call_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "detected_at TEXT NOT NULL, filled_at TEXT, "
    "PRIMARY KEY (call_id, seq))"
)


class Store:
    def __init__(self, db_path: str, now_fn: Callable[[], datetime] | None = None):
        self.db_path = db_path
        # 解禁判定必须按“现在钟点”来：可注入时钟便于把时间拨到解禁前后
        # 验证“到点自动可见、没到点仍看不见”；默认就是真实 UTC 时钟。
        self._now_dt_fn = now_fn or (lambda: datetime.now(timezone.utc))
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

    def now_dt(self) -> datetime:
        """当前时刻（带时区，UTC 可比）。压稿是否已解只由它和 release_at 判。"""
        dt = self._now_dt_fn()
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def _now(self) -> str:
        return self.now_dt().isoformat()

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
        now = self._now()
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
            now = self._now()
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
            # 撤回状态在读取时叠加；下面两个默认值由 get_draft/list_drafts 补全
            "is_withdrawn": False,
            "withdrawn_at": None,
            # 压稿状态同样在读取时叠加（holds 表 + 当前钟点）；默认没压过
            "is_held": False,          # 此刻是否仍压着（压过且已到点 → false）
            "released": True,          # 没压过视为随时可见；压着未到点 → false
            "release_at": None,        # 解禁时刻；没压过为 null
            "held_at": None,           # 压稿时刻；没压过为 null
            "visibility": "visible",   # held = 还压着，正文/缺口不可见
        }

    def _withdrawn_at(self, draft_no: str) -> str | None:
        r = self._conn.execute(
            "SELECT withdrawn_at FROM withdrawals WHERE draft_no=?", (draft_no,)
        ).fetchone()
        return None if r is None else r["withdrawn_at"]

    def _hold_row(self, draft_no: str) -> sqlite3.Row | None:
        """这一稿的压稿记录（一稿至多一条），没压过返回 None。"""
        return self._conn.execute(
            "SELECT * FROM holds WHERE draft_no=?", (draft_no,)
        ).fetchone()

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def _is_held(self, draft_no: str) -> bool:
        """这一稿此刻是否仍压着（压过、但还没到解禁时刻）。"""
        h = self._hold_row(draft_no)
        return h is not None and self.now_dt() < self._parse_ts(h["release_at"])

    # 压着期间对外一律抹掉的快照字段：正文、拼装单元、缺口、缺口历史、状态等，
    # 调用方只能拿到“还压着、何时解”，不能从任何字段反推出正文或缺口。
    _REDACTED_KEYS = (
        "status", "content", "parts", "gaps", "gap_history", "was_incomplete",
        "fragment_count", "version", "supersedes", "predecessor_had_gaps",
        "predecessor_gaps",
    )

    def present_draft(self, draft: dict | None) -> dict | None:
        """对外呈现一稿：还压着时只留身份信息和压稿状态，正文/缺口一律 null；
        没压着（含已到点自动解禁、从没压过）则原样返回当时那份快照。幂等：
        传入已经遮罩过的稿不会再处理。"""
        if draft is None or not draft.get("is_held"):
            return draft
        if draft.get("content") is None and draft.get("visibility") == "held":
            return draft  # 已经遮罩过
        view = dict(draft)
        for key in self._REDACTED_KEYS:
            view[key] = None
        view["visibility"] = "held"
        return view

    def _with_draft_lifecycle(self, draft: dict) -> dict:
        draft["withdrawn_at"] = self._withdrawn_at(draft["draft_no"])
        draft["is_withdrawn"] = draft["withdrawn_at"] is not None
        h = self._hold_row(draft["draft_no"])
        if h is None:
            # 默认值在 _row_to_draft 里已放好（没压过：可见）
            return draft
        released = self.now_dt() >= self._parse_ts(h["release_at"])
        draft["release_at"] = h["release_at"]
        draft["held_at"] = h["held_at"]
        # 到点即解：不写任何“解禁”动作，只按当前钟点判定 —— 重启后同理，
        # 到点的稿不用再压一次、也不用任何操作，再拿就是当时的正文和缺口
        draft["released"] = released
        draft["is_held"] = not released
        draft["visibility"] = "visible" if released else "held"
        return draft

    def _load_draft(self, draft_no: str) -> dict | None:
        """按稿号取完整快照（含撤回/压稿标记），**不做压稿遮罩**。
        仅供内部逻辑（回放要读 parts、勘误要读原文）使用；对外呈现一律走
        get_draft → present_draft。"""
        r = self._conn.execute(
            "SELECT * FROM drafts WHERE draft_no=?", (draft_no,)
        ).fetchone()
        return None if r is None else self._with_draft_lifecycle(self._row_to_draft(r))

    def get_draft(self, draft_no: str) -> dict | None:
        """按稿号取已发稿。正文来自 drafts 快照；撤回标记来自 withdrawals。
        后来的补段、重传、新稿都不会改变这里返回的正文和缺口。
        稿还压着（没到解禁时刻）时只回“还压着、何时解”：正文、拼装单元、
        缺口等快照字段全为 null；到点后同一调用自动给回当时那份完整快照。"""
        with self._lock:
            return self.present_draft(self._load_draft(draft_no))

    def get_draft_by_seq(self, call_id: str, draft_seq: int) -> dict | None:
        """按“通话 + 第几稿”取稿：查询本身就带着 call_id，结构上不可能
        拿到另一通电话的稿。压着时同样只给压稿状态、不给正文和缺口。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM drafts WHERE call_id=? AND draft_seq=?",
                (call_id, draft_seq),
            ).fetchone()
            if r is None:
                return None
            return self.present_draft(self._with_draft_lifecycle(self._row_to_draft(r)))

    def list_drafts(self, call_id: str) -> list[dict] | None:
        """某通电话已发出的全部稿（按发稿顺序）。未知 call_id 返回 None。
        还压着的稿在列表里同样只露压稿状态 —— 列得出有这稿、何时解，
        但正文和缺口看不见；到点后列表里自动是完整样子。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM drafts WHERE call_id=? ORDER BY draft_seq",
                (call_id,),
            ).fetchall()
            return [
                self.present_draft(self._with_draft_lifecycle(self._row_to_draft(r)))
                for r in rows
            ]

    # ------------------------------------------------------------------ 压稿

    HELD = "held"                # 压着，没到解禁时刻：正文/缺口不可见
    RELEASE_IN_PAST = "release_in_past"        # 解禁时刻早于发稿时刻
    ALREADY_HELD = "already_held"              # 一稿只能压一次
    ORDER_VIOLATION = "order_violation"        # 后发的稿不能比先发的更早解

    def hold_draft(self, draft_no: str, release_at: datetime) -> tuple[dict | None, str]:
        """把一稿压到指定时刻才见，返回 (压稿后呈现的稿, 结果)。结果为：

        - ``"held"``：压上了，release_at 就此钉死，不改、不提前解；
        - ``"already_held"``：这稿压过了 —— 一稿只能压一次（哪怕已到点）；
        - ``"release_in_past"``：解禁时刻早于这稿发出的时刻，不允许；
        - ``"order_violation"``：同一通电话里它比先发的稿更早解，不允许；
        - ``"unknown"``：稿号不存在，稿为 None。

        只往 holds 插一行，绝不 UPDATE/DELETE：解禁时刻写上去就不能改，也
        没有“提前解开”的入口 —— 解不解只由读取时的钟点决定。压稿不动
        drafts，那一稿当时的正文和缺口照样原样躺在快照里，只是没到点不呈现。
        """
        release_at = (
            release_at if release_at.tzinfo is not None
            else release_at.replace(tzinfo=timezone.utc)
        )
        with self._lock, self._conn:
            r = self._conn.execute(
                "SELECT call_id, draft_seq, issued_at FROM drafts WHERE draft_no=?",
                (draft_no,),
            ).fetchone()
            if r is None:
                return None, "unknown"
            if self._hold_row(draft_no) is not None:
                # 一稿只能压一次：已压（含已到点自动解禁）也不许重压/改时刻
                return self.get_draft(draft_no), self.ALREADY_HELD
            issued = self._parse_ts(r["issued_at"])
            if release_at < issued:
                # 解禁时刻不能早于这稿发出的时刻 —— 没发出来谈不上压
                return self.get_draft(draft_no), self.RELEASE_IN_PAST

            # 同一通电话里，后发出的稿不能比先发出的稿更早解：与这通电话
            # 每一稿的解禁时刻对账（含先发、但此刻才压的稿），按 draft_seq
            # 分两边比；不按压稿先后比 —— 规矩认的是发稿顺序。
            others = self._conn.execute(
                "SELECT h.release_at AS release_at, d.draft_seq AS draft_seq"
                " FROM holds h JOIN drafts d ON d.draft_no = h.draft_no"
                " WHERE d.call_id=?",
                (r["call_id"],),
            ).fetchall()
            for o in others:
                other_at = self._parse_ts(o["release_at"])
                if (o["draft_seq"] < r["draft_seq"] and release_at < other_at) or \
                   (o["draft_seq"] > r["draft_seq"] and other_at < release_at):
                    return self.get_draft(draft_no), self.ORDER_VIOLATION

            now_text = self._now()
            self._conn.execute(
                "INSERT INTO holds(draft_no, call_id, release_at, held_at)"
                " VALUES(?,?,?,?)",
                (draft_no, r["call_id"], release_at.isoformat(), now_text),
            )
            return self.get_draft(draft_no), self.HELD

    def get_hold(self, draft_no: str) -> dict | None:
        """按稿号取压稿信息。压着时只能知道“还压着、何时解”——正文和缺口
        不给；已到点的再拿，draft 就是当时那份完整快照。没压过返回 None。"""
        with self._lock:
            h = self._hold_row(draft_no)
            if h is None:
                return None
            draft = self._load_draft(draft_no)
            return self._hold_view(h, draft)

    def list_holds(self, call_id: str) -> list[dict] | None:
        """某通电话压过的全部稿（按发稿顺序）。查询带 call_id，两通电话的
        压稿记录不串。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT h.* FROM holds h JOIN drafts d ON d.draft_no = h.draft_no"
                " WHERE h.call_id=? ORDER BY d.draft_seq",
                (call_id,),
            ).fetchall()
            return [self._hold_view(x, self._load_draft(x["draft_no"])) for x in rows]

    def _hold_view(self, h: sqlite3.Row, draft: dict) -> dict:
        """压稿信息视图：身份信息、解禁时刻与当前压/解状态始终可见；嵌的
        draft 走 present_draft —— 压着时正文和缺口全为 null，到点才是快照。"""
        held = self.now_dt() < self._parse_ts(h["release_at"])
        return {
            "draft_no": h["draft_no"],
            "call_id": h["call_id"],
            "draft_seq": draft["draft_seq"],
            "held_at": h["held_at"],
            "release_at": h["release_at"],
            "released": not held,
            "is_held": held,
            "visibility": self.HELD if held else "visible",
            # 压着时只有“还压着、何时解”；到点后这里才是当时的正文和缺口
            "draft": self.present_draft(draft),
        }

    # ------------------------------------------------------------------ 撤回

    def withdraw_draft(self, draft_no: str) -> tuple[dict | None, str]:
        """按稿号撤回一份尚未签收的已发稿。

        返回 (撤回记录, 结果)。结果为：
        - ``"withdrawn"``：本次撤回成功；
        - ``"already_withdrawn"``：稿已经撤过，返回原撤回记录，时间不动；
        - ``"already_signed"``：稿已经签收，拒绝撤回；
        - ``"not_claimed"``：还没人认领 —— 没认领的稿不能撤；
        - ``"unknown"``：稿号不存在，记录为 None。

        撤回只在 withdrawals 插一行：drafts 的正文、parts、缺口、发稿时间全部
        不 UPDATE；receipts 的待签状态也不删不签。SQLite 外键和稿号中的
        call_id 共同保证撤的是这一个 draft_no，两通电话不能串号。
        """
        with self._lock, self._conn:
            draft = self._conn.execute(
                "SELECT call_id FROM drafts WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if draft is None:
                return None, "unknown"

            existing = self._conn.execute(
                "SELECT withdrawn_at FROM withdrawals WHERE draft_no=?",
                (draft_no,),
            ).fetchone()
            if existing is not None:
                return self.get_withdrawal(draft_no), "already_withdrawn"

            signed = self._conn.execute(
                "SELECT 1 FROM receipts WHERE draft_no=? AND signed_at IS NOT NULL",
                (draft_no,),
            ).fetchone()
            if signed is not None:
                return self.get_withdrawal(draft_no), "already_signed"

            # 下游投递与签收同构：投出去被收下是终态（不能撤）；投出去还没
            # 回音也不能撤 —— 不能在下游等着答复时把稿抽走。退回之后再撤不拦。
            latest = self._latest_delivery_row(draft_no)
            if latest is not None and latest["accepted_at"] is not None:
                return self.get_withdrawal(draft_no), "delivery_accepted"
            if latest is not None and latest["returned_at"] is None:
                return self.get_withdrawal(draft_no), "delivery_pending"

            if not self._is_claimed(draft_no):
                # 没认领不能撤：稿和待签都原样不动，等有人认领后再撤
                return None, "not_claimed"

            now = self._now()
            self._conn.execute(
                "INSERT INTO withdrawals(draft_no, call_id, withdrawn_at)"
                " VALUES(?,?,?)",
                (draft_no, draft["call_id"], now),
            )
            return self.get_withdrawal(draft_no), "withdrawn"

    def get_withdrawal(self, draft_no: str) -> dict | None:
        """取一稿的撤回记录；未撤回或稿号未知均返回 None。"""
        with self._lock:
            w = self._conn.execute(
                "SELECT * FROM withdrawals WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if w is None:
                return None
            return self._withdrawal_view(w, self.get_draft(w["draft_no"]))

    def list_withdrawals(self, call_id: str) -> list[dict] | None:
        """某通电话已经撤回的全部稿（按发稿顺序）。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT w.* FROM withdrawals w JOIN drafts d"
                " ON d.draft_no = w.draft_no"
                " WHERE w.call_id=? ORDER BY d.draft_seq",
                (call_id,),
            ).fetchall()
            return [self._withdrawal_view(r, self.get_draft(r["draft_no"])) for r in rows]

    def _withdrawal_view(self, w: sqlite3.Row, draft: dict) -> dict:
        receipt = self.get_receipt(w["draft_no"])
        return {
            "draft_no": w["draft_no"],
            "call_id": w["call_id"],
            "draft_seq": draft["draft_seq"],
            "withdrawn_at": w["withdrawn_at"],
            # 撤回后仍看得出撤的是哪一稿；draft 是发稿当时冻结的正文和缺口
            "draft": draft,
            "receipt_status": receipt["status"] if receipt is not None else None,
        }

    # ------------------------------------------------------------------ 签收

    def sign_draft(self, draft_no: str) -> tuple[dict | None, str]:
        """按稿号签收一稿，返回 (签收单, 结果)。结果为：

        - ``"signed"``：本次新签；
        - ``"already_signed"``：已签过 —— 同一份不能签两次，首次签收时间不动；
        - ``"already_withdrawn"``：已撤回 —— 撤过的稿不能再签；
        - ``"not_claimed"``：还没人认领 —— 没认领的稿不能签；
        - ``"unknown"``：稿号不存在，签收单为 None。

        UPDATE 带 signed_at IS NULL 条件并以影响行数判胜负；撤回与认领先在
        同一把锁/事务内判定，已签与已撤互斥。稿号本身带着 call_id、一稿一号，
        拿一通的号永远签不到另一通的稿。
        """
        with self._lock, self._conn:
            receipt = self.get_receipt(draft_no)
            if receipt is None:
                return None, "unknown"
            if not self._is_claimed(draft_no):
                # 没认领不能签：待签单原样欠着，等有人认领后再签
                return receipt, "not_claimed"
            withdrawn = self._conn.execute(
                "SELECT 1 FROM withdrawals WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if withdrawn is not None:
                # 撤回是终态：待签单仍在，但不能把撤过的稿再签出去
                return receipt, "already_withdrawn"

            cur = self._conn.execute(
                "UPDATE receipts SET signed_at=?"
                " WHERE draft_no=? AND signed_at IS NULL",
                (self._now(), draft_no),
            )
            return self.get_receipt(draft_no), (
                "signed" if cur.rowcount > 0 else "already_signed"
            )

    def _receipt_view(self, r: sqlite3.Row, draft: dict) -> dict:
        return {
            "draft_no": draft["draft_no"],
            "call_id": draft["call_id"],
            "draft_seq": draft["draft_seq"],
            "signed_at": r["signed_at"],
            "withdrawn_at": draft["withdrawn_at"],
            "is_withdrawn": draft["is_withdrawn"],
            "release_at": draft["release_at"],
            "is_held": draft["is_held"],
            # 签收与撤回互斥：已签不可撤，撤过不可签；没到点是 held；
            # 待签且未撤、已到点（或没压过）才是 pending
            "status": "signed" if r["signed_at"] else (
                "withdrawn" if draft["is_withdrawn"] else (
                    "held" if draft["is_held"] else "pending"
                )
            ),
            "issued_at": draft["issued_at"],
            # 当时那一稿的完整快照：正文、缺口……发出即冻结。待签期间后来
            # 补段、重传、会话变新都碰不到它；签完也看得出签的是哪一稿。
            # 稿还压着时经 present_draft 遮罩 —— 正文和缺口看不见。
            "draft": self.present_draft(draft),
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

    def start_playback(self, draft_no: str) -> tuple[dict | None, str]:
        """拿稿号开始（或回到）一条回放，返回 (回放视图, 结果)。结果为：

        - ``"started"``：本次新开始（position=0）；
        - ``"existing"``：已开始过 —— 只回到当前进度，绝不重头再听；
        - ``"withdrawn"``：稿已撤回 —— 不插行、不重开（即使此前没开始过）；
        - ``"held"``：稿还压着 —— 正文看不见，不能开始；到点自动解禁后才行；
        - ``"not_claimed"``：还没人认领 —— 没认领的稿不能听；
        - ``"unknown"``：稿号不存在，视图为 None。
        """
        with self._lock, self._conn:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None, "unknown"
            if draft["is_withdrawn"]:
                # 撤回是终态：即使此前没开始过，也不允许撤后再开一条回放
                return self.get_playback(draft_no), "withdrawn"
            if draft["is_held"]:
                # 还压着：正文和缺口都看不见，更不能听 —— 不产生任何进度。
                # 没有任何“提前解”的入口，到点后同一调用自动放行。
                return self.get_playback(draft_no), "held"
            if not self._is_claimed(draft_no):
                # 没认领不能听：不产生任何进度，等有人认领后再开始
                return self.get_playback(draft_no), "not_claimed"
            now = self._now()
            cur = self._conn.execute(
                "INSERT INTO playbacks(draft_no, call_id, position, started_at, updated_at)"
                " VALUES(?,?,0,?,?)"
                " ON CONFLICT(draft_no) DO NOTHING",
                (draft_no, draft["call_id"], now, now),
            )
            return self.get_playback(draft_no), (
                "started" if cur.rowcount > 0 else "existing"
            )

    def advance_playback(self, draft_no: str) -> dict | None:
        """按顺序往下听一个拼装单元。

        - 下一个单元是正常片段：position 前进一格，返回该片段（heard）；
        - 下一个单元是**发稿当时的缺口**：停住 —— 不前进、不跳过，状态
          blocked，position 原封不动，updated_at 也不动；
        - 稿已撤回：停在听到的位置，状态 withdrawn，不前进、不写完成时间；
        - 稿仍压着：状态 held，next/heard 为空，位置不动 —— 压着时正文
          不可见，已经听到的位置也不再外泄下一段；到点后再推自然继续；
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
            draft = self._load_draft(draft_no)  # 只读快照；本方法不写 drafts
            if draft["is_withdrawn"]:
                # 撤回到达即停：保留 position，不再推进、不跳过、不写 finished_at
                return self._playback_view(
                    self._get_playback_row(draft_no), draft, None
                )
            if draft["is_held"]:
                # 压着（先听过、后被压）：停在听到的位置，next/heard 为空，
                # 不前进、不跳过；到点自动解禁后同一调用才接着往下
                return self._playback_view(
                    self._get_playback_row(draft_no), draft, None
                )
            parts = draft["parts"]
            pos = pb["position"]
            heard = None
            if pos < len(parts) and "text" in parts[pos]:
                pos += 1
                heard = parts[pos - 1]
                now = self._now()
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
    def _playback_state(
        parts: list[dict], position: int, withdrawn: bool = False, held: bool = False
    ) -> str:
        if withdrawn:
            return PB_WITHDRAWN
        if held:
            return PB_HELD
        if position >= len(parts):
            return PB_FINISHED
        return PB_BLOCKED if "gap" in parts[position] else PB_PLAYING

    def _playback_view(
        self, pb: sqlite3.Row | None, draft: dict | None = None, heard: dict | None = None
    ) -> dict | None:
        """把 playbacks 行 + drafts 快照装成回放视图。

        稿号未知返回 None；稿在但还没开始（playbacks 无行）由调用方决定如何
        表达 —— 本方法只服务已经开始的回放。视图里嵌整份稿快照，是同一份
        只读数据的呈现，回放推进不改它一个字。稿还压着时状态为 held，
        next 为空、嵌的 draft 也经 present_draft 遮罩，正文和缺口不外泄。
        """
        if pb is None:
            return None
        if draft is None:
            draft = self._load_draft(pb["draft_no"])
        parts = draft["parts"]
        pos = pb["position"]
        state = self._playback_state(
            parts, pos, draft["is_withdrawn"], draft["is_held"]
        )
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
            # 发稿那一刻的快照，永不被回放改动；压着时由 present_draft 遮罩
            "draft": self.present_draft(draft),
        }
        if state == PB_WITHDRAWN or state == PB_HELD:
            view["next"] = None           # 撤回/压着都不给下一个单元
        elif pos < len(parts):
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
        （409 语义：还没拿稿号开始过）。稿还压着时即使开始过也只回 held：
        进度不删，但 next 为空、嵌的稿经遮罩，正文不外泄。"""
        with self._lock:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None
            pb = self._get_playback_row(draft_no)
            if pb is None:
                status = PB_NOT_STARTED
                if draft["is_withdrawn"]:
                    status = PB_WITHDRAWN
                elif draft["is_held"]:
                    status = PB_HELD
                return {
                    "draft_no": draft["draft_no"],
                    "call_id": draft["call_id"],
                    "draft_seq": draft["draft_seq"],
                    "status": status,
                    "position": 0,
                    # 压着时连拼装单元数也不给（单元数会暴露缺口数）
                    "total_units": None if draft["is_held"] else len(draft["parts"]),
                    "started_at": None,
                    "updated_at": None,
                    "finished_at": None,
                    "next": None,
                    "heard": None,
                    "draft": self.present_draft(draft),
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
            return [self._playback_view(r, self._load_draft(r["draft_no"])) for r in rows]

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

    # ------------------------------------------------------------------ 认领

    def _open_claim_row(self, draft_no: str) -> sqlite3.Row | None:
        """当前持有中的认领（released_at 为 NULL 的那一条），没有则 None。"""
        return self._conn.execute(
            "SELECT * FROM claims WHERE draft_no=? AND released_at IS NULL",
            (draft_no,),
        ).fetchone()

    def _is_claimed(self, draft_no: str) -> bool:
        """这一稿此刻是否有人认领着 —— 签收、回放、撤回的前置条件。"""
        return self._open_claim_row(draft_no) is not None

    def claim_draft(self, draft_no: str, claimed_by: str) -> tuple[dict | None, str]:
        """按稿号认领一稿，返回 (认领视图, 结果)。结果为：

        - ``"claimed"``：本次认领成功，此后别人不能再认走；
        - ``"reclaimed"``：就是当前持有人本人重复认领，幂等，时间不动；
        - ``"already_held"``：别人正认领着，不能抢 —— 得等对方交出去；
        - ``"unknown"``：稿号不存在，视图为 None。

        认领只往 claims 插一行：drafts 里那一稿当时的正文和缺口一个字不改。
        稿号自带 call_id，拿一通的号认不到另一通的稿。
        """
        with self._lock, self._conn:
            draft = self._conn.execute(
                "SELECT call_id FROM drafts WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if draft is None:
                return None, "unknown"
            open_claim = self._open_claim_row(draft_no)
            if open_claim is not None:
                if open_claim["claimed_by"] == claimed_by:
                    return self.get_claim(draft_no), "reclaimed"
                return self.get_claim(draft_no), "already_held"
            self._conn.execute(
                "INSERT INTO claims(draft_no, call_id, claimed_by, claimed_at)"
                " VALUES(?,?,?,?)",
                (draft_no, draft["call_id"], claimed_by, self._now()),
            )
            return self.get_claim(draft_no), "claimed"

    def release_claim(self, draft_no: str) -> tuple[dict | None, str]:
        """把当前认领交出去，返回 (认领视图, 结果)。结果为：

        - ``"released"``：本次交出成功，之后换别人认领；
        - ``"not_claimed"``：当前没人认领着，无可交；
        - ``"unknown"``：稿号不存在，视图为 None。

        交出只填当前那条认领的 released_at（UPDATE 带 IS NULL 条件，影响 0 行
        即没人持有）：历史认领记录一行不删，稿和签收单都碰不到。
        """
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM drafts WHERE draft_no=?", (draft_no,)
            ).fetchone()
            if exists is None:
                return None, "unknown"
            cur = self._conn.execute(
                "UPDATE claims SET released_at=?"
                " WHERE draft_no=? AND released_at IS NULL",
                (self._now(), draft_no),
            )
            if cur.rowcount == 0:
                return self.get_claim(draft_no), "not_claimed"
            return self.get_claim(draft_no), "released"

    def get_claim(self, draft_no: str) -> dict | None:
        """按稿号取认领状态（当前谁认着 + 历次认领/交出记录）。
        未知稿号返回 None。"""
        with self._lock:
            draft = self.get_draft(draft_no)
            if draft is None:
                return None
            return self._claim_view(draft)

    def list_claims(self, call_id: str) -> list[dict] | None:
        """某通电话全部稿的认领状态（按发稿顺序）。查询本身带着 call_id，
        结构上列不出另一通电话的认领。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT draft_no FROM drafts WHERE call_id=? ORDER BY draft_seq",
                (call_id,),
            ).fetchall()
            return [self._claim_view(self.get_draft(r["draft_no"])) for r in rows]

    def _claim_view(self, draft: dict) -> dict:
        rows = self._conn.execute(
            "SELECT claimed_by, claimed_at, released_at FROM claims"
            " WHERE draft_no=? ORDER BY id",
            (draft["draft_no"],),
        ).fetchall()
        history = [
            {
                "claimed_by": r["claimed_by"],
                "claimed_at": r["claimed_at"],
                "released_at": r["released_at"],
            }
            for r in rows
        ]
        current = next((h for h in history if h["released_at"] is None), None)
        return {
            "draft_no": draft["draft_no"],
            "call_id": draft["call_id"],
            "draft_seq": draft["draft_seq"],
            "status": "claimed" if current is not None else "unclaimed",
            "claimed_by": None if current is None else current["claimed_by"],
            "claimed_at": None if current is None else current["claimed_at"],
            # 历次认领/交出全留痕：谁认过、何时认、何时交出去
            "history": history,
            # 当时那一稿的完整快照 —— 认领动作改不了它的正文和缺口
            "draft": draft,
        }

    # ------------------------------------------------------------------ 下游投递

    DEL_PENDING = "pending"    # 投出去了，下游还没给回音
    DEL_ACCEPTED = "accepted"  # 下游收下 —— 终态：不能退、不能再投
    DEL_RETURNED = "returned"  # 下游退回 —— 退了之后才能再投
    DEL_NONE = "none"          # 还没往下游投过

    def _latest_delivery_row(self, draft_no: str) -> sqlite3.Row | None:
        """这一稿最近一次投递（attempt 最大的那行），没投过返回 None。"""
        return self._conn.execute(
            "SELECT * FROM deliveries WHERE draft_no=?"
            " ORDER BY attempt DESC LIMIT 1",
            (draft_no,),
        ).fetchone()

    @staticmethod
    def _delivery_status(row: sqlite3.Row | None) -> str:
        if row is None:
            return Store.DEL_NONE
        if row["accepted_at"] is not None:
            return Store.DEL_ACCEPTED
        if row["returned_at"] is not None:
            return Store.DEL_RETURNED
        return Store.DEL_PENDING

    def deliver_draft(self, draft_no: str) -> tuple[dict | None, str]:
        """按稿号把一稿投给下游，返回 (投递视图, 结果)。结果为：

        - ``"delivered"``：本次投出去（新起一次投递，attempt+1）；
        - ``"pending"``：上一次投出去还没回音 —— 不能再投一次；
        - ``"accepted"``：下游已经收下 —— 收下后不能再投；
        - ``"withdrawn"``：稿已撤回 —— 撤过的稿不能投；
        - ``"held"``：稿还压着 —— 没到点、正文不可见，不能投；
        - ``"not_claimed"``：还没人认领 —— 没人认领的不能投；
        - ``"unknown"``：稿号不存在，视图为 None。

        投递只往 deliveries 插一行：drafts 里那一稿当时的正文、parts、缺口
        一个字不改。稿号自带 call_id、一稿一号，拿一通的号投不到另一通的稿。
        """
        with self._lock, self._conn:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None, "unknown"
            if not self._is_claimed(draft_no):
                # 没人认领的不能投：不产生投递记录，等有人认领后再投
                return self.get_delivery(draft_no), "not_claimed"
            if draft["is_withdrawn"]:
                return self.get_delivery(draft_no), "withdrawn"
            if draft["is_held"]:
                # 还压着不能投：不能把看不见正文的稿送到下游；到点自动解禁
                return self.get_delivery(draft_no), "held"
            latest = self._latest_delivery_row(draft_no)
            status = self._delivery_status(latest)
            if status == self.DEL_PENDING:
                # 投出去还没回音之前不能再投：返回还欠着回音的那一次
                return self.get_delivery(draft_no), "pending"
            if status == self.DEL_ACCEPTED:
                return self.get_delivery(draft_no), "accepted"
            attempt = 1 if latest is None else latest["attempt"] + 1
            now = self._now()
            self._conn.execute(
                "INSERT INTO deliveries(draft_no, call_id, attempt, delivered_at)"
                " VALUES(?,?,?,?)",
                (draft_no, draft["call_id"], attempt, now),
            )
            return self.get_delivery(draft_no), "delivered"

    def decide_draft(self, draft_no: str, accepted: bool) -> tuple[dict | None, str]:
        """下游对最近一次待回音的投递给答复：收下或退回。结果为：

        - ``"accepted"``/``"returned"``：本次答复写进了那次投递；
        - ``"not_pending"``：最近一次投递已经有回音，或从没投过 —— 收下后
          不能再退，退回后也不能再退，没投过无从答复；
        - ``"withdrawn"``：稿已撤回 —— 撤过的稿不再收下游答复；
        - ``"held"``：稿还压着 —— 压着期间不往下游办，也不收答复；
        - ``"not_claimed"``：还没人认领 —— 没认领的稿没有下游在办；
        - ``"unknown"``：稿号不存在，视图为 None。

        答复只 UPDATE deliveries 自己那行的回音时间（带“仍是待回音”条件，
        影响 0 行即已被答复过），绝不碰 drafts —— 投递和答复都改不了那一稿
        当时的正文和缺口。
        """
        with self._lock, self._conn:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None, "unknown"
            if not self._is_claimed(draft_no):
                return self.get_delivery(draft_no), "not_claimed"
            if draft["is_withdrawn"]:
                return self.get_delivery(draft_no), "withdrawn"
            if draft["is_held"]:
                return self.get_delivery(draft_no), "held"
            latest = self._latest_delivery_row(draft_no)
            if latest is None or latest["accepted_at"] is not None \
                    or latest["returned_at"] is not None:
                return self.get_delivery(draft_no), "not_pending"
            now = self._now()
            col = "accepted_at" if accepted else "returned_at"
            cur = self._conn.execute(
                f"UPDATE deliveries SET {col}=?"
                " WHERE id=? AND accepted_at IS NULL AND returned_at IS NULL",
                (now, latest["id"]),
            )
            if cur.rowcount == 0:
                return self.get_delivery(draft_no), "not_pending"
            return self.get_delivery(draft_no), (
                self.DEL_ACCEPTED if accepted else self.DEL_RETURNED
            )

    def _delivery_view(self, draft: dict) -> dict:
        """一稿的投递总览：当前状态 + 历次投递（按 attempt 排序）。

        每次投递都带稿号、通话、第几稿、第几次投、投出/收下/退回时间，投完
        看得出投的是哪一稿；draft 嵌的是 drafts 表那份 INSERT-only 快照，
        投递、退回、再投、收下都改不了它的正文和缺口。"""
        rows = self._conn.execute(
            "SELECT attempt, delivered_at, accepted_at, returned_at"
            " FROM deliveries WHERE draft_no=? ORDER BY attempt",
            (draft["draft_no"],),
        ).fetchall()
        attempts = [
            {
                "attempt": r["attempt"],
                "delivered_at": r["delivered_at"],
                "accepted_at": r["accepted_at"],
                "returned_at": r["returned_at"],
                "status": self._delivery_status(r),
            }
            for r in rows
        ]
        latest = attempts[-1] if attempts else None
        return {
            "draft_no": draft["draft_no"],
            "call_id": draft["call_id"],
            "draft_seq": draft["draft_seq"],
            # 当前投到哪了：没投过 none / 待回音 pending / 已收下 accepted /
            # 已退回 returned（退了之后才能再投）
            "status": self._delivery_status(self._latest_delivery_row(draft["draft_no"])),
            "attempt_count": len(attempts),
            "current_attempt": None if latest is None else latest["attempt"],
            "attempts": attempts,
            "delivered_at": None if latest is None else latest["delivered_at"],
            "accepted_at": None if latest is None else latest["accepted_at"],
            "returned_at": None if latest is None else latest["returned_at"],
            # 发稿当时的完整快照 —— 投递流转改不了它的正文和缺口；压着时遮罩
            "draft": self.present_draft(draft),
        }

    def get_delivery(self, draft_no: str) -> dict | None:
        """按稿号取投递总览（从未投过 status=none）。未知稿号返回 None。
        稿还压着时只给投递状态，嵌的稿快照经遮罩，正文和缺口看不见。"""
        with self._lock:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None
            return self._delivery_view(draft)

    def list_deliveries(self, call_id: str) -> list[dict] | None:
        """某通电话全部稿的投递状态（按发稿顺序）。查询本身带着 call_id，
        结构上列不出另一通电话的投递。未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT draft_no FROM drafts WHERE call_id=? ORDER BY draft_seq",
                (call_id,),
            ).fetchall()
            return [self._delivery_view(self._load_draft(r["draft_no"])) for r in rows]

    # ------------------------------------------------------------------ 勘误

    def issue_errata(self, draft_no: str, seq: int, new_text: str) -> tuple[dict | None, str]:
        """按稿号对一稿的某一段出勘误，返回 (勘误记录, 结果)。结果为：

        - ``"issued"``：本次勘误出具成功；
        - ``"already_issued"``：这一稿的这一段已经出过 —— 同一段不能出两次，
          返回原勘误记录，时间不动；
        - ``"unknown_seq"``：这一段不在该稿快照里（缺口不是段、序号越出
          该稿范围）—— 勘误必须对着这一稿真实发出的段；
        - ``"withdrawn"``：稿已撤回 —— 撤过的稿不再出勘误；
        - ``"held"``：稿还压着 —— 正文都看不见，不能对着它出勘误；
        - ``"not_claimed"``：还没人认领 —— 没人认领的不能出；
        - ``"unknown"``：稿号不存在，记录为 None。

        勘误只往 errata 插一行：drafts 里那一稿当时的正文、parts、缺口一个
        字不改。old_text 取自该稿快照并随记录钉住，勘误单自带"对的是哪一稿
        的哪一段、当时是什么、改成什么"。稿号自带 call_id、一稿一号，拿一
        通的号给另一通出不了勘误。
        """
        with self._lock, self._conn:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None, "unknown"
            if not self._is_claimed(draft_no):
                # 没人认领的不能出：不产生任何勘误记录，等有人认领后再出
                return None, "not_claimed"
            if draft["is_withdrawn"]:
                return None, "withdrawn"
            if draft["is_held"]:
                # 还压着：原文不可见，不许对着看不见的正文出勘误（也防止
                # 响应借 old_text 把正文带出去）。到点后同稿号自然放行。
                return None, "held"
            # 对着哪一段：必须是这一稿快照里真实存在的片段（缺口不是段，
            # 越出该稿范围的序号也不是）
            text_by_seq = {p["seq"]: p["text"] for p in draft["parts"] if "text" in p}
            if seq not in text_by_seq:
                return None, "unknown_seq"
            existing = self._conn.execute(
                "SELECT * FROM errata WHERE draft_no=? AND seq=?",
                (draft_no, seq),
            ).fetchone()
            if existing is not None:
                # 同一段不能出两次：原记录原样返回，时间不动
                return self._erratum_view(existing, draft), "already_issued"
            self._conn.execute(
                "INSERT INTO errata(draft_no, call_id, seq, old_text, new_text,"
                " issued_at) VALUES(?,?,?,?,?,?)",
                (draft_no, draft["call_id"], seq, text_by_seq[seq], new_text, self._now()),
            )
            row = self._conn.execute(
                "SELECT * FROM errata WHERE draft_no=? AND seq=?",
                (draft_no, seq),
            ).fetchone()
            return self._erratum_view(row, draft), "issued"

    def _erratum_view(self, r: sqlite3.Row, draft: dict) -> dict:
        held = draft.get("is_held", False)
        return {
            # 对的是哪一稿、哪一段 —— 身份信息始终在；压着时当时原文和改成
            # 什么都是正文的一部分，一律抹成 null，到点后再拿才看得见
            "draft_no": r["draft_no"],
            "call_id": r["call_id"],
            "draft_seq": draft["draft_seq"],
            "seq": r["seq"],
            "old_text": None if held else r["old_text"],
            "new_text": None if held else r["new_text"],
            "issued_at": r["issued_at"],
            # 当时那一稿的完整快照 —— 出勘误改不了它的正文和缺口；压着时遮罩
            "draft": self.present_draft(draft),
        }

    def get_errata(self, draft_no: str) -> dict | None:
        """按稿号取这一稿出过的全部勘误（按段序）。未知稿号返回 None。
        稿还压着时只看得出勘了哪几段（段序），原文/改成什么/稿快照都不露。"""
        with self._lock:
            draft = self._load_draft(draft_no)
            if draft is None:
                return None
            rows = self._conn.execute(
                "SELECT * FROM errata WHERE draft_no=? ORDER BY seq",
                (draft_no,),
            ).fetchall()
            return {
                "draft_no": draft["draft_no"],
                "call_id": draft["call_id"],
                "draft_seq": draft["draft_seq"],
                "errata": [self._erratum_view(r, draft) for r in rows],
                # 当时那一稿的快照原样嵌着 —— 勘误碰不到它的正文和缺口
                "draft": self.present_draft(draft),
            }

    def list_errata(self, call_id: str) -> list[dict] | None:
        """某通电话出过的全部勘误（按发稿顺序、段序）。查询本身带着
        call_id，结构上列不出另一通电话的勘误 —— 两通电话的勘误不串。
        未知 call_id 返回 None。"""
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM calls WHERE call_id=?", (call_id,)
            ).fetchone() is None:
                return None
            rows = self._conn.execute(
                "SELECT e.* FROM errata e JOIN drafts d ON d.draft_no = e.draft_no"
                " WHERE e.call_id=? ORDER BY d.draft_seq, e.seq",
                (call_id,),
            ).fetchall()
            return [self._erratum_view(r, self._load_draft(r["draft_no"])) for r in rows]

    def _latest_draft_info(self, call_id: str, view: dict) -> dict | None:
        """会话视图里带的“最近一稿”摘要：稿号、发稿时间，以及活视图相对
        该稿是否已有变化（正文/状态/缺口任一不同）。变了就意味着对外给的
        那稿已经过时、该出新稿了；内容相同的重传不算变化。"""
        r = self._conn.execute(
            "SELECT d.draft_no, d.issued_at, d.status, d.content, d.gaps_json,"
            " w.withdrawn_at, h.release_at AS release_at, h.held_at AS held_at"
            " FROM drafts d"
            " LEFT JOIN withdrawals w ON w.draft_no = d.draft_no"
            " LEFT JOIN holds h ON h.draft_no = d.draft_no"
            " WHERE d.call_id=? ORDER BY d.draft_seq DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        if r is None:
            return None
        changed = (
            r["status"] != view["status"]
            or r["content"] != view["content"]
            or json.loads(r["gaps_json"]) != view["gaps"]
        )
        # 最近一稿此刻是否仍压着：按当前钟点与解禁时刻比对，到点自动翻成
        # 已解（不写任何解禁动作）。latest_draft 只是摘要，本来就不含正文。
        is_held = (
            r["release_at"] is not None
            and self.now_dt() < self._parse_ts(r["release_at"])
        )
        return {
            "draft_no": r["draft_no"],
            "issued_at": r["issued_at"],
            "withdrawn_at": r["withdrawn_at"],
            "is_withdrawn": r["withdrawn_at"] is not None,
            "held_at": r["held_at"],
            "release_at": r["release_at"],
            "is_held": is_held,
            "released": not is_held,
            "visibility": "held" if is_held else "visible",
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
                n_withdrawn = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM withdrawals WHERE call_id=?",
                    (c["call_id"],),
                ).fetchone()["n"]
                # 此刻仍压着的稿数：到点自动解禁、不写动作，故按钟点现算
                held_rows = self._conn.execute(
                    "SELECT release_at FROM holds WHERE call_id=?",
                    (c["call_id"],),
                ).fetchall()
                now = self.now_dt()
                n_held = sum(
                    1 for h in held_rows if now < self._parse_ts(h["release_at"])
                )
                out.append(
                    {
                        "call_id": c["call_id"],
                        "status": R.status_of(seqs, c["last_seq"], n_gap_events > 0),
                        "fragment_count": len(seqs),
                        "open_gaps": open_gaps,
                        "was_incomplete": n_gap_events > 0,
                        "conflicts": c["conflicts"],
                        "drafts_issued": n_drafts,
                        "drafts_withdrawn": n_withdrawn,
                        "drafts_held": n_held,
                        "first_seen_at": c["first_seen_at"],
                        "last_activity_at": c["last_activity_at"],
                        "completed_at": c["completed_at"],
                    }
                )
            return out
