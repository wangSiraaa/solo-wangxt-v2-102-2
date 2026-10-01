"""OR-Tools 频率位置规划（离线简化模型）。

为给定的一组载波寻找一组满足最小间隔约束的中心频率位置：

- 频率离散到 1 kHz 网格，用 CP-SAT 求解整数模型。
- 同极化、或复用规则为 forbidden/unknown 的极化对：两个频带不得相交，
  且边缘净距不小于要求值；用 AddCircuit 实现“i 在 j 左”或“j 在 i 左”的析取。
- 复用规则为 allowed（已知隔离度足够）的极化对：允许同频，不加间隔约束。
- guard_only 模式：统一使用规则中的保护间隔。
- mask_aware 模式：每对载波的间隔按双方掩模尾部泄漏都不越限来反算
  （功率/掩模不同 => 两个方向阈值不同）。

目标：最小化各载波相对其偏好位置（录入中心频率，截断到可用频段内）的偏移量。

只输出频率方案，不连接任何设备，也不产生发射指令。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

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
         band: BandLimits, mode: str = "guard_only") -> dict:
    """用 CP-SAT 求一组可行频率位置。"""
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    n = len(carriers)

    # kHz 整数域
    def khz(mhz: float) -> int:
        return int(round(mhz * 1000.0 / GRID_KHZ))

    halfbw = [khz(c.bandwidth_mhz / 2.0) for c in carriers]
    lo = khz(band.low_mhz)
    hi = khz(band.high_mhz)

    x: dict[int, cp_model.IntVar] = {}
    deviation: dict[int, cp_model.IntVar] = {}
    for i, c in enumerate(carriers):
        x[i] = model.new_int_var(lo + halfbw[i], hi - halfbw[i], f"center_{i}")
        pref = min(max(khz(c.center_mhz), lo + halfbw[i]), hi - halfbw[i])
        deviation[i] = model.new_int_var(0, hi - lo, f"dev_{i}")
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
            "message": "在给定频段与间隔约束下找不到可行方案，可放宽保护间隔、扩大频段或允许极化复用。",
            "assignments": [],
            "pair_constraints": pair_info,
            "mode": mode,
        }

    assignments = []
    for i, c in enumerate(carriers):
        new_center = solver.value(x[i]) * GRID_KHZ / 1000.0
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
        "band_limits_mhz": [band.low_mhz, band.high_mhz],
        "message": f"已找到可行频率位置（共 {n} 个载波，目标偏移 "
                   f"{solver.objective_value * GRID_KHZ:.0f} kHz）。",
    }
