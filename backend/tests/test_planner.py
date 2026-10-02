"""OR-Tools 规划器测试。"""
from app.services import allocation as alloc_mod
from app.services.analysis import AnalysisRules, Carrier, analyze, leakage_power_dbm
from app.services.planner import BandLimits, plan

C = lambda **kw: Carrier(id=None, **kw)
RULES = AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                      reuse_policy={"H|V": "unknown", "RHCP|V": "allowed"})


def c(name, center, power=20.0, pol="H", mask="strict", bw=4.0):
    return C(name=name, center_mhz=center, bandwidth_mhz=bw,
             power_dbm=power, polarization=pol, mask_name=mask)


def alloc(segments, exclusions=(), version=1, name="t"):
    return alloc_mod.Allocation(
        segments=tuple(alloc_mod.Segment(lo, hi) for lo, hi in segments),
        exclusions=tuple(alloc_mod.Exclusion(lo, hi, why) for lo, hi, why in exclusions),
        version=version, name=name)


def _gaps_ok(assignments, req=1.0, check_pols=("H",)):
    by_pol = {}
    for a in assignments:
        by_pol.setdefault(a["polarization"], []).append(a)
    for pol in check_pols:
        xs = sorted(by_pol.get(pol, []), key=lambda a: a["center_mhz"])
        for x, y in zip(xs, xs[1:]):
            gap = y["low_mhz"] - x["high_mhz"]
            assert gap >= req - 1e-9, (pol, x["name"], y["name"], gap)


def test_guard_only_feasible_and_spaced():
    carriers = [c("C1", 100, 30, "H", "loose"), c("C2", 104.5, 20, "H", "loose"),
                c("C6", 100, 20, "V", "strict"), c("C4", 130, 30, "H", "strict")]
    r = plan(carriers, RULES, BandLimits(90, 150), "guard_only")
    assert r["feasible"]
    _gaps_ok(r["assignments"])
    # unknown 异极化对也被排开（保守处理）
    cs = sorted(r["assignments"], key=lambda a: a["center_mhz"])
    h6 = next(a for a in cs if a["name"] == "C6")
    c1 = next(a for a in cs if a["name"] == "C1")
    assert abs(h6["center_mhz"] - c1["center_mhz"]) >= 4.0 + 1.0


def test_mask_aware_satisfies_analysis_postcheck():
    carriers = [c("C1", 100, 30, "H", "loose"), c("C2", 104.5, 20, "H", "loose"),
                c("C6", 100, 20, "V", "strict"), c("C4", 130, 30, "H", "strict")]
    r = plan(carriers, RULES, BandLimits(90, 200), "mask_aware")
    assert r["feasible"]
    planned = [c(a["name"], a["center_mhz"], a["power_dbm"], a["polarization"],
                 a["mask_name"], a["bandwidth_mhz"]) for a in r["assignments"]]
    post = analyze(planned, RULES)
    assert post["counts"]["error"] == 0
    assert post["counts"]["warning"] == 0


def test_infeasible_tight_band():
    carriers = [c("a", 100), c("b", 100), c("c", 100)]
    r = plan(carriers, RULES, BandLimits(98, 105), "guard_only")
    assert not r["feasible"]


def test_allowed_polarization_cochannel():
    carriers = [c("a", 100, pol="V"), c("b", 100, pol="RHCP")]
    r = plan(carriers, RULES, BandLimits(95, 106), "guard_only")
    assert r["feasible"]
    # 两者可以同址（偏移为 0）
    assert all(a["shift_mhz"] == 0.0 for a in r["assignments"])

# ---- 多段不连续频谱 + 排除窗（验收 1 / 2 / 3） -----------------------------

def test_two_segments_assignment_and_no_hole_crossing():
    """验收 1：两段不连续频谱，不同载波落入正确段；任何占用都不跨洞/排除窗。"""
    two = alloc([(90, 100), (110, 120)])
    # A/C 偏好段一附近，B 偏好段二；D 偏好落在空洞内（105），必须被移入某一段
    carriers = [c("A", 92), c("B", 112), c("C", 96), c("D", 105)]
    r = plan(carriers, RULES, two, "guard_only")
    assert r["feasible"], r["message"]
    by = {a["name"]: a for a in r["assignments"]}
    assert by["A"]["segment_index"] == 0
    assert by["C"]["segment_index"] == 0
    assert by["B"]["segment_index"] == 1
    assert by["D"]["segment_index"] in (0, 1)
    # 每条载波都有归属解释
    for a in r["assignments"]:
        assert a["assignment_reason"]
        assert a["piece_mhz"] is not None
    # 无一跨越段间空洞或排除窗：所有占用完整位于某有效小片内
    for a in r["assignments"]:
        lo, hi = a["low_mhz"], a["high_mhz"]
        assert alloc_mod.containing_piece(two, lo, hi) is not None
        assert not alloc_mod.crossed_exclusions(two, lo, hi)
        piece_lo, piece_hi = a["piece_mhz"]
        assert lo >= piece_lo - 1e-9 and hi <= piece_hi + 1e-9


