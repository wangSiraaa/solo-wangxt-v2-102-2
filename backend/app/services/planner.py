"""OR-Tools 频率位置规划（离线简化模型）。

为给定的一组载波寻找一组满足最小间隔约束的中心频率位置：

- 频率离散到 1 kHz 网格，用 CP-SAT 求解整数模型。
- 频谱分配方案（:mod:`app.services.allocation`）可以包含**多个不连续的可用
  频段**和不能跨越的**排除窗**。每个载波的占用带宽必须完整落在某个有效小片
  （可用段被排除窗切出的净空）内：为每个载波建一个“选择哪个小片”的布尔，
  用该小片的上下界约束中心频率，且每载波恰好选一个小片。不存在宽度足够的
  小片时模型必然无解，明确返回不可行。
- 同极化、或复用规则为 forbidden/unknown 的极化对：两个频带不得相交，
  且边缘净距不小于要求值；用布尔析取实现“i 在 j 左”或“j 在 i 左”。
  不同小片中的载波天然被空洞隔开，析取自动满足。
- 复用规则为 allowed（已知隔离度足够）的极化对：允许同频，不加间隔约束
  （分段逻辑不会绕过同/异极化与禁止/未知规则）。
- guard_only 模式：统一使用规则中的保护间隔。
- mask_aware 模式：每对载波的间隔按双方掩模尾部泄漏都不越限来反算
  （功率/掩模不同 => 两个方向阈值不同）。

目标：最小化各载波相对其偏好位置（录入中心频率，截断到所选小片内）的偏移量。

只输出频率方案，不连接任何设备，也不产生发射指令。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import allocation as alloc_mod
from .analysis import Carrier, AnalysisRules
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
    """旧的单一连续频段（等价于只含一个段的 Allocation）。"""
    low_mhz: float
    high_mhz: float


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


def plan(carriers: list[Carrier], rules: AnalysisRules,
         band: BandLimits | alloc_mod.Allocation,
         mode: str = "guard_only") -> dict:
    """用 CP-SAT 求一组可行频率位置。

    ``band`` 可以是旧的单连续频段 ``BandLimits``，也可以是多段 + 排除窗的
    ``Allocation``；前者内部转换为等价的单段分配。
    """
    from ortools.sat.python import cp_model

    allocation = (band if isinstance(band, alloc_mod.Allocation)
                  else alloc_mod.allocation_from_band(band.low_mhz, band.high_mhz))
    alloc_mod.validate_allocation(allocation)
    pieces_with_anchor = alloc_mod.effective_pieces_with_anchor(allocation)

    model = cp_model.CpModel()
    n = len(carriers)

    # kHz 整数域
    def khz(mhz: float) -> int:
        return int(round(mhz * 1000.0 / GRID_KHZ))

    halfbw = [khz(c.bandwidth_mhz / 2.0) for c in carriers]
    span_lo = khz(min(s.low_mhz for s in allocation.segments))
    span_hi = khz(max(s.high_mhz for s in allocation.segments))

    x: dict[int, cp_model.IntVar] = {}
    deviation: dict[int, cp_model.IntVar] = {}
    # 每个载波选择的有效小片序号（解后填）与选择布尔（求解用）
    piece_options: dict[int, list[int]] = {}
    select_bools: dict[tuple[int, int], cp_model.IntVar] = {}

    for i, c in enumerate(carriers):
        # 只保留宽度放得下该载波占用带宽的有效小片
        fit = [k for k, (p, _cuts, _lo, _hi) in enumerate(pieces_with_anchor)
               if khz(p.width_mhz) >= 2 * halfbw[i]]
        if not fit:
            return _infeasible_too_wide(c, allocation, pieces_with_anchor, mode, [])
        piece_options[i] = fit
        x[i] = model.new_int_var(span_lo, span_hi, f"center_{i}")
        deviation[i] = model.new_int_var(0, span_hi - span_lo, f"dev_{i}")

        for k in fit:
            piece = pieces_with_anchor[k][0]
            lo_k = khz(piece.low_mhz)
            hi_k = khz(piece.high_mhz)
            b = model.new_bool_var(f"c{i}_piece{k}")
            select_bools[(i, k)] = b
            # 选中该小片：占用带宽完整落在小片内（不跨空洞/排除窗）
            model.add(x[i] >= lo_k + halfbw[i]).only_enforce_if(b)
            model.add(x[i] <= hi_k - halfbw[i]).only_enforce_if(b)
        # 每载波恰好落在一个小片
        model.add_exactly_one(select_bools[(i, k)] for k in fit)
        # 偏好位置（录入中心）；偏移由各小片的可选域自然决定
        pref = khz(c.center_mhz)
        model.add_abs_equality(deviation[i], x[i] - pref)

    pair_info: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = carriers[i], carriers[j]
            same_pol = a.polarization == b.polarization
            # 同极化无极化隔离，按禁止同频处理；异极化查输入规则
            policy = "forbidden" if same_pol else rules.policy_for(a.polarization, b.polarization)
            if policy == "allowed":
                pair_info.append({"a": a.name, "b": b.name, "constraint": "co-channel allowed",
                                  "required_edge_mhz": 0.0, "reuse_policy": policy})
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
            # 若两载波被分配到不同有效小片（中间隔着段间空洞或排除窗），
            # 几何上必然满足其中一个排序约束。
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
            "message": "在给定可用频段/排除窗与间隔约束下找不到可行方案，可放宽保护间隔、"
                       "增加/扩大可用频段、缩小载波带宽或允许极化复用。",
            "assignments": [],
            "pair_constraints": pair_info,
            "mode": mode,
            "allocation": alloc_mod.allocation_view(allocation),
            "infeasible_reasons": [],
        }

    assignments = []
    for i, c in enumerate(carriers):
        new_center = solver.value(x[i]) * GRID_KHZ / 1000.0
        chosen_k = next(k for k in piece_options[i]
                        if solver.value(select_bools[(i, k)]) == 1)
        piece, cuts, seg_lo, seg_hi = pieces_with_anchor[chosen_k]
        # 归属解释：为什么落在该段/小片
        pref = c.center_mhz
        pref_in_chosen = (piece.low_mhz <= pref <= piece.high_mhz)
        if abs(new_center - pref) < 1e-9:
            why = f"录入位置 {pref:g} MHz 本就位于 S{_segment_index_of(allocation, seg_lo) + 1} " \
                  f"的有效小片 [{piece.low_mhz:g}, {piece.high_mhz:g}] 内，无需移动"
        elif pref_in_chosen:
            why = (f"仍在 S{_segment_index_of(allocation, seg_lo) + 1} 的同一小片"
                   f"[{piece.low_mhz:g}, {piece.high_mhz:g}] 内，但为满足与其他载波的"
                   f"间隔/掩模约束在片内移动 {new_center - pref:+.3g} MHz")
        else:
            why = (f"录入中心 {pref:g} MHz 不在可行净空（在其他段/空洞/排除窗），"
                   f"移入 S{_segment_index_of(allocation, seg_lo) + 1} 的小片"
                   f"[{piece.low_mhz:g}, {piece.high_mhz:g}] 以最小化偏移"
                   f"（移动 {new_center - pref:+.3g} MHz）")
        if cuts:
            why += f"；该小片由 {len(cuts)} 个排除窗从段内切出"
        crossed = alloc_mod.crossed_exclusions(
            allocation, new_center - c.bandwidth_mhz / 2.0,
            new_center + c.bandwidth_mhz / 2.0)
        if crossed:
            # 理论上不会发生（约束保证），作为防御性检查
            why += "；警告：占用跨越了排除窗"
        assignments.append({
            "name": c.name,
            "original_center_mhz": c.center_mhz,
            "center_mhz": round(new_center, 4),
            "low_mhz": round(new_center - c.bandwidth_mhz / 2.0, 4),
            "high_mhz": round(new_center + c.bandwidth_mhz / 2.0, 4),
            "bandwidth_mhz": c.bandwidth_mhz,
            "power_dbm": c.power_dbm,
            "polarization": c.polarization,
            "mask_name": c.mask_name,
            "shift_mhz": round(new_center - c.center_mhz, 4),
            "segment_index": _segment_index_of(allocation, seg_lo),
            "segment_mhz": [seg_lo, seg_hi],
            "piece_index": chosen_k,
            "piece_mhz": [round(piece.low_mhz, 4), round(piece.high_mhz, 4)],
            "crosses_exclusion": bool(crossed),
            "assignment_reason": why,
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
        "band_limits_mhz": [allocation.segments[0].low_mhz, allocation.segments[-1].high_mhz],
        "allocation": alloc_mod.allocation_view(allocation),
        "infeasible_reasons": [],
        "message": f"已找到可行频率位置（共 {n} 个载波，分布于 "
                   f"{len({a['segment_index'] for a in assignments})} 个可用段，目标偏移 "
                   f"{solver.objective_value * GRID_KHZ:.0f} kHz）。",
    }


def _segment_index_of(allocation: alloc_mod.Allocation, seg_low_mhz: float) -> int:
    for k, s in enumerate(allocation.segments):
        if abs(s.low_mhz - seg_low_mhz) < alloc_mod.EPS_MHZ:
            return k
    return -1


def _infeasible_too_wide(c: Carrier, allocation: alloc_mod.Allocation,
                         pieces_with_anchor, mode: str,
                         pair_info: list[dict]) -> dict:
    """载波比任何有效小片都宽（只能跨空洞/排除窗）时的明确不可行返回。"""
    widths = sorted({round(p.width_mhz, 4) for p, _c, _l, _h in pieces_with_anchor},
                    reverse=True)
    crossed = alloc_mod.crossed_exclusions(
        allocation, c.center_mhz - c.bandwidth_mhz / 2.0,
        c.center_mhz + c.bandwidth_mhz / 2.0)
    reason = (
        f"载波 {c.name} 占用带宽 {c.bandwidth_mhz:g} MHz 大于任一可用净空小片"
        f"（最宽 {widths[0] if widths else 0:g} MHz），无法在不跨越段间空洞或"
        f"排除窗的前提下安置")
    if crossed:
        names = ", ".join(f"[{e.low_mhz:g}, {e.high_mhz:g}]" + (f" {e.reason}" if e.reason else "")
                          for e in crossed)
        reason += f"；录入位置还跨越了排除窗：{names}"
    return {
        "feasible": False,
        "status": "MODEL_INVALID",
        "message": reason + "。请缩小载波带宽、扩大可用频段或调整排除窗。",
        "assignments": [],
        "pair_constraints": pair_info,
        "mode": mode,
        "allocation": alloc_mod.allocation_view(allocation),
        "infeasible_reasons": [{
            "carrier": c.name,
            "reason": "bandwidth_exceeds_every_piece",
            "bandwidth_mhz": c.bandwidth_mhz,
            "max_piece_width_mhz": widths[0] if widths else 0.0,
            "piece_widths_mhz": widths,
            "crossed_exclusions": [{"low_mhz": e.low_mhz, "high_mhz": e.high_mhz,
                                    "reason": e.reason} for e in crossed],
        }],
    }
