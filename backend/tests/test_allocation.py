"""频谱分配方案：纯函数、分段规划与分配感知分析的测试。"""
import math

import pytest

from app.services.allocation import (Allocation, Exclusion, Segment,
                                     compute_free_windows, describe_fit,
                                     diagnose_carrier_fit, find_window,
                                     validate_allocation)
from app.services.analysis import AnalysisRules, Carrier, analyze
from app.services.planner import plan

C = lambda **kw: Carrier(id=None, **kw)
RULES = AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                      reuse_policy={"H|V": "unknown", "RHCP|V": "allowed"})


def c(name, center, power=20.0, pol="H", mask="strict", bw=4.0):
    return C(name=name, center_mhz=center, bandwidth_mhz=bw,
             power_dbm=power, polarization=pol, mask_name=mask)


def two_seg_allocation():
    """[90,110] 与 [130,150] 两段，中间 20 MHz 空洞。"""
    return Allocation(
        segments=(Segment(90.0, 110.0, "S1"), Segment(130.0, 150.0, "S2")),
        exclusions=())


# ---- 纯函数：空闲窗与校验 ---------------------------------------------------

def test_free_windows_subtract_exclusions():
    alloc = Allocation(
        segments=(Segment(80.0, 120.0, "S1"),),
        exclusions=(Exclusion(95.0, 99.0, "hole"), Exclusion(110.0, 112.0, "")))
    wins = compute_free_windows(alloc)
    assert [(w.low_mhz, w.high_mhz) for w in wins] == [
        (80.0, 95.0), (99.0, 110.0), (112.0, 120.0)]


def test_free_windows_exclusion_clipped_to_segment():
    # 排除窗部分搭在段外：按交集裁剪
    alloc = Allocation(
        segments=(Segment(90.0, 100.0, "S1"), Segment(110.0, 120.0, "S2")),
        exclusions=(Exclusion(85.0, 92.0, "overlap boundary"),))
    wins = compute_free_windows(alloc)
    assert [(w.low_mhz, w.high_mhz) for w in wins] == [(92.0, 100.0), (110.0, 120.0)]


def test_validate_rejects_bad_geometry():
    with pytest.raises(ValueError, match="至少需要一个"):
        validate_allocation([], [])
    with pytest.raises(ValueError, match="上界必须大于下界"):
        validate_allocation([Segment(100.0, 90.0)], [])
    with pytest.raises(ValueError, match="重叠"):
        validate_allocation([Segment(90.0, 105.0), Segment(100.0, 120.0)], [])
    with pytest.raises(ValueError, match="包络"):
        validate_allocation([Segment(90.0, 100.0)], [Exclusion(200.0, 210.0)])
    # 相切不算重叠
    validate_allocation([Segment(90.0, 100.0), Segment(100.0, 110.0)], [])


def test_diagnose_carrier_fit_distinguishes_causes():
    alloc = Allocation(segments=(Segment(90.0, 120.0, "S1"),),
                       exclusions=(Exclusion(104.0, 106.0, "hole"),))
    # 段宽 30，排除窗把空闲窗切成 14/14：带宽 20 只能跨窗
    msg = diagnose_carrier_fit("X", 20.0, alloc)
    assert "排除窗" in msg
    # 带宽 40 超过段宽本身
    msg2 = diagnose_carrier_fit("Y", 40.0, alloc)
    assert "大于任一可用段" in msg2
    assert diagnose_carrier_fit("Z", 10.0, alloc) is None


# ---- 分段规划 ---------------------------------------------------------------

def _assert_all_inside_windows(result, allocation):
    wins = [(w["low_mhz"], w["high_mhz"]) for w in result["allocation"]["free_windows"]]
    for a in result["assignments"]:
        assert any(lo - 1e-6 <= a["low_mhz"] and a["high_mhz"] <= hi + 1e-6
                   for lo, hi in wins), f"{a['name']} 跨洞或越界: {a}"


def test_two_segments_carriers_land_in_correct_segment():
    alloc = two_seg_allocation()
    carriers = [c("L1", 95), c("L2", 100), c("H1", 140), c("H2", 145)]
    r = plan(carriers, RULES, allocation=alloc, mode="guard_only")
    assert r["feasible"]
    _assert_all_inside_windows(r, alloc)
    by_name = {a["name"]: a for a in r["assignments"]}
    # 低段载波留在 S1，高段载波留在 S2（最小偏移目标不会把它们搬过空洞）
    assert by_name["L1"]["segment_label"] == "S1"
    assert by_name["L2"]["segment_label"] == "S1"
    assert by_name["H1"]["segment_label"] == "S2"
    assert by_name["H2"]["segment_label"] == "S2"
    assert by_name["L1"]["high_mhz"] <= 110.0 + 1e-6
    assert by_name["H1"]["low_mhz"] >= 130.0 - 1e-6
    # 每条载波都有落在该段的说明
    for a in r["assignments"]:
        assert a["segment_reason"]
        assert a["window_mhz"][0] <= a["low_mhz"]
        assert a["window_mhz"][1] >= a["high_mhz"]


def test_exclusion_window_never_crossed():
    alloc = Allocation(
        segments=(Segment(80.0, 120.0, "S1"),),
        exclusions=(Exclusion(95.0, 99.0, "保护空洞"),))
    carriers = [c(f"K{i}", 84 + i * 3.0) for i in range(6)]
    r = plan(carriers, RULES, allocation=alloc, mode="guard_only")
    assert r["feasible"]
    _assert_all_inside_windows(r, alloc)
    for a in r["assignments"]:
        # 占用频带不得与排除窗相交
        assert a["high_mhz"] <= 95.0 + 1e-6 or a["low_mhz"] >= 99.0 - 1e-6, a


