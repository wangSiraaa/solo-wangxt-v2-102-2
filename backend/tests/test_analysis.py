"""冲突分析与功率汇总的单元测试（纯函数，不需要数据库/HTTP）。"""
import math

from app.services.analysis import (AnalysisRules, Carrier, analyze,
                                   edge_gap_mhz, leakage_power_dbm)
from app.services.units import dbm_to_watt, total_power_dbm, watt_to_dbm

C = lambda **kw: Carrier(id=None, **kw)

BASE = dict(bandwidth_mhz=4, polarization="H", mask_name="strict")
RULES = AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                      reuse_policy={"H|V": "unknown", "RHCP|V": "allowed"})


def c(name, center, power=20.0, pol="H", mask="strict", bw=4.0):
    return C(name=name, center_mhz=center, bandwidth_mhz=bw,
             power_dbm=power, polarization=pol, mask_name=mask)


def test_power_units():
    assert math.isclose(dbm_to_watt(30), 1.0)
    assert math.isclose(watt_to_dbm(1.0), 30.0)
    # 两个 30 dBm (1 W) 载波：线性域 2 W -> 33.01 dBm，绝不是 60 dBm
    assert math.isclose(total_power_dbm([30, 30]), 33.0103, abs_tol=1e-3)
    # 10 个 20 dBm (0.1 W)：1 W -> 30 dBm
    assert math.isclose(total_power_dbm([20] * 10), 30.0, abs_tol=1e-9)


def test_edge_gap_signs():
    a, b = c("a", 100), c("b", 106)       # [98,102],[104,108] 净距 2
    assert math.isclose(edge_gap_mhz(a, b), 2.0)
    b2 = c("b2", 104)                      # [102,106] 相切
    assert math.isclose(edge_gap_mhz(a, b2), 0.0)
    b3 = c("b3", 103)                      # [101,105] 重叠 1
    assert math.isclose(edge_gap_mhz(a, b3), -1.0)


def test_overlap_detected_as_pair():
    res = analyze([c("C9", 170), c("C10", 173.5)], RULES)
    ov = [f for f in res["findings"] if f["type"] == "overlap"]
    assert len(ov) == 1
    assert {ov[0]["carrier_a"], ov[0]["carrier_b"]} == {"C9", "C10"}
    assert math.isclose(ov[0]["overlap_mhz"], 0.5)
    assert ov[0]["severity"] == "error"


def test_guard_shortfall_detected_as_pair():
    res = analyze([c("C4", 130, 30, "H", "strict"),
                   c("C5", 134.5, 25, "H", "loose")], RULES)
    gs = [f for f in res["findings"] if f["type"] == "guard_shortfall"]
    assert len(gs) == 1
    assert {gs[0]["carrier_a"], gs[0]["carrier_b"]} == {"C4", "C5"}
    assert math.isclose(gs[0]["gap_mhz"], 0.5)
    assert math.isclose(gs[0]["deficit_mhz"], 0.5)


def test_guard_ok_no_finding():
    res = analyze([c("a", 100, mask="strict"), c("b", 106, mask="strict")], RULES)
    assert not [f for f in res["findings"] if f["type"] == "guard_shortfall"]


def test_mask_tail_is_directional_with_power():
    # 同几何、同掩模，只有功率不同：强 -> 弱越界，弱 -> 强也越界但数值不同
    strong = c("C7", 150, 30, "H", "loose")
    weak = c("C8", 156, 20, "H", "strict")
    l_sw = leakage_power_dbm(strong, weak)
    l_ws = leakage_power_dbm(weak, strong)
    assert l_sw > -45.0 and l_ws < -45.0
    res = analyze([strong, weak], RULES)
    tails = [f for f in res["findings"] if f["type"] == "mask_tail"]
    assert len(tails) == 1
    assert tails[0]["carrier_a"] == "C7" and tails[0]["carrier_b"] == "C8"

    # 把弱载波也换成 loose：两个方向都越界，且强->弱泄漏量更大
    weak2 = c("C2", 104.5, 20, "H", "loose")
    strong2 = c("C1", 100, 30, "H", "loose")
    res2 = analyze([strong2, weak2], RULES)
    t2 = {(f["carrier_a"], f["carrier_b"]): f["leakage_dbm"]
          for f in res2["findings"] if f["type"] == "mask_tail"}
    assert ("C1", "C2") in t2 and ("C2", "C1") in t2
    assert t2[("C1", "C2")] > t2[("C2", "C1")]


def test_mask_tail_zero_outside_span():
    # 两 strict(span 7) 载波净距 > span - bw/2，无信号交叠 => -inf
    far = c("a", 100, 30, "H", "loose")
    victim = c("b", 130, 20, "H", "strict")
    assert leakage_power_dbm(far, victim) == float("-inf")


def test_polarization_policies():
    h = c("H1", 100, pol="H")
    v = c("V1", 100, pol="V")
    rhcp = c("R1", 100, pol="RHCP")

    # unknown：同频段重叠 -> 待评估，不是 error
    res = analyze([h, v], RULES)
    pend = [f for f in res["findings"] if f["type"] == "reuse_unknown"]
    assert len(pend) == 1 and pend[0]["severity"] == "pending"
    assert not [f for f in res["findings"] if f["type"] == "overlap"]

    # allowed：允许复用，不报任何几何/泄漏冲突
    res2 = analyze([v, rhcp], RULES)
    assert res2["findings"] == []

    # forbidden 规则：报 overlap error
    rules_f = AnalysisRules(1.0, -45.0, {"H|V": "forbidden"})
    res3 = analyze([h, v], rules_f)
    assert [f for f in res3["findings"] if f["type"] == "overlap"]

    # 同极化永远按需要间隔处理（即使没有任何规则）
    res4 = analyze([h, c("H2", 100, pol="H")], AnalysisRules(1.0, -45.0))
    assert [f for f in res4["findings"] if f["type"] == "overlap"]


def test_summary_structure():
    res = analyze([c("a", 100, 30), c("b", 106, 20)], RULES)
    ps = res["power_summary"]
    assert ps["carrier_count"] == 2
    assert math.isclose(ps["total_power_w"], 1.1)
    assert math.isclose(ps["total_power_dbm"], 30.414, abs_tol=1e-3)
