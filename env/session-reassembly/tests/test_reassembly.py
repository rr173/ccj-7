"""拼装纯逻辑的单测。"""

from app.reassembly import (
    ASSEMBLING,
    COMPLETE,
    INCOMPLETE,
    build_parts,
    gap_marker,
    join_content,
    missing_below,
    status_of,
    to_ranges,
)


def test_missing_below():
    assert missing_below({1, 2, 4}, 4) == [3]
    assert missing_below({1, 2, 3}, 3) == []
    assert missing_below({2}, 2) == [1]


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


def test_status_of():
    assert status_of({1, 2}, None) == ASSEMBLING          # 连续但未见结束标记
    assert status_of({1, 2, 4}, None) == INCOMPLETE       # 中间缺 3
    assert status_of({1, 2, 4}, 4) == INCOMPLETE          # 有结束标记也缺 3
    assert status_of({1, 2, 3, 4}, 4) == COMPLETE         # 到齐
    assert status_of({1, 2}, 4) == ASSEMBLING             # 末尾还没到，不算缺口
