"""拼装纯逻辑的单测。"""

from app.reassembly import (
    ASSEMBLING,
    COMPLETE,
    INCOMPLETE,
    build_parts,
    gap_marker,
    intersect_ranges,
    join_content,
    missing_below,
    missing_ranges,
    status_of,
    subtract_ranges,
    to_ranges,
)


def test_missing_below():
    assert missing_below({1, 2, 4}, 4) == [3]
    assert missing_below({1, 2, 3}, 3) == []
    assert missing_below({2}, 2) == [1]


def test_missing_ranges():
    assert missing_ranges({1, 2, 4}, 4) == [[3, 3]]
    assert missing_ranges({1, 4}, 6) == [[2, 3], [5, 6]]
    assert missing_ranges({1, 2, 3}, 3) == []
    assert missing_ranges(set(), 100_000_000) == []   # 空会话：不算缺口
    assert missing_ranges({2, 99_999_999}, 100_000_000) == [
        [1, 1], [3, 99_999_998], [100_000_000, 100_000_000]]
    # 序号空一亿个也只是一个区间，绝不逐号展开
    assert missing_ranges({1, 100_000_000}, 100_000_000) == [[2, 99_999_999]]


def test_subtract_and_intersect_ranges():
    assert subtract_ranges([[1, 10]], [[3, 4], [7, 8]]) == [[1, 2], [5, 6], [9, 10]]
    assert subtract_ranges([[2, 4]], [[2, 4]]) == []
    assert intersect_ranges([[1, 5]], [[3, 8]]) == [[3, 5]]
    assert intersect_ranges([[1, 2], [6, 7]], [[2, 6]]) == [[2, 2], [6, 6]]


def test_to_ranges():
    assert to_ranges([3, 4, 5, 8, 10, 11]) == [[3, 5], [8, 8], [10, 11]]
    assert to_ranges([]) == []


def test_gap_marker():
    assert gap_marker(3, 3) == "[缺口:片段3]"
    assert gap_marker(3, 5) == "[缺口:片段3-5]"


def test_build_parts_inserts_gap_markers():
    parts = build_parts({1: "甲", 2: "乙", 4: "丁"}, hi=4)
    assert parts[0] == {"seq": 1, "text": "甲"}
    assert parts[2]["gap"] == [3, 3]
    content = join_content(parts)
    assert "甲" in content and "丁" in content
    assert "[缺口:片段3]" in content


def test_build_parts_huge_hole_does_not_explode():
    # 一亿个序号的空洞：parts 只有 文本/缺口/文本 三个单元
    parts = build_parts({1: "开头", 100_000_000: "结尾"}, hi=100_000_000)
    assert len(parts) == 3
    assert parts[1] == {"gap": [2, 99_999_999], "marker": "[缺口:片段2-99999999]"}
    assert join_content(parts).count("缺口") == 1


def test_status_of():
    assert status_of({1, 2}, None) == ASSEMBLING          # 连续但未见结束标记
    assert status_of({1, 2, 4}, None) == INCOMPLETE       # 中间缺 3
    assert status_of({1, 2, 4}, 4) == INCOMPLETE          # 有结束标记也缺 3
    assert status_of({1, 2, 3, 4}, 4) == COMPLETE         # 到齐
    assert status_of({1, 2}, 4) == ASSEMBLING             # 末尾还没到，不算缺口
    # is_last 声明的序号已越过、但正文范围里还夹着缺口：绝不许报 complete
    assert status_of({1, 2, 4}, 1) == INCOMPLETE
    assert status_of({1, 2}, 1) == COMPLETE               # 无缺口且越过结束序号
    assert status_of({1, 2, 3}, 2) == COMPLETE            # 同上，越界但连续
    assert status_of(set(), None) == ASSEMBLING

