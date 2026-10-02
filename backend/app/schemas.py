"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

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


# ---- 频谱分配方案 ----

class SegmentIn(BaseModel):
    low_mhz: float
    high_mhz: float
    label: str = ""

    @model_validator(mode="after")
    def _ordered(self):
        if not self.high_mhz > self.low_mhz:
            raise ValueError(f"可用段 [{self.low_mhz}, {self.high_mhz}] 上界必须大于下界")
        return self


class ExclusionIn(BaseModel):
    low_mhz: float
    high_mhz: float
    reason: str = ""

    @model_validator(mode="after")
    def _ordered(self):
        if not self.high_mhz > self.low_mhz:
            raise ValueError(f"排除窗 [{self.low_mhz}, {self.high_mhz}] 上界必须大于下界")
        return self


class AllocationIn(BaseModel):
    """一次提交的完整分配方案几何（保存时整体校验，不部分生效）。"""
    segments: list[SegmentIn] = Field(min_length=1)
    exclusions: list[ExclusionIn] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def _no_overlap(self):
        ordered = sorted(self.segments, key=lambda s: (s.low_mhz, s.high_mhz))
        for a, b in zip(ordered, ordered[1:]):
            if b.low_mhz < a.high_mhz - 1e-9:
                raise ValueError(
                    f"可用段 [{a.low_mhz}, {a.high_mhz}] 与 "
                    f"[{b.low_mhz}, {b.high_mhz}] 重叠")
        return self


class SegmentOut(BaseModel):
    low_mhz: float
    high_mhz: float
    label: str = ""


class ExclusionOut(BaseModel):
    low_mhz: float
    high_mhz: float
    reason: str = ""


class AllocationOut(BaseModel):
    version: int
    note: str = ""
    created_at: Optional[str] = None
    segments: list[SegmentOut]
    exclusions: list[ExclusionOut]


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)
    # 可选：给出分配方案时额外检查载波是否完整落入可用空闲窗
    allocation: Optional[AllocationIn] = None


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    mode: Literal["guard_only", "mask_aware"] = "guard_only"
    # 可选：多段可用频段 + 排除窗；缺省时退化为 [band_low, band_high] 单段
    allocation: Optional[AllocationIn] = None

    def validate_band(self) -> None:
        if self.band_high_mhz <= self.band_low_mhz:
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
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    carriers: list[CarrierIn] = Field(default_factory=list)
    # 可选：缺省时由 band_low/high 迁移为等价单段方案（版本 1）
    allocation: Optional[AllocationIn] = None


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
    created_at: Optional[str] = None


class ScenarioOut(BaseModel):
    id: int
    name: str
    description: str
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str]
    carriers: list[CarrierOut]
    allocation: Optional[AllocationOut] = None


class PlanSaveIn(BaseModel):
    """把一次（无状态求解得到的）规划结果冻结为场景规划记录。"""
    mode: Literal["guard_only", "mask_aware"] = "guard_only"
    assignments: list[dict] = Field(min_length=1)


class PlanRecordOut(BaseModel):
    id: int
    scheme_version: int
    current_version: int
    mode: str
    feasible: bool
    message: str
    created_at: Optional[str] = None
    stale: bool
    stale_reasons: list[str]
    assignments: list[dict]
    post_check: dict


class ScenarioExport(BaseModel):
    """场景导出格式（含全部分配方案版本与规划记录，导入时可完整追溯）。"""
    format: str = "spectrum-workbench/scenario"
    format_version: int = 1
    name: str
    description: str = ""
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str] = Field(default_factory=dict)
    carriers: list[CarrierOut] = Field(default_factory=list)
    allocations: list[AllocationOut] = Field(default_factory=list)
    plans: list[dict] = Field(default_factory=list)


class ScenarioImport(BaseModel):
    """导入请求：整个文件先校验通过才落库（冲突/不完整区间不得部分保存）。"""
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    carriers: list[CarrierIn] = Field(default_factory=list)
    # 新格式：全部版本；旧格式可只带单个 allocation 或完全没有（按 band 迁移）
    allocations: list[AllocationIn] = Field(default_factory=list)
    allocation: Optional[AllocationIn] = None
    plans: list[dict] = Field(default_factory=list)


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str
