"""纯函数：乱序片段的重排、缺口检测与内容拼装。不碰存储，便于单测。"""

from __future__ import annotations

# 会话状态
ASSEMBLING = "assembling"    # 目前连续、但还没见到结束标记，仍在拼接中
INCOMPLETE = "incomplete"    # 中间有缺口（已有更靠后的片段到达，中间却缺号）
COMPLETE = "complete"        # 已见结束标记且 1..last_seq 全部到齐


def missing_below(seqs: set[int], hi: int) -> list[int]:
    """1..hi 中尚未收到的序号（升序）。"""
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


def gap_marker(a: int, b: int) -> str:
    """缺口的对外可见标记，直接嵌在拼装出的内容里。"""
    return f"[缺口:片段{a}]" if a == b else f"[缺口:片段{a}-{b}]"


def build_parts(seq_to_text: dict[int, str], hi: int) -> list[dict]:
    """按 1..hi 顺序生成拼装单元：收到的放文本，缺失的放缺口标记。"""
    parts: list[dict] = []
    n = 1
    while n <= hi:
        if n in seq_to_text:
            parts.append({"seq": n, "text": seq_to_text[n]})
            n += 1
        else:
            a = n
            while n <= hi and n not in seq_to_text:
                n += 1
            parts.append({"gap": [a, n - 1], "marker": gap_marker(a, n - 1)})
    return parts


def join_content(parts: list[dict]) -> str:
    """把拼装单元合成会话文本，一行一个片段，缺口处放显式标记。"""
    return "\n".join(p["text"] if "text" in p else p["marker"] for p in parts)


def status_of(seqs: set[int], last_seq: int | None) -> str:
    """判定会话状态。宁可报“还在拼/有缺口”，绝不假装完整。"""
    if last_seq is not None and all(n in seqs for n in range(1, last_seq + 1)):
        return COMPLETE
    if seqs and missing_below(seqs, max(seqs)):
        return INCOMPLETE
    return ASSEMBLING
