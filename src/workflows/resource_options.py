"""首通与指定关卡作战共用的场次、AP 和果实限制。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_quests: int | None = Field(default=3, ge=1, le=30)
    ap_budget: int | None = Field(default=100, ge=1, le=999)
    refill_apple: Literal['none', 'bronze', 'blue', 'silver', 'gold'] = 'none'
    max_apples: int | None = Field(default=0, ge=0, le=999)

    @model_validator(mode='after')
    def bounded_apple_usage(self):
        if self.refill_apple == 'none' and self.max_apples != 0:
            raise ValueError('禁用果实恢复时使用上限须为0')
        if self.refill_apple != 'none' and self.max_apples == 0:
            raise ValueError('启用果实恢复须设置1至999个上限，或null表示不限')
        return self
