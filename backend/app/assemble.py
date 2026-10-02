"""schema <-> 领域模型转换，以及绘图数据组装。"""
from __future__ import annotations

import numpy as np

from .schemas import AllocationIn, AnalyzeRequest, CarrierIn, RulesIn
from .services.allocation import Allocation, Exclusion, Segment
from .services.analysis import AnalysisRules, Carrier
from .services.masks import MASKS, get_mask, psd_on_grid
from .services.units import dbm_to_watt


def to_domain(c: CarrierIn, cid: int | None = None) -> Carrier:
    return Carrier(
        id=cid, name=c.name, center_mhz=c.center_mhz,
        bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
        polarization=c.polarization, mask_name=c.mask_name,
    )


def to_rules(r: RulesIn) -> AnalysisRules:
    return AnalysisRules(
        guard_required_mhz=r.guard_required_mhz,
        leakage_limit_dbm=r.leakage_limit_dbm,
        reuse_policy=dict(r.reuse_policy),
    )


def to_allocation(a: AllocationIn) -> Allocation:
    return Allocation(
        segments=tuple(Segment(s.low_mhz, s.high_mhz, s.label) for s in a.segments),
        exclusions=tuple(Exclusion(e.low_mhz, e.high_mhz, e.reason)
                         for e in a.exclusions),
    )


def validate_masks(carriers: list[CarrierIn]) -> None:
    for c in carriers:
        if c.mask_name not in MASKS:
            raise ValueError(f"载波 {c.name!r} 引用了未知掩模 {c.mask_name!r}")


def build_spectrum(carriers: list[Carrier], grid_step_mhz: float) -> dict:
    """在统一频率网格上组装各载波 PSD 曲线与聚合谱（线性域功率叠加）。"""
    if not carriers:
        return {"f_mhz": [], "curves": [], "aggregate_dbm_hz": []}

    span = max(get_mask(c.mask_name).span_mhz for c in carriers)
    f_lo = min(c.center_mhz - span for c in carriers)
    f_hi = max(c.center_mhz + span for c in carriers)
    f = np.arange(f_lo, f_hi + grid_step_mhz / 2, grid_step_mhz)

    curves = []
    total_w_hz = np.zeros_like(f)
    for c in carriers:
        psd = psd_on_grid(get_mask(c.mask_name), f, c.center_mhz,
                          c.bandwidth_mhz, c.power_dbm)
        w_hz = dbm_to_watt(psd)  # -inf -> 0 W
        total_w_hz += w_hz
        # 绘图用 null 表示无信号，避免 Plotly 把 -inf 画成贴底线
        curves.append({
            "name": c.name,
            "mask_name": c.mask_name,
            "polarization": c.polarization,
            "psd_dbm_hz": [None if not np.isfinite(v) else round(float(v), 2)
                           for v in psd],
        })
    with np.errstate(divide="ignore"):
        agg_dbm = 10.0 * np.log10(np.where(total_w_hz > 0, total_w_hz / 1e-3, np.nan))
    aggregate = [None if not np.isfinite(v) else round(float(v), 2) for v in agg_dbm]
    return {
        "f_mhz": [round(float(v), 4) for v in f],
        "curves": curves,
        "aggregate_dbm_hz": aggregate,
    }


def bands_view(carriers: list[Carrier]) -> list[dict]:
    return [{
        "name": c.name,
        "center_mhz": c.center_mhz,
        "low_mhz": round(c.low, 4),
        "high_mhz": round(c.high, 4),
        "bandwidth_mhz": c.bandwidth_mhz,
        "power_dbm": c.power_dbm,
        "polarization": c.polarization,
        "mask_name": c.mask_name,
    } for c in carriers]
