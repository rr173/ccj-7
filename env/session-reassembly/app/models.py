"""片段格式约定（上游链路必须遵守）：

- call_id : 唯一标识一次通话。不同通话的 call_id 必须不同 —— 服务严格按
            call_id 归组，从根上保证两次通话的片段不会被织进同一条会话。
- seq     : 从 1 开始的整数，同一次通话内按顺序分配，一个片段一个号。
- text    : 片段承载的通话内容（一句或一段）。
- is_last : 该通话最后一个片段置 true，其 seq 即片段总数。

重传片段 = (call_id, seq) 相同。内容相同的重传幂等去重；内容不同的视为冲突，
保留先到者并计入 conflicts。
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class FragmentIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=1)
    text: str = Field(default="", max_length=65536)
    is_last: bool = False


class BridgeIn(BaseModel):
    """拿两通**不同的**电话搭一座桥，按序号一对一对齐。

    - 必须是两通都已收到片段（非空）的通话；
    - 同一通电话不能同时待在两座桥里（拆了之后可以再搭）。
    """

    model_config = ConfigDict(populate_by_name=True)

    left_call_id: str = Field(
        min_length=1, max_length=256,
        validation_alias=AliasChoices("left_call_id", "left", "call_id_a", "a"),
    )
    right_call_id: str = Field(
        min_length=1, max_length=256,
        validation_alias=AliasChoices("right_call_id", "right", "call_id_b", "b"),
    )


class BridgeSwapIn(BaseModel):
    """桥还搭着的时候，把其中一边换成另一通**已经有片段**的电话。

    - side      ：换哪一边（left/right）；
    - call_id   ：换上来的是哪通电话 —— 必须非空、不能已经待在别的活动桥里，
                  也不能就是这座桥上当前两边中的任何一通；
    - 已拆掉的桥不能再换边。桥号不变，换完按新的两边重新对齐；
      换边当时旧的两边是谁、对到哪，单独留痕，绝不被新的两边盖掉。
    """

    model_config = ConfigDict(populate_by_name=True)

    side: Literal["left", "right"] = Field(
        validation_alias=AliasChoices("side", "replace_side", "which")
    )
    call_id: str = Field(
        min_length=1, max_length=256,
        validation_alias=AliasChoices(
            "call_id", "new_call_id", "incoming_call_id", "with"
        ),
    )


class ClaimIn(BaseModel):
    """认领一稿：认领人标识。稿一旦被人认领，别人不能再认走，
    直到当前认领人把它交出去（release）。"""
    claimed_by: str = Field(min_length=1, max_length=256)


class ErrataIn(BaseModel):
    """对一稿的某一段出勘误：对着哪一段（该稿快照里的片段序号）、改成什么。
    勘误只新增记录，那一稿当时的正文和缺口一个字不动。"""
    seq: int = Field(ge=1)
    new_text: str = Field(max_length=65536)


class HoldIn(BaseModel):
    """把一稿压到指定时刻才解：写明几点几分能见。

    解禁时刻不能早于这稿发出的时刻；写上去就不能改、也不能提前解开；
    同一通电话里后发出的稿，解禁时刻不能早于先发的稿。没到点之前只能
    知道这稿还压着、何时解，正文和缺口一律看不见。
    """

    model_config = ConfigDict(populate_by_name=True)

    release_at: datetime = Field(
        ...,
        validation_alias=AliasChoices(
            "release_at", "unlock_at", "embargo_until", "release_time"
        ),
    )

    @field_validator("release_at", mode="before")
    @classmethod
    def _naive_as_utc(cls, v):
        # 不带时区的时刻一律按 UTC 解释，绝不当地方式时间蒙混
        if isinstance(v, str):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        return v
