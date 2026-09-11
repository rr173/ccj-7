"""片段格式约定（上游链路必须遵守）：

- call_id : 唯一标识一次通话。不同通话的 call_id 必须不同 —— 服务严格按
            call_id 归组，从根上保证两次通话的片段不会被织进同一条会话。
- seq     : 从 1 开始的整数，同一次通话内按顺序分配，一个片段一个号。
- text    : 片段承载的通话内容（一句或一段）。
- is_last : 该通话最后一个片段置 true，其 seq 即片段总数。

重传片段 = (call_id, seq) 相同。内容相同的重传幂等去重；内容不同的视为冲突，
保留先到者并计入 conflicts。
"""

from pydantic import BaseModel, Field


class FragmentIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=1)
    text: str = Field(default="", max_length=65536)
    is_last: bool = False


class ClaimIn(BaseModel):
    """认领一稿：认领人标识。稿一旦被人认领，别人不能再认走，
    直到当前认领人把它交出去（release）。"""
    claimed_by: str = Field(min_length=1, max_length=256)


class ErrataIn(BaseModel):
    """对一稿的某一段出勘误：对着哪一段（该稿快照里的片段序号）、改成什么。
    勘误只新增记录，那一稿当时的正文和缺口一个字不动。"""
    seq: int = Field(ge=1)
    new_text: str = Field(max_length=65536)