def test_exclusion_inside_segment_is_not_crossed():
    """段内排除窗把可用段切成两片，规划只能在其中一片内安置载波。"""
    a = alloc([(90, 120)], [(100, 104, "保护空洞")])
    carriers = [c("L", 95), c("R", 115)]
    r = plan(carriers, RULES, a, "guard_only")
    assert r["feasible"]
    for x in r["assignments"]:
        lo, hi = x["low_mhz"], x["high_mhz"]
        assert not alloc_mod.crossed_exclusions(a, lo, hi)
    # 两个载波分别在排除窗两侧（片宽 10 / 16）
    lx = next(x for x in r["assignments"] if x["name"] == "L")
    rx = next(x for x in r["assignments"] if x["name"] == "R")
    assert lx["high_mhz"] <= 100 + 1e-9
    assert rx["low_mhz"] >= 104 - 1e-9


def test_carrier_wider_than_every_piece_is_infeasible():
    """验收 2a：载波比任一可用段都宽 -> 明确不可行，给出原因。"""
    a = alloc([(90, 100), (110, 118)])  # 最宽净空 10 MHz
    r = plan([c("W", 95, bw=12)], RULES, a)
    assert not r["feasible"]
    assert r["status"] == "MODEL_INVALID"
    reason = r["infeasible_reasons"][0]
    assert reason["carrier"] == "W"
    assert reason["reason"] == "bandwidth_exceeds_every_piece"
    assert reason["max_piece_width_mhz"] == 10.0


def test_carrier_that_can_only_span_exclusion_is_infeasible():
    """验收 2b：载波只能跨越排除窗才能安置 -> 明确不可行。"""
    a = alloc([(90, 110)], [(98, 102, "保护空洞")])  # 两片各 8 MHz
    r = plan([c("X", 100, bw=9)], RULES, a)
    assert not r["feasible"]
    assert r["infeasible_reasons"][0]["crossed_exclusions"]
    assert "排除窗" in r["message"]
    # 两个仅略宽于单片的载波：预检通过但 CP-SAT 同样判定不可行
    r2 = plan([c("X", 94, bw=8.5), c("Y", 106, bw=8.5)], RULES, a)
    assert not r2["feasible"]


def test_allowed_policy_cochannel_within_one_piece():
    """验收 3：同频且极化允许复用的载波仍可同址，分段不改变该规则。"""
    rules = AnalysisRules(guard_required_mhz=1.0,
                          reuse_policy={"H|V": "allowed"})
    a = alloc([(90, 100), (110, 120)])
    r = plan([c("P1", 95, pol="H"), c("P2", 95, pol="V")], rules, a)
    assert r["feasible"]
    assert all(x["shift_mhz"] == 0.0 for x in r["assignments"])
    pc = {(p["a"], p["b"]): p for p in r["pair_constraints"]}
    assert pc[("P1", "P2")]["constraint"] == "co-channel allowed"


def test_forbidden_and_unknown_not_bypassed_by_segments():
    """验收 3：禁止 / 未知规则不会因分段而被绕过。"""
    # 同极化（按禁止）：窄片 [94,99] 容不下两个按 1 MHz 保护间隔排开的 4 MHz 载波
    r = plan([c("Q1", 95, pol="H"), c("Q2", 95, pol="H")], RULES,
             alloc([(94, 99)]))
    assert not r["feasible"]
    # H|V 规则未知（保守排开）：同样放不下
    r2 = plan([c("Q1", 95, pol="H"), c("Q2", 95, pol="V")], RULES,
              alloc([(94, 99)]))
    assert not r2["feasible"]
    # 放到两段里时仍然被排开（本就不可能重叠，结论一致），且未知对带排序约束
    r3 = plan([c("Q1", 95, pol="H"), c("Q2", 115, pol="V")], RULES,
              alloc([(90, 100), (110, 120)]))
    assert r3["feasible"]
    pc = {(p["a"], p["b"]): p for p in r3["pair_constraints"]}
    assert pc[("Q1", "Q2")]["constraint"] == "ordered non-overlap"
    assert pc[("Q1", "Q2")]["reuse_policy"] == "unknown"


def test_mask_aware_with_segments_postcheck_clean():
    """掩模感知模式在多段场景下仍保证规划后双向泄漏达标。"""
    a = alloc([(90, 130), (140, 200)])
    carriers = [c("C1", 100, 30, "H", "loose"), c("C2", 104.5, 20, "H", "loose"),
                c("C4", 150, 30, "H", "strict"), c("C5", 154.5, 25, "H", "loose")]
    r = plan(carriers, RULES, a, "mask_aware")
    assert r["feasible"]
    planned = [c(x["name"], x["center_mhz"], x["power_dbm"], x["polarization"],
                 x["mask_name"], x["bandwidth_mhz"]) for x in r["assignments"]]
    post = analyze(planned, RULES)
    assert post["counts"]["error"] == 0
    assert post["counts"]["warning"] == 0
    # C1/C2 与 C4/C5 被分到两段（掩模需求净距 > 10 MHz 空洞）
    seg_of = {x["name"]: x["segment_index"] for x in r["assignments"]}
    assert seg_of["C1"] == seg_of["C2"] == 0
    assert seg_of["C4"] == seg_of["C5"] == 1
