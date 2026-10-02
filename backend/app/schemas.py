"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

POLARIZATIONS = ("H", "V", "LHCP", "RHCP")
REUSE_VALUES = ("forbidden", "allowed", "unknown")


class CarrierIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    center_mhz: float = Field(gt=0)
    bandwidth_mhz: float = Field(gt=0)
    power_dbm: float
    polarization: Literal["H", "V", "LHCP", "RHCP"]
    mask_name: str = "strict"

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("载波名不能为空")
        return v


class RulesIn(BaseModel):
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    # key 形如 "H|V"（极化名按字母排序后拼接），value forbidden/allowed/unknown
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)


class SegmentIn(BaseModel):
    """一个可用频段（允许载波完整落入的频率区间）。"""
    low_mhz: float
    high_mhz: float


class ExclusionIn(BaseModel):
    """一个不能跨越的排除窗（保护空洞）。"""
    low_mhz: float
    high_mhz: float
    reason: str = Field(default="", max_length=128)


class AllocationIn(BaseModel):
    """频谱分配方案：多个带版本的可用频段 + 排除窗。

    跨字段的几何校验（段重叠、排除窗悬空、有效净空为空等）在 endpoint 层
    调用 allocation 服务完成，语义错误统一返回 400，且不会发生部分保存。
    """
    name: str = Field(default="默认分配方案", max_length=128)
    # 客户端可不传版本（新建=1）；更新时版本由服务端按内容变化权威递增
    version: Optional[int] = Field(default=None, ge=1)
    segments: list[SegmentIn] = Field(min_length=1)
    exclusions: list[ExclusionIn] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)
    # 可选：频谱分配方案（多段可用 + 排除窗），用于图上显示边界/排除区
    # 及标注每个录入载波是否落在有效净空内
    allocation: Optional[AllocationIn] = None


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 旧版单一连续频段（allocation 缺省时使用，保持向后兼容）
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    # 新版：多段可用频段 + 排除窗的版本化分配方案
    allocation: Optional[AllocationIn] = None
    mode: Literal["guard_only", "mask_aware"] = "guard_only"

    def validate_band(self) -> None:
        if self.allocation is None and self.band_high_mhz <= self.band_low_mhz:
            raise ValueError("band_high_mhz 必须大于 band_low_mhz")


class CarrierOut(BaseModel):
    id: Optional[int] = None
    name: str
    center_mhz: float
    bandwidth_mhz: float
    power_dbm: float
    polarization: str
    mask_name: str


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    # 旧版单频段字段保留：allocation 缺省时等价迁移为单段方案
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    allocation: Optional[AllocationIn] = None
    carriers: list[CarrierIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
    created_at: Optional[str] = None
    allocation_version: Optional[int] = None
    has_plans: bool = False


class SegmentOut(BaseModel):
    low_mhz: float
    high_mhz: float
    width_mhz: Optional[float] = None


class ExclusionOut(BaseModel):
    low_mhz: float
    high_mhz: float
    width_mhz: Optional[float] = None
    reason: str = ""


class AllocationOut(BaseModel):
    name: str
    version: int
    fingerprint: str
    segments: list[SegmentOut]
    exclusions: list[ExclusionOut]
    # 段被排除窗切出的有效净空小片（载波必须完整落入其中之一）
    pieces: list[dict] = Field(default_factory=list)


class ScenarioOut(BaseModel):
    id: int
    name: str
    description: str
    # 旧版字段保留并始终填充（= 分配方案的总体外边界），兼容旧客户端
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str]
    allocation: AllocationOut
    # True 表示该场景是旧格式（无 allocation 列数据），读取时即时迁移为等价单段
    allocation_migrated: bool = False
    carriers: list[CarrierOut]


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str
