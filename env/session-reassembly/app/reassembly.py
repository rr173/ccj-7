"""纯函数：乱序片段的重排、缺口检测与内容拼装。不碰存储，便于单测。

缺口一律用**闭区间** [lo, hi] 表示和传递，绝不按序号逐个展开 —— 序号空
一大截（比如只收到 1 和 1 亿）时，展开成一亿个元素会直接吃光内存、把查询
卡死；区间表示下缺口再多也只是几个 [lo, hi]。
"""

from __future__ import annotations

# 会话状态
ASSEMBLING = "assembling"    # 目前连续、但还没见到结束标记（或结尾片段还在路上）
INCOMPLETE = "incomplete"    # 中间有缺口（已有更靠后的片段到达，中间却缺号）
COMPLETE = "complete"        # 已见结束标记，且到过的序号连成一片、越过了结束序号


def missing_ranges(seqs: set[int], hi: int) -> list[list[int]]:
    """1..hi 中尚未收到的序号，直接以闭区间返回，不逐号展开。

    如 seqs={1,4}, hi=6 -> [[2,3],[5,6]]；hi<1 或无缺口 -> []。
    """
    if hi < 1 or not seqs:
        return []
    ranges: list[list[int]] = []
    expected = 1
    for n in sorted(seqs):
        if n < 1:
            continue
        if n > hi:
            break
        if n > expected:
            ranges.append([expected, n - 1])
        expected = n + 1
    if expected <= hi:
        ranges.append([expected, hi])
    return ranges


def missing_below(seqs: set[int], hi: int) -> list[int]:
    """1..hi 中尚未收到的序号（升序）。仅供小范围/测试使用，缺口可能巨大时
    必须用 missing_ranges —— 本函数会逐号展开。"""
    return [n for n in range(1, hi + 1) if n not in seqs]


def to_ranges(nums: list[int]) -> list[list[int]]:
    """把升序整数序列合并成闭区间列表，如 [3,4,5,8] -> [[3,5],[8,8]]。"""
    ranges: list[list[int]] = []
    for n in nums:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ranges


def merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """排序并合并相交/相邻的闭区间。"""
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            if hi > out[-1][1]:
                out[-1][1] = hi
        else:
            out.append([lo, hi])
    return out


def subtract_ranges(a: list[list[int]], b: list[list[int]]) -> list[list[int]]:
    """区间集合差 a - b（两边均为已合并闭区间），结果仍是合并区间。"""
    out: list[list[int]] = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi < cur:
                continue
            if blo > hi:
                break
            if blo > cur:
                out.append([cur, blo - 1])
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append([cur, hi])
    return merge_ranges(out)


def intersect_ranges(a: list[list[int]], b: list[list[int]]) -> list[list[int]]:
    """区间集合交（两边均为已合并闭区间）。"""
    out: list[list[int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def gap_marker(a: int, b: int) -> str:
    """缺口的对外可见标记，直接嵌在拼装出的内容里。"""
    return f"[缺口:片段{a}]" if a == b else f"[缺口:片段{a}-{b}]"


def build_parts(seq_to_text: dict[int, str], hi: int) -> list[dict]:
    """按 1..hi 顺序生成拼装单元：收到的放文本，缺失的整段放一个缺口标记。

    遍历的是**已到片段**而不是 1..hi 每个序号，所以 hi 即便到十亿、中间整段
    缺失，parts 也只有“文本 + 一个缺口区间 + 文本”几个单元。
    """
    parts: list[dict] = []
    cursor = 1
    for seq in sorted(seq_to_text):
        if seq > hi:
            break
        if cursor < seq:
            parts.append({"gap": [cursor, seq - 1], "marker": gap_marker(cursor, seq - 1)})
        parts.append({"seq": seq, "text": seq_to_text[seq]})
        cursor = seq + 1
    if cursor <= hi:
        parts.append({"gap": [cursor, hi], "marker": gap_marker(cursor, hi)})
    return parts


def join_content(parts: list[dict]) -> str:
    """把拼装单元合成会话文本，一行一个片段，缺口处放显式标记。"""
    return "\n".join(p["text"] if "text" in p else p["marker"] for p in parts)


def status_of(seqs: set[int], last_seq: int | None) -> str:
    """判定会话状态。宁可报“还在拼/有缺口”，绝不假装完整。

    缺口判定以**实际到过的最大序号**为上界：只要更大的号已经到了、中间却空着，
    那就是确凿的缺口，与 is_last 声明在第几号无关 —— 片段越过 is_last 序号
    到达也一样，正文里夹着缺口就绝不能报 complete。
    结尾片段还在路上（已有序号连续、但还没到 last_seq）不算缺口，仍是 assembling。
    """
    if not seqs:
        return ASSEMBLING
    top = max(seqs)
    if missing_ranges(seqs, top):
        return INCOMPLETE
    if last_seq is not None and top >= last_seq:
        return COMPLETE
    return ASSEMBLING
