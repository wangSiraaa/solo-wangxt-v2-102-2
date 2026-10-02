"""API 集成测试：用内存 SQLite 覆盖 PostgreSQL engine，不依赖外部服务。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base, CarrierRow, MaskRow, Scenario
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services.masks import MASKS


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for m in MASKS.values():
            s.add(MaskRow(name=m.name, points=[list(p) for p in m.points],
                          span_mhz=m.span_mhz, description=m.description))
        sc = Scenario(name="教学演示场景", description="t", band_low_mhz=80,
                      band_high_mhz=220, guard_required_mhz=1.0,
                      leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                      carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS])
        s.add(sc)
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_masks(client):
    names = {m["name"] for m in client.get("/api/masks").json()}
    assert {"strict", "loose", "clean"} <= names


def test_analyze_demo_locates_every_conflict_pair(client):
    sc = client.get("/api/scenarios/1").json()
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
                      "reuse_policy": sc["reuse_policy"]},
            "plot_grid_mhz": 0.05}
    res = client.post("/api/analyze", json=body).json()

    pairs = {(f["carrier_a"], f["carrier_b"], f["type"]) for f in res["findings"]}
    # 频带重叠：C9/C10
    assert ("C9", "C10", "overlap") in pairs
    # 保护带不足：C1/C2、C4/C5
    assert ("C1", "C2", "guard_shortfall") in pairs
    assert ("C4", "C5", "guard_shortfall") in pairs
    # 掩模尾部越界（方向性，载波对+方向）
    assert ("C1", "C2", "mask_tail") in pairs
    assert ("C2", "C1", "mask_tail") in pairs
    assert ("C7", "C8", "mask_tail") in pairs
    # 功率不对称：C8 -> C7 不应越界
    assert ("C8", "C7", "mask_tail") not in pairs
    # 极化复用待评估：C1/C6；允许复用：C11/C12 无任何条目
    assert ("C1", "C6", "reuse_unknown") in pairs
    assert not [p for p in pairs if set(p[:2]) == {"C11", "C12"}]

    # 线性域功率汇总
    ps = res["power_summary"]
    assert ps["total_power_dbm"] < ps["naive_dbm_sum"]


def test_plan_endpoint(client):
    sc = client.get("/api/scenarios/1").json()
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
                      "reuse_policy": sc["reuse_policy"]},
            "band_low_mhz": 80, "band_high_mhz": 300, "mode": "mask_aware"}
    r = client.post("/api/plan", json=body).json()
    assert r["feasible"]
    assert r["post_check"]["counts"]["error"] == 0


def test_scenario_crud(client):
    payload = {"name": "新建场景", "description": "d", "band_low_mhz": 90,
               "band_high_mhz": 120, "guard_required_mhz": 1.0,
               "leakage_limit_dbm": -45.0, "reuse_policy": {},
               "carriers": [{"name": "A", "center_mhz": 100, "bandwidth_mhz": 4,
                             "power_dbm": 10, "polarization": "H", "mask_name": "strict"}]}
    r = client.post("/api/scenarios", json=payload)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert client.get(f"/api/scenarios/{sid}").json()["carriers"][0]["name"] == "A"
    assert client.delete(f"/api/scenarios/{sid}").json()["deleted"] == sid
    assert client.get(f"/api/scenarios/{sid}").status_code == 404


def test_bad_mask_rejected(client):
    body = {"carriers": [{"name": "A", "center_mhz": 100, "bandwidth_mhz": 4,
                          "power_dbm": 10, "polarization": "H", "mask_name": "nope"}]}
    assert client.post("/api/analyze", json=body).status_code == 400


# ---- 频谱分配方案 -----------------------------------------------------------

TWO_SEG = {"segments": [{"low_mhz": 90, "high_mhz": 110, "label": "S1"},
                        {"low_mhz": 130, "high_mhz": 150, "label": "S2"}],
           "exclusions": [{"low_mhz": 98, "high_mhz": 102, "reason": "保护空洞"}]}
CARRIERS_2SEG = [
    {"name": "L1", "center_mhz": 94, "bandwidth_mhz": 4, "power_dbm": 20,
     "polarization": "H", "mask_name": "strict"},
    {"name": "L2", "center_mhz": 106, "bandwidth_mhz": 4, "power_dbm": 20,
     "polarization": "H", "mask_name": "strict"},
    {"name": "H1", "center_mhz": 140, "bandwidth_mhz": 4, "power_dbm": 20,
     "polarization": "H", "mask_name": "strict"},
]


def _mk_scenario(client, name, **kw):
    payload = {"name": name, "description": "", "band_low_mhz": 90,
               "band_high_mhz": 150, "guard_required_mhz": 1.0,
               "leakage_limit_dbm": -45.0, "reuse_policy": {},
               "carriers": CARRIERS_2SEG}
    payload.update(kw)
    r = client.post("/api/scenarios", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_plan_with_two_segments_and_exclusion(client):
    body = {"carriers": CARRIERS_2SEG,
            "rules": {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
                      "reuse_policy": {}},
            "mode": "guard_only", "allocation": TWO_SEG}
    r = client.post("/api/plan", json=body).json()
    assert r["feasible"]
    wins = [(w["low_mhz"], w["high_mhz"]) for w in r["allocation"]["free_windows"]]
    # 排除窗把 S1 切成 [90,98] 与 [102,110]
    assert (90.0, 98.0) in wins and (102.0, 110.0) in wins and (130.0, 150.0) in wins
    for a in r["assignments"]:
        assert any(lo <= a["low_mhz"] and a["high_mhz"] <= hi for lo, hi in wins)
        assert a["segment_reason"]
    # post-check：无跨洞占用
    assert r["post_check"]["allocation_check"]["all_inside"]
    assert r["post_check"]["counts"]["error"] == 0
    by_name = {a["name"]: a for a in r["assignments"]}
    assert by_name["H1"]["segment_label"] == "S2"
    assert by_name["L1"]["segment_label"] == "S1"


def test_plan_infeasible_carrier_too_wide(client):
    body = {"carriers": [{"name": "W", "center_mhz": 100, "bandwidth_mhz": 25,
                          "power_dbm": 20, "polarization": "H", "mask_name": "strict"}],
            "rules": {}, "mode": "guard_only", "allocation": TWO_SEG}
    r = client.post("/api/plan", json=body).json()
    assert not r["feasible"]
    assert r["infeasible_carriers"][0]["name"] == "W"
    assert "大于任一可用段" in r["infeasible_carriers"][0]["reason"]


def test_analyze_with_allocation_flags_out_of_band(client):
    body = {"carriers": [{"name": "X", "center_mhz": 100, "bandwidth_mhz": 4,
                          "power_dbm": 20, "polarization": "H", "mask_name": "strict"}],
            "rules": {}, "allocation": TWO_SEG}
    r = client.post("/api/analyze", json=body).json()
    oob = [f for f in r["findings"] if f["type"] == "out_of_band"]
    assert len(oob) == 1 and "排除窗" in oob[0]["fit_reason"]
    assert r["allocation"]["free_windows"]


def test_scenario_without_allocation_migrated(client):
    sc = _mk_scenario(client, "迁移场景")  # 未提交 allocation
    assert sc["allocation"]["version"] == 1
    assert sc["allocation"]["segments"] == [
        {"low_mhz": 90.0, "high_mhz": 150.0, "label": "S1"}]
    assert sc["allocation"]["exclusions"] == []
    # 刷新后仍可追溯
    again = client.get(f"/api/scenarios/{sc['id']}")
    assert again.json()["allocation"]["version"] == 1


def test_allocation_versioning_and_plan_stale(client):
    sc = _mk_scenario(client, "版本场景", allocation=TWO_SEG)
    sid = sc["id"]
    assert sc["allocation"]["version"] == 1

    # 求解并冻结保存规划记录
    plan_body = {"carriers": CARRIERS_2SEG, "rules": {}, "mode": "guard_only",
                 "allocation": TWO_SEG}
    pr = client.post("/api/plan", json=plan_body).json()
    assert pr["feasible"]
    save = client.post(f"/api/scenarios/{sid}/plans",
                       json={"mode": "guard_only", "assignments": pr["assignments"]})
    assert save.status_code == 200, save.text
    rec = save.json()
    assert rec["scheme_version"] == 1 and not rec["stale"]

    # 编辑分配方案：S2 收缩到 [130,140]，使落在高段的载波越界 -> 版本 +1
    new_alloc = {"segments": [{"low_mhz": 90, "high_mhz": 110, "label": "S1"},
                              {"low_mhz": 130, "high_mhz": 140, "label": "S2"}],
                 "exclusions": []}
    r2 = client.put(f"/api/scenarios/{sid}/allocation", json=new_alloc)
    assert r2.status_code == 200 and r2.json()["version"] == 2

    plans = client.get(f"/api/scenarios/{sid}/plans").json()
    assert len(plans) == 1
    p0 = plans[0]
    assert p0["scheme_version"] == 1 and p0["current_version"] == 2
    assert p0["stale"]
    assert any("v1" in m and "v2" in m for m in p0["stale_reasons"])
    assert any("越界" in m for m in p0["stale_reasons"])
    # 旧规划不被偷偷移动：冻结的 assignments 与求解结果一致
    assert p0["assignments"] == pr["assignments"]
    # 历史结论（保存时的 post-check）仍可追溯
    assert p0["post_check"]["allocation_check"]["all_inside"]

    # 版本历史完整
    versions = client.get(f"/api/scenarios/{sid}/allocation/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["segments"][1]["high_mhz"] == 150.0

    # 旧规划相对新方案越界 -> 拒绝再次保存（不部分保存）
    again = client.post(f"/api/scenarios/{sid}/plans",
                        json={"mode": "guard_only", "assignments": pr["assignments"]})
    assert again.status_code == 400


def test_save_plan_rejects_assignments_outside_current_scheme(client):
    sc = _mk_scenario(client, "复核场景", allocation=TWO_SEG)
    sid = sc["id"]
    bad = [{"name": "Z", "center_mhz": 120.0, "low_mhz": 118.0, "high_mhz": 122.0,
            "bandwidth_mhz": 4, "power_dbm": 20, "polarization": "H",
            "mask_name": "strict"}]
    r = client.post(f"/api/scenarios/{sid}/plans",
                    json={"mode": "guard_only", "assignments": bad})
    assert r.status_code == 400
    assert client.get(f"/api/scenarios/{sid}/plans").json() == []


def test_export_import_roundtrip_preserves_history(client):
    sc = _mk_scenario(client, "导出场景", allocation=TWO_SEG)
    sid = sc["id"]
    pr = client.post("/api/plan", json={"carriers": CARRIERS_2SEG, "rules": {},
                                        "mode": "guard_only",
                                        "allocation": TWO_SEG}).json()
    client.post(f"/api/scenarios/{sid}/plans",
                json={"mode": "guard_only", "assignments": pr["assignments"]})
    # 改一次方案 -> v2，旧规划变过期
    new_alloc = {"segments": [{"low_mhz": 90, "high_mhz": 110, "label": "S1"},
                              {"low_mhz": 130, "high_mhz": 140, "label": "S2"}],
                 "exclusions": []}
    client.put(f"/api/scenarios/{sid}/allocation", json=new_alloc)

    exported = client.get(f"/api/scenarios/{sid}/export").json()
    assert exported["format"] == "spectrum-workbench/scenario"
    assert len(exported["allocations"]) == 2
    assert len(exported["plans"]) == 1

    # 导入（改名）：边界、版本、历史结论完整恢复
    exported["name"] = "导入场景"
    imp = client.post("/api/scenarios/import", json=exported)
    assert imp.status_code == 200, imp.text
    new_id = imp.json()["id"]
    assert imp.json()["allocation"]["version"] == 2
    assert imp.json()["allocation"]["segments"][1]["high_mhz"] == 140.0
    versions = client.get(f"/api/scenarios/{new_id}/allocation/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    plans = client.get(f"/api/scenarios/{new_id}/plans").json()
    assert len(plans) == 1 and plans[0]["stale"]
    assert plans[0]["scheme_version"] == 1
    assert plans[0]["post_check"] == exported["plans"][0]["post_check"]


def test_import_conflict_and_invalid_are_atomic(client):
    before = len(client.get("/api/scenarios").json())
    sc = _mk_scenario(client, "冲突场景", allocation=TWO_SEG)
    exported = client.get(f"/api/scenarios/{sc['id']}/export").json()

    # 名称冲突 -> 409，不部分保存
    r = client.post("/api/scenarios/import", json=exported)
    assert r.status_code == 409
    # 区间不完整（上界<=下界）-> 400/422，不部分保存
    exported["name"] = "坏区间场景"
    exported["allocations"] = [{"version": 1, "note": "", "created_at": None,
                                "segments": [{"low_mhz": 100, "high_mhz": 90,
                                              "label": "bad"}],
                                "exclusions": []}]
    r2 = client.post("/api/scenarios/import", json=exported)
    assert r2.status_code in (400, 422)
    # 段重叠 -> 400/422，不部分保存
    exported["allocations"] = [{"version": 1, "note": "", "created_at": None,
                                "segments": [{"low_mhz": 90, "high_mhz": 105,
                                              "label": "a"},
                                             {"low_mhz": 100, "high_mhz": 120,
                                              "label": "b"}],
                                "exclusions": []}]
    r3 = client.post("/api/scenarios/import", json=exported)
    assert r3.status_code in (400, 422)
    # 只有“冲突场景”一个新增，坏导入没有留下任何残留
    names = [s["name"] for s in client.get("/api/scenarios").json()]
    assert len(names) == before + 1
    assert "坏区间场景" not in names


def test_old_format_import_migrates_single_band(client):
    # 旧格式：没有 allocations 字段，只有 band_low/high
    old = {"name": "旧格式场景", "band_low_mhz": 85, "band_high_mhz": 125,
           "guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
           "reuse_policy": {}, "carriers": list(CARRIERS_2SEG[:1])}
    r = client.post("/api/scenarios/import", json=old)
    assert r.status_code == 200, r.text
    alloc = r.json()["allocation"]
    assert alloc["version"] == 1
    assert alloc["segments"] == [{"low_mhz": 85.0, "high_mhz": 125.0, "label": "S1"}]