def test_carrier_wider_than_any_segment_infeasible():
    alloc = two_seg_allocation()  # 两段各 20 MHz
    r = plan([c("WIDE", 100, bw=25.0)], RULES, allocation=alloc, mode="guard_only")
    assert not r["feasible"]
    assert r["status"] == "INFEASIBLE"
    bad = r["infeasible_carriers"]
    assert bad and bad[0]["name"] == "WIDE"
    assert "大于任一可用段" in bad[0]["reason"]


def test_carrier_only_fitting_across_exclusion_infeasible():
    # 段宽 30 MHz，排除窗切成 14 + 14；带宽 20 只能跨排除窗
    alloc = Allocation(segments=(Segment(90.0, 120.0, "S1"),),
                       exclusions=(Exclusion(104.0, 106.0, "hole"),))
    r = plan([c("X", 105, bw=20.0)], RULES, allocation=alloc, mode="guard_only")
    assert not r["feasible"]
    bad = r["infeasible_carriers"]
    assert bad and "排除窗" in bad[0]["reason"]


def test_polarization_rules_not_bypassed_by_segments():
    alloc = two_seg_allocation()
    # allowed 对：同频复用仍然允许（可同址，偏移为 0）
    r = plan([c("a", 100, pol="V"), c("b", 100, pol="RHCP")], RULES,
             allocation=alloc, mode="guard_only")
    assert r["feasible"]
    assert all(x["shift_mhz"] == 0.0 for x in r["assignments"])

    # unknown 对：仍须按保护间隔排开，分段逻辑不得绕过
    r2 = plan([c("h", 100, pol="H"), c("v", 100, pol="V")], RULES,
              allocation=alloc, mode="guard_only")
    assert r2["feasible"]
    xs = sorted(r2["assignments"], key=lambda a: a["center_mhz"])
    gap = xs[1]["low_mhz"] - xs[0]["high_mhz"]
    assert gap >= RULES.guard_required_mhz - 1e-9

    # forbidden 对：同样必须排开
    rules_f = AnalysisRules(1.0, -45.0, {"H|V": "forbidden"})
    r3 = plan([c("h", 100, pol="H"), c("v", 100, pol="V")], rules_f,
              allocation=alloc, mode="guard_only")
    assert r3["feasible"]
    xs3 = sorted(r3["assignments"], key=lambda a: a["center_mhz"])
    assert xs3[1]["low_mhz"] - xs3[0]["high_mhz"] >= 1.0 - 1e-9


def test_mask_aware_postcheck_clean_across_segments():
    alloc = Allocation(
        segments=(Segment(90.0, 112.0, "S1"), Segment(130.0, 152.0, "S2")),
        exclusions=(Exclusion(100.0, 102.0, "hole"),))
    carriers = [c("C1", 96, 30, "H", "loose"), c("C2", 104, 20, "H", "loose"),
                c("C3", 140, 30, "H", "strict"), c("C4", 146, 20, "V", "strict")]
    r = plan(carriers, RULES, allocation=alloc, mode="mask_aware")
    assert r["feasible"]
    _assert_all_inside_windows(r, alloc)
    planned = [c(a["name"], a["center_mhz"], a["power_dbm"], a["polarization"],
                 a["mask_name"], a["bandwidth_mhz"]) for a in r["assignments"]]
    post = analyze(planned, RULES, alloc)
    assert post["counts"]["error"] == 0
    assert post["counts"]["warning"] == 0


def test_single_band_backward_compatible():
    # 旧调用（band 参数）等价于单段方案
    carriers = [c("a", 100), c("b", 100)]
    from app.services.planner import BandLimits
    r = plan(carriers, RULES, BandLimits(95, 110), "guard_only")
    assert r["feasible"]
    assert len(r["allocation"]["segments"]) == 1
    assert r["allocation"]["free_windows"] == [
        {"segment_index": 0, "segment_label": "S1", "low_mhz": 95.0, "high_mhz": 110.0}]


# ---- 分配感知分析 ------------------------------------------------------------

def test_analyze_flags_out_of_band_and_crossing_exclusion():
    alloc = Allocation(
        segments=(Segment(90.0, 110.0, "S1"), Segment(130.0, 150.0, "S2")),
        exclusions=(Exclusion(98.0, 102.0, "保护空洞"),))
    inside = c("OK", 94)          # [92,96] 完整在 S1 下窗
    crossing = c("X", 100)        # [98,102] 正好压在排除窗上
    outside = c("Y", 120)         # [118,122] 在段间空洞里
    res = analyze([inside, crossing, outside],
                  AnalysisRules(guard_required_mhz=0.0), alloc)
    oob = {f["carrier_a"]: f for f in res["findings"] if f["type"] == "out_of_band"}
    assert set(oob) == {"X", "Y"}
    assert "排除窗" in oob["X"]["fit_reason"]
    assert oob["X"]["severity"] == "error"
    assert res["counts"]["error"] >= 2


def test_analyze_without_allocation_unchanged():
    res = analyze([c("a", 100), c("b", 106)], RULES)
    assert not [f for f in res["findings"] if f["type"] == "out_of_band"]
