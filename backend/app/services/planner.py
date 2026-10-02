"""OR-Tools 频率位置规划（离线简化模型）。

为给定的一组载波寻找一组满足最小间隔约束的中心频率位置：

- 频率离散到 1 kHz 网格，用 CP-SAT 求解整数模型。
- 频谱分配方案：可用频段可为多个不连续区间，并可含排除窗；每个载波的
  占用带宽必须完整落入某一个“空闲窗”（可用段减去排除窗），求解器为每个
  载波选择一个可行窗（布尔变量 + only_enforce_if 通道约束）。
- 同极化、或复用规则为 forbidden/unknown 的极化对：两个频带不得相交，
  且边缘净距不小于要求值；用布尔析取表示“i 在 j 左”或“j 在 i 左”。
- 允许复用的极化对不加间隔约束（可同址），分段逻辑不改变该规则。
- guard_only 模式：统一用保护间隔；mask_aware 模式：每对载波按**双向**
  掩模泄漏都不越限反算所需净距（含 0.5 dB 规划裕量，保证返回方案在分析
  口径下必然达标）。

目标：最小化各载波相对其偏好位置（录入中心频率，截断到最近的可容纳空闲窗）
的总偏移量。无解时返回 INFEASIBLE 与逐载波诊断（哪条载波因段宽/排除窗
放不下）。返回中为每条载波给出落在该段的文字说明（segment_reason）。

只输出频率方案，不连接任何设备，也不产生发射指令。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .allocation import (Allocation, FreeWindow, Segment, allocation_view,
                         compute_free_windows, diagnose_carrier_fit,
                         feasible_windows_for)
from .analysis import AnalysisRules, Carrier
from .masks import get_mask
from .units import dbm_to_watt, watt_to_dbm

GRID_KHZ = 1  # 频率规划网格：1 kHz
# 泄漏评估网格 (MHz)：必须与分析模块 leakage_power_dbm 的默认网格一致，
# 否则规划求得的“达标净距”在分析口径下可能因数值错位而差之毫厘。
EVAL_GRID_MHZ = 0.01
# 规划裕量 (dB)：泄漏限值在规划口径上收紧 0.5 dB，
# 避免网格边界点的数值取舍让“恰好达标”的方案在分析口径下越限。
PLAN_MARGIN_DB = 0.5


@dataclass
class BandLimits:
    low_mhz: float
    high_mhz: float


def allocation_from_band(band: BandLimits) -> Allocation:
    """旧的单一连续可用范围 -> 等价的单段分配方案（迁移口径）。"""
    return Allocation(segments=(Segment(band.low_mhz, band.high_mhz, "S1"),),
                      exclusions=())


def _leakage_at_separation(tx: Carrier, victim_bw_mhz: float,
                           separation_mhz: float, grid_step_mhz: float = 0.01) -> float:
    """tx 中心与一个位于其右侧、带宽 victim_bw 的虚拟受害载波中心相距 separation 时，
    落入受害频带的泄漏功率 dBm。"""
    mask = get_mask(tx.mask_name)
    from .masks import spectrum_curve
    f, psd = spectrum_curve(mask, tx.center_mhz, tx.bandwidth_mhz,
                            tx.power_dbm, grid_step_mhz)
    v_low = tx.center_mhz + separation_mhz - victim_bw_mhz / 2.0
    v_high = tx.center_mhz + separation_mhz + victim_bw_mhz / 2.0
    inside = (f >= v_low - 1e-12) & (f <= v_high + 1e-12)
    if not np.any(inside):
        return float("-inf")
    p_w = float(np.trapezoid(dbm_to_watt(psd[inside]), dx=grid_step_mhz * 1e6))
    return watt_to_dbm(p_w)


def _required_gap_one_direction(tx: Carrier, victim: Carrier,
                                rules: AnalysisRules, grid_step_mhz: float = EVAL_GRID_MHZ) -> float:
    """扫描最小边缘净距 g（网格对齐），使 tx 落入 victim 频带的泄漏 <= 限值。

    规划口径将限值收紧 PLAN_MARGIN_DB，保证规划结果在分析口径下留有裕量。
    净距 g 时中心间距 = g + (BW_tx + BW_victim)/2。
    g 达到 mask 跨度 - victim 半宽后两频带不再相交，泄漏为 -inf，必然达标。
    """
    limit = rules.leakage_limit_dbm - PLAN_MARGIN_DB
    max_gap = get_mask(tx.mask_name).span_mhz - victim.bandwidth_mhz / 2.0
    n_steps = int(max_gap / grid_step_mhz) + 1
    for k in range(n_steps + 1):
        g = k * grid_step_mhz
        sep = g + (tx.bandwidth_mhz + victim.bandwidth_mhz) / 2.0
        if _leakage_at_separation(tx, victim.bandwidth_mhz, sep, grid_step_mhz) <= limit:
            return g
    return max_gap


def _safe_edge_gap(a: Carrier, b: Carrier, rules: AnalysisRules) -> float:
    """掩模感知所需的最小边缘净距 (MHz)。

    泄漏与“谁在左”无关：无论排序如何，a->b 与 b->a 两个方向的泄漏都必须达标，
    因此取两个方向所需净距的最大值，同时不小于保护间隔规则。
    结果向上对齐到 10 kHz（EVAL_GRID_MHZ 的整数倍，1 kHz 规划网格可精确实现）。
    """
    g = max(_required_gap_one_direction(a, b, rules),
            _required_gap_one_direction(b, a, rules),
            rules.guard_required_mhz)
    return float(np.ceil(g / EVAL_GRID_MHZ + 1e-9) * EVAL_GRID_MHZ)


def _clip_to_windows(pref: float, centers: list[tuple[float, float]]) -> float:
    """把偏好中心截断到最近的可行中心区间（空闲窗扣除半宽后）。"""
    def dist(lo_hi):
        lo, hi = lo_hi
        if lo <= pref <= hi:
            return 0.0
        return min(abs(pref - lo), abs(pref - hi))
    lo, hi = min(centers, key=dist)
    return min(max(pref, lo), hi)


def _segment_reason(c: Carrier, window: FreeWindow, n_feasible: int,
                    allocation: Allocation) -> str:
    """解释这条载波为什么落在该空闲窗（教学口径，可复核）。"""
    seg = allocation.segments[window.segment_index]
    where = (f"段 {window.segment_label or window.segment_index + 1} "
             f"[{seg.low_mhz:g}, {seg.high_mhz:g}] MHz 的空闲窗 "
             f"[{window.low_mhz:g}, {window.high_mhz:g}] MHz")
    if n_feasible <= 1:
        return (f"带宽 {c.bandwidth_mhz:g} MHz 只有该空闲窗能完整容纳"
                f"（其余段/窗被排除窗或段边界切窄），故落入{where}")
    return (f"共 {n_feasible} 个空闲窗可容纳带宽 {c.bandwidth_mhz:g} MHz，"
            f"该窗使相对录入位置 {c.center_mhz:g} MHz 的总偏移最小，故落入{where}")


def plan(carriers: list[Carrier], rules: AnalysisRules,
         band: BandLimits | None = None, mode: str = "guard_only",
         allocation: Allocation | None = None) -> dict:
    """用 CP-SAT 求一组可行频率位置。

    allocation 缺省时用 band 构造等价单段方案（向后兼容旧调用）。
    """
    from ortools.sat.python import cp_model

    if allocation is None:
        if band is None:
            raise ValueError("必须给出 band 或 allocation")
        allocation = allocation_from_band(band)
    windows = compute_free_windows(allocation)

    # ---- 逐载波可行性预检：放不下的载波直接给出诊断，不必进求解器 ----
    per_carrier_windows: list[list[FreeWindow]] = []
    impossible: list[dict] = []
    for c in carriers:
        feas = feasible_windows_for(windows, c.bandwidth_mhz)
        per_carrier_windows.append(feas)
        if not feas:
            impossible.append({
                "name": c.name, "bandwidth_mhz": c.bandwidth_mhz,
                "reason": diagnose_carrier_fit(c.name, c.bandwidth_mhz, allocation),
            })
    if impossible or (carriers and not windows):
        names = "、".join(d["name"] for d in impossible) or "全部载波"
        return {
            "feasible": False,
            "status": "INFEASIBLE",
            "message": f"载波 {names} 在当前频谱分配方案下无容身空闲窗；"
                       f"请加宽可用段、调整排除窗或减小载波带宽。",
            "assignments": [],
            "pair_constraints": [],
            "infeasible_carriers": impossible,
            "mode": mode,
            "allocation": allocation_view(allocation),
        }

    model = cp_model.CpModel()
    n = len(carriers)

    # kHz 整数域
    def khz(mhz: float) -> int:
        return int(round(mhz * 1000.0 / GRID_KHZ))

    halfbw = [khz(c.bandwidth_mhz / 2.0) for c in carriers]
    lo = khz(min(w.low_mhz for w in windows))
    hi = khz(max(w.high_mhz for w in windows))

    x: dict[int, cp_model.IntVar] = {}
    deviation: dict[int, cp_model.IntVar] = {}
    for i, c in enumerate(carriers):
        x[i] = model.new_int_var(lo + halfbw[i], hi - halfbw[i], f"center_{i}")
        # 偏好位置：录入中心截断到最近的可容纳空闲窗
        centers_mhz = [(w.low_mhz + c.bandwidth_mhz / 2.0,
                        w.high_mhz - c.bandwidth_mhz / 2.0)
                       for w in per_carrier_windows[i]]
        pref = khz(_clip_to_windows(c.center_mhz, centers_mhz))
        deviation[i] = model.new_int_var(0, hi - lo, f"dev_{i}")
        model.add_abs_equality(deviation[i], x[i] - pref)

        # ---- 分段选择：恰好落入一个可容纳空闲窗 ----
        lits = []
        for k, w in enumerate(per_carrier_windows[i]):
            lit = model.new_bool_var(f"c{i}_in_w{k}")
            model.add(x[i] >= khz(w.low_mhz) + halfbw[i]).only_enforce_if(lit)
            model.add(x[i] <= khz(w.high_mhz) - halfbw[i]).only_enforce_if(lit)
            lits.append(lit)
        model.add_exactly_one(lits)

    pair_info: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = carriers[i], carriers[j]
            same_pol = a.polarization == b.polarization
            # 同极化无极化隔离，按禁止同频处理；异极化查输入规则
            policy = "forbidden" if same_pol else rules.policy_for(a.polarization, b.polarization)
            if policy == "allowed":
                pair_info.append({"a": a.name, "b": b.name, "constraint": "co-channel allowed",
                                  "required_edge_mhz": 0.0})
                continue

            if mode == "mask_aware":
                # 双向泄漏都达标所需净距（与排序无关）
                req_edge = khz(_safe_edge_gap(a, b, rules))
                req_i_left = req_j_left = req_edge
            else:
                g = khz(rules.guard_required_mhz)
                req_i_left = req_j_left = g

            # 析取： x_i + half_i + req_i_left + half_j <= x_j
            #   或   x_j + half_j + req_j_left + half_i <= x_i
            lit_ij = model.new_bool_var(f"{i}_left_of_{j}")
            lit_ji = model.new_bool_var(f"{j}_left_of_{i}")
            model.add(x[i] + halfbw[i] + req_i_left + halfbw[j] <= x[j]).only_enforce_if(lit_ij)
            model.add(x[j] + halfbw[j] + req_j_left + halfbw[i] <= x[i]).only_enforce_if(lit_ji)
            # 两个“排在左边”的布尔至少一个为真（二者互斥，覆盖所有排序）
            model.add_bool_or(lit_ij, lit_ji)

            pair_info.append({
                "a": a.name, "b": b.name,
                "constraint": "ordered non-overlap",
                "required_edge_mhz": round(req_i_left * GRID_KHZ / 1000.0, 4),
                "reuse_policy": policy,
            })

    model.minimize(sum(deviation.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "feasible": False,
            "status": solver.status_name(status),
            "message": "在给定可用段、排除窗与间隔约束下找不到可行方案，"
                       "可放宽保护间隔、扩大可用段、减少排除窗或允许极化复用。",
            "assignments": [],
            "pair_constraints": pair_info,
            "mode": mode,
            "allocation": allocation_view(allocation),
        }

    assignments = []
    for i, c in enumerate(carriers):
        new_center = solver.value(x[i]) * GRID_KHZ / 1000.0
        new_low = new_center - c.bandwidth_mhz / 2.0
        new_high = new_center + c.bandwidth_mhz / 2.0
        win = next(w for w in per_carrier_windows[i]
                   if w.low_mhz - 1e-6 <= new_low and new_high <= w.high_mhz + 1e-6)
        assignments.append({
            "name": c.name,
            "original_center_mhz": c.center_mhz,
            "center_mhz": round(new_center, 4),
            "low_mhz": round(new_low, 4),
            "high_mhz": round(new_high, 4),
            "bandwidth_mhz": c.bandwidth_mhz,
            "power_dbm": c.power_dbm,
            "polarization": c.polarization,
            "mask_name": c.mask_name,
            "shift_mhz": round(new_center - c.center_mhz, 4),
            "segment_label": win.segment_label or f"S{win.segment_index + 1}",
            "window_mhz": [round(win.low_mhz, 4), round(win.high_mhz, 4)],
            "segment_reason": _segment_reason(
                c, win, len(per_carrier_windows[i]), allocation),
        })

    used = [(p["low_mhz"], p["high_mhz"]) for p in assignments]
    return {
        "feasible": True,
        "status": solver.status_name(status),
        "mode": mode,
        "objective_khz": solver.objective_value * GRID_KHZ,
        "assignments": assignments,
        "pair_constraints": pair_info,
        "occupied_span_mhz": round(max(h for _, h in used) - min(l for l, _ in used), 4),
        "band_limits_mhz": [min(w.low_mhz for w in windows),
                            max(w.high_mhz for w in windows)],
        "allocation": allocation_view(allocation),
        "message": f"已找到可行频率位置（共 {n} 个载波，目标偏移 "
                   f"{solver.objective_value * GRID_KHZ:.0f} kHz）。",
    }

