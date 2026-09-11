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


# ------------------------------------------------------------ 两稿对照

def _diff_units(parts: list[dict]) -> list[tuple[int, int, str, dict]]:
    """把拼装单元归一成 (lo, hi, kind, 原单元)：文本段是点 [seq,seq]，
    缺口段是区间 [lo,hi]。单元顺序拼接即严丝合缝覆盖 1..top。"""
    units = []
    for p in parts:
        if "text" in p:
            units.append((p["seq"], p["seq"], "text", p))
        else:
            lo, hi = p["gap"]
            units.append((lo, hi, "gap", p))
    return units


def _diff_gap_side(lo: int, hi: int) -> dict:
    return {"kind": "gap", "gap": [lo, hi], "marker": gap_marker(lo, hi)}


def _diff_text_side(p: dict) -> dict:
    return {"kind": "text", "seq": p["seq"], "text": p["text"]}


def _diff_tail_record(lo: int, hi: int, unit: tuple | None, side: str) -> dict:
    """只有一边覆盖到的段（另一边那稿短、还没到这段）。side 指明 unit 是
    a 还是 b；另一边记 absent。点段单元始终是点 [n,n]，缺口单元可能是区间。"""
    present = (
        _diff_gap_side(lo, hi) if unit[2] == "gap" else _diff_text_side(unit[3])
    ) if unit is not None else {"kind": "absent"}
    rec = {"seq": lo} if lo == hi else {"range": [lo, hi]}
    rec["a"] = present if side == "a" else {"kind": "absent"}
    rec["b"] = present if side == "b" else {"kind": "absent"}
    return rec


def diff_draft_parts(parts_a: list[dict], parts_b: list[dict]) -> list[dict]:
    """对照两份**稿快照**的拼装单元，只把对不上的段记下来（按段序升序）。

    两边一致的段不记：两边都是同一文本的点段一致；两边都缺（无论缺口区间
    各自多大，重叠部分）也一致 —— 两稿当时都没有这段，没什么可对的。
    对不上的段分两种记法：

    - 点段（lo==hi）：``{"seq": n, "a": 边, "b": 边}``，边为
      ``{"kind":"text","seq","text"}``（这一稿当时有这段）、
      ``{"kind":"gap","gap":[n,n],"marker"}``（这一稿当时这里缺）或
      ``{"kind":"absent"}``（这一稿当时只到这之前，根本没到这段）；
    - 区间段（lo<hi）：``{"range":[lo,hi], ...}`` —— 只可能是一边整段缺口、
      另一边那稿还没到这里（absent），点段文本不会产生区间。

    全程沿区间边界推进、绝不按序号逐个展开：空一大截也只出一条区间记录。
    """
    ua = _diff_units(parts_a)
    ub = _diff_units(parts_b)
    na, nb = len(ua), len(ub)
    i = j = 0
    la = ua[0][0] if na else 0   # 当前单元的有效起点（区间可能被对边切成小片）
    lb = ub[0][0] if nb else 0
    out: list[dict] = []

    while i < na or j < nb:
        if i >= na:
            out.append(_diff_tail_record(lb, ub[j][1], ub[j], "b"))
            j += 1
            lb = ub[j][0] if j < nb else 0
            continue
        if j >= nb:
            out.append(_diff_tail_record(la, ua[i][1], ua[i], "a"))
            i += 1
            la = ua[i][0] if i < na else 0
            continue

        A, B = ua[i], ub[j]
        lo = la                      # 两表都从 1 严丝合缝拼起，游标始终对齐
        hi = min(A[1], B[1])
        ka, kb = A[2], B[2]
        if ka == "gap" and kb == "gap":
            pass                     # 两边这段都缺：一致，不记
        elif lo == hi and ka == "text" and kb == "text" \
                and A[3]["text"] == B[3]["text"]:
            pass                     # 同一段、两边正文一字不差：一致，不记
        else:
            # 文本点只会切出点段（hi==lo）；点段缺口按这一个号给标记
            sa = _diff_text_side(A[3]) if ka == "text" else _diff_gap_side(lo, hi)
            sb = _diff_text_side(B[3]) if kb == "text" else _diff_gap_side(lo, hi)
            out.append({"seq": lo, "a": sa, "b": sb})

        if A[1] == hi:
            i += 1
            la = ua[i][0] if i < na else 0
        else:
            la = hi + 1             # A 的缺口区间被对边切下一小片，剩余部分继续
        if B[1] == hi:
            j += 1
            lb = ub[j][0] if j < nb else 0
        else:
            lb = hi + 1
    return out


def status_of(seqs: set[int], last_seq: int | None, had_gap: bool = False) -> str:
    """判定会话状态。宁可报“还在拼/有缺口”，绝不假装完整。

    缺口判定以**实际到过的最大序号**为上界：只要更大的号已经到了、中间却空着，
    那就是确凿的缺口，与 is_last 声明在第几号无关 —— 片段越过 is_last 序号
    到达也一样，正文里夹着缺口就绝不能报 complete。

    没有缺口时是否完整，两种凭据满足其一即可：
    - 见过 is_last，且实际到达已越过结束序号（正常收尾）；
    - **曾经缺过**（对外给过 incomplete）：既然更靠后的片段早就到过、现在缺的
      那段也补齐了，结尾不可能还藏在后面 —— 缺段补齐即完整，即使结束标记
      一直没到也不许停在“拼接中”。
    两者都不满足（连续、没缺过、没见过结束标记）才是 assembling：结尾片段还
    可能在路上。
    """
    if not seqs:
        return ASSEMBLING
    top = max(seqs)
    if missing_ranges(seqs, top):
        return INCOMPLETE
    # 已声明总数、但实际到达还没越过结束序号：结尾片段还在路上，继续等
    if last_seq is not None and top < last_seq:
        return ASSEMBLING
    # 到过的序号已连成一片：正常收尾（越过结束序号），或曾经缺过、缺段已补齐
    # （此时更靠后的片段早就到过，结尾不可能还藏在后面）→ 完整
    if last_seq is not None or had_gap:
        return COMPLETE
    return ASSEMBLING
