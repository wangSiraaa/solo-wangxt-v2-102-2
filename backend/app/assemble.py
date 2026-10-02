"""schema <-> 领域模型转换，以及绘图数据组装。"""
from __future__ import annotations

import numpy as np

from .schemas import AllocationIn, AnalyzeRequest, CarrierIn, RulesIn
from .services import allocation as alloc_mod
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


def to_allocation(a: AllocationIn | None, fallback_low: float | None = None,
                  fallback_high: float | None = None) -> alloc_mod.Allocation | None:
    """把请求中的 AllocationIn 转成领域对象。

    ``a`` 为 None 且给定 fallback 时，从旧版单频段迁移为等价单段方案。
    所有几何语义错误抛 ValueError（endpoint 统一转 400，不发生部分保存）。
    """
    if a is None:
        if fallback_low is None:
            return None
        return alloc_mod.allocation_from_band(fallback_low, fallback_high)  # type: ignore[arg-type]
    alloc = alloc_mod.allocation_from_payload(a.model_dump())
    alloc_mod.validate_allocation(alloc)
    return alloc


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


def _allocation_annotation(c: Carrier, alloc: alloc_mod.Allocation | None) -> dict:
    """标注一条载波占用相对分配方案的归属（图与 post-check 共用）。"""
    if alloc is None:
        return {}
    contained = alloc_mod.containing_piece(alloc, c.low, c.high)
    crossed = alloc_mod.crossed_exclusions(alloc, c.low, c.high)
    inside_any_segment = any(
        c.low >= s.low_mhz - alloc_mod.EPS_MHZ and c.high <= s.high_mhz + alloc_mod.EPS_MHZ
        for s in alloc.segments)
    if contained is not None:
        idx, piece = contained
        anchor = alloc_mod.effective_pieces_with_anchor(alloc)[idx]
        seg_idx = next((k for k, s in enumerate(alloc.segments)
                        if abs(s.low_mhz - anchor[2]) < alloc_mod.EPS_MHZ), -1)
        status = "inside"
    elif crossed:
        seg_idx = -1
        status = "crosses_exclusion"
    elif not inside_any_segment:
        seg_idx = -1
        status = "outside_segments"
    else:
        seg_idx = -1
        status = "spans_gap"
    return {
        "allocation_status": status,
        "segment_index": seg_idx,
        "piece_index": contained[0] if contained is not None else None,
        "piece_mhz": [round(contained[1].low_mhz, 4), round(contained[1].high_mhz, 4)]
                      if contained is not None else None,
        "crossed_exclusions": [
            {"low_mhz": e.low_mhz, "high_mhz": e.high_mhz, "reason": e.reason}
            for e in crossed],
    }


def bands_view(carriers: list[Carrier],
               allocation: alloc_mod.Allocation | None = None) -> list[dict]:
    out = []
    for c in carriers:
        item = {
            "name": c.name,
            "center_mhz": c.center_mhz,
            "low_mhz": round(c.low, 4),
            "high_mhz": round(c.high, 4),
            "bandwidth_mhz": c.bandwidth_mhz,
            "power_dbm": c.power_dbm,
            "polarization": c.polarization,
            "mask_name": c.mask_name,
        }
        if allocation is not None:
            item.update(_allocation_annotation(c, allocation))
        out.append(item)
    return out
