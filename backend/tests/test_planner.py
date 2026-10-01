"""OR-Tools 规划器测试。"""
from app.services.analysis import AnalysisRules, Carrier, analyze, leakage_power_dbm
from app.services.planner import BandLimits, plan

C = lambda **kw: Carrier(id=None, **kw)
RULES = AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                      reuse_policy={"H|V": "unknown", "RHCP|V": "allowed"})


def c(name, center, power=20.0, pol="H", mask="strict", bw=4.0):
    return C(name=name, center_mhz=center, bandwidth_mhz=bw,
             power_dbm=power, polarization=pol, mask_name=mask)


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
