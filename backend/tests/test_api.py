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


# ---- 频谱分配方案：迁移 / 多段 / 不可行 / 版本 / 快照过期 / 导出导入 -------

def test_legacy_scenario_migrates_to_single_segment(client):
    """旧格式场景（只有 band_low/high）读取时等价迁移为单段分配方案。"""
    sc = client.get("/api/scenarios/1").json()
    assert sc["allocation"]["version"] == 1
    assert len(sc["allocation"]["segments"]) == 1
    assert sc["allocation"]["segments"][0]["low_mhz"] == 80
    assert sc["allocation"]["segments"][0]["high_mhz"] == 220
    # 旧字段仍填充为总体外边界
    assert (sc["band_low_mhz"], sc["band_high_mhz"]) == (80, 220)


def _create_two_segment_scenario(client, name="两段场景"):
    payload = {
        "name": name, "description": "d",
        "guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
        "reuse_policy": {},
        "allocation": {
            "name": "两段", "version": 1,
            "segments": [{"low_mhz": 90, "high_mhz": 100},
                         {"low_mhz": 110, "high_mhz": 120}],
            "exclusions": [],
        },
        "carriers": [
            {"name": "A", "center_mhz": 92, "bandwidth_mhz": 4, "power_dbm": 20,
             "polarization": "H", "mask_name": "strict"},
            {"name": "B", "center_mhz": 112, "bandwidth_mhz": 4, "power_dbm": 20,
             "polarization": "H", "mask_name": "strict"},
        ],
    }
    r = client.post("/api/scenarios", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"], payload


def test_legacy_plan_request_without_allocation_still_works(client):
    """旧客户端不带 allocation、只给 band_low/high 的规划调用仍兼容。"""
    sc = client.get("/api/scenarios/1").json()
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
                      "reuse_policy": sc["reuse_policy"]},
            "band_low_mhz": 80, "band_high_mhz": 300, "mode": "mask_aware"}
    r = client.post("/api/plan", json=body).json()
    assert r["feasible"]
    assert r["allocation"]["segments"][0]["low_mhz"] == 80


def test_true_legacy_row_with_null_allocation_migrates(client):
    """数据库里 allocation=NULL 的旧场景读取时迁移为等价单段并显式标注。"""
    from app.db import Scenario as ScenarioModel
    with Session(main.engine) as s:
        s.add(ScenarioModel(name="老场景", description="legacy", band_low_mhz=70,
                            band_high_mhz=180, guard_required_mhz=1.0,
                            leakage_limit_dbm=-45.0, reuse_policy={}, allocation=None))
        s.commit()
    sid = max(x["id"] for x in client.get("/api/scenarios").json())
    sc = client.get(f"/api/scenarios/{sid}").json()
    assert sc["allocation_migrated"] is True
    seg = sc["allocation"]["segments"]
    assert len(seg) == 1 and (seg[0]["low_mhz"], seg[0]["high_mhz"]) == (70, 180)
    # 保存一次后（不改分配）版本仍为 1，且不再标迁移（已显式落库）
    payload = {
        "name": "老场景", "description": "legacy",
        "band_low_mhz": 70, "band_high_mhz": 180,
        "guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
        "reuse_policy": {},
        "allocation": {"name": "默认", "segments": [{"low_mhz": 70, "high_mhz": 180}]},
        "carriers": [],
    }
    r = client.put(f"/api/scenarios/{sid}", json=payload)
    assert r.status_code == 200
    out = r.json()
    assert out["allocation_migrated"] is False
    assert out["allocation"]["version"] == 1


def test_two_segment_plan_endpoint_assignment_and_chart_data(client):
    """验收 1（API 层）：两段不连续频谱下规划分段落位，返回分配视图供画图。"""
    sid, payload = _create_two_segment_scenario(client)
    body = {"carriers": payload["carriers"], "rules": {"guard_required_mhz": 1.0},
            "allocation": payload["allocation"], "mode": "guard_only"}
    r = client.post("/api/plan", json=body).json()
    assert r["feasible"]
    by = {a["name"]: a for a in r["assignments"]}
    assert by["A"]["segment_index"] == 0 and by["B"]["segment_index"] == 1
    # 图表数据：频段边界 / 排除区 / 有效小片 / 每条载波的归属
    view = r["allocation"]
    assert [s["low_mhz"] for s in view["segments"]] == [90, 110]
    assert {p["index"] for p in view["pieces"]} == {0, 1}
    for b in r["bands"]:
        assert b["allocation_status"] == "inside"
    # 无任何占用跨越 100–110 的段间空洞
    for a in r["assignments"]:
        assert not (a["low_mhz"] < 100 and a["high_mhz"] > 110)
    # post_check 零冲突
    assert r["post_check"]["counts"]["error"] == 0


def test_plan_endpoint_wide_carrier_infeasible(client):
    """验收 2（API 层）：超宽载波明确不可行，不保存任何东西。"""
    allocation = {"segments": [{"low_mhz": 90, "high_mhz": 100},
                               {"low_mhz": 110, "high_mhz": 118}]}
    body = {"carriers": [{"name": "W", "center_mhz": 95, "bandwidth_mhz": 12,
                          "power_dbm": 20, "polarization": "H", "mask_name": "strict"}],
            "rules": {"guard_required_mhz": 1.0},
            "allocation": allocation, "mode": "guard_only"}
    r = client.post("/api/plan", json=body).json()
    assert r["feasible"] is False
    assert r["infeasible_reasons"][0]["reason"] == "bandwidth_exceeds_every_piece"


def test_bad_allocation_rejected_entirely(client):
    """导入/保存冲突或不完整区间时返回 400，不产生半成品场景。"""
    # 段重叠
    bad = {"name": "坏段场景",
           "allocation": {"segments": [{"low_mhz": 90, "high_mhz": 100},
                                       {"low_mhz": 99, "high_mhz": 110}]},
           "carriers": []}
    assert client.post("/api/scenarios", json=bad).status_code == 400
    # 排除窗悬空
    dangling = {"name": "悬空窗场景",
                "allocation": {"segments": [{"low_mhz": 90, "high_mhz": 100}],
                               "exclusions": [{"low_mhz": 150, "high_mhz": 160}]},
                "carriers": []}
    assert client.post("/api/scenarios", json=dangling).status_code == 400
    # 确认没有任何场景被部分保存
    names = {s["name"] for s in client.get("/api/scenarios").json()}
    assert "坏段场景" not in names and "悬空窗场景" not in names


def test_version_bumps_on_content_change_only(client):
    sid, payload = _create_two_segment_scenario(client, "版本场景")
    assert client.get(f"/api/scenarios/{sid}").json()["allocation"]["version"] == 1
    # 内容不变（即便回传版本号缺省）-> 版本保留
    same = dict(payload, name="版本场景")
    r = client.put(f"/api/scenarios/{sid}", json=same)
    assert r.status_code == 200
    assert r.json()["allocation"]["version"] == 1
    # 编辑一个频段的边界 -> 版本 +1
    changed = dict(payload, name="版本场景")
    changed["allocation"] = {
        "segments": [{"low_mhz": 90, "high_mhz": 99},   # 段一上界 100 -> 99
                     {"low_mhz": 110, "high_mhz": 120}],
        "exclusions": []}
    r = client.put(f"/api/scenarios/{sid}", json=changed)
    assert r.json()["allocation"]["version"] == 2


def test_saved_plan_goes_stale_after_band_edit_and_is_never_moved(client):
    """验收 4：编辑频段使旧计划越界 -> 计划标为过期，但频率位置原封不动。"""
    sid, payload = _create_two_segment_scenario(client, "过期场景")
    # 保存一次规划快照
    r = client.post(f"/api/scenarios/{sid}/plans", json={"mode": "guard_only"})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["saved"] is True
    pid = saved["plan"]["id"]
    orig_centers = {a["name"]: a["center_mhz"]
                    for a in saved["plan"]["result"]["assignments"]}
    assert saved["plan"]["stale"] is False

    # 把段一缩到 90–91 MHz：A（中心 92，半宽 2）必然越界
    changed = dict(payload, name="过期场景")
    changed["allocation"] = {"segments": [{"low_mhz": 90, "high_mhz": 91},
                                          {"low_mhz": 110, "high_mhz": 120}]}
    assert client.put(f"/api/scenarios/{sid}", json=changed).status_code == 200

    # 刷新：旧规划过期，且给出越界载波；但结果里的中心频率没有被偷偷移动
    listing = client.get(f"/api/scenarios/{sid}/plans").json()
    entry = listing["plans"][0]
    assert entry["stale"] is True
    assert entry["allocation_version"] == 1
    assert entry["current_allocation_version"] == 2

    detail = client.get(f"/api/scenarios/{sid}/plans/{pid}").json()
    assert detail["stale"] is True
    assert detail["version_ok"] is False
    viol_names = {v["carrier"] for v in detail["boundary_violations"]}
    assert "A" in viol_names
    new_centers = {a["name"]: a["center_mhz"]
                   for a in detail["result"]["assignments"]}
    assert new_centers == orig_centers  # 旧规划位置不可变、未被移动
    # 历史结论（生成时计数）仍可追溯
    assert detail["baseline_counts"]["error"] == 0


def test_export_import_roundtrip_with_allocation_and_history(client):
    """验收 4：导出/导入后多段边界、版本与规划历史可追溯；脏数据整体拒绝。"""
    sid, payload = _create_two_segment_scenario(client, "导出场景")
    client.post(f"/api/scenarios/{sid}/plans", json={"mode": "guard_only"})
    doc = client.get(f"/api/scenarios/{sid}/export").json()
    assert doc["schema_version"] == 2
    assert len(doc["plans"]) == 1
    doc["scenario"]["name"] = "导入回读场景"
    r = client.post("/api/scenarios/import", json=doc)
    assert r.status_code == 200, r.text
    assert r.json()["plans_imported"] == 1
    # 回读：边界/版本保留
    imported = client.get("/api/scenarios").json()
    new_id = next(s["id"] for s in imported if s["name"] == "导入回读场景")
    sc = client.get(f"/api/scenarios/{new_id}").json()
    assert [seg["high_mhz"] for seg in sc["allocation"]["segments"]] == [100, 120]
    assert sc["allocation"]["version"] == 1
    plans = client.get(f"/api/scenarios/{new_id}/plans").json()
    assert len(plans["plans"]) == 1
    assert plans["plans"][0]["allocation_version"] == 1

    # 脏导入：规划快照缺字段 -> 400，且整份不保存
    bad = {**doc, "scenario": {**doc["scenario"], "name": "脏导入场景"}}
    del bad["plans"][0]["result"]["assignments"][0]["center_mhz"]
    r = client.post("/api/scenarios/import", json=bad)
    assert r.status_code == 400
    names = {s["name"] for s in client.get("/api/scenarios").json()}
    assert "脏导入场景" not in names

    # 脏导入：分配区间段倒置 -> 400
    bad2 = {"format": "spectrum-workbench-scenario", "schema_version": 2,
            "scenario": {**doc["scenario"], "name": "坏区间导入",
                         "allocation": {"segments": [{"low_mhz": 100, "high_mhz": 90}]}},
            "plans": []}
    assert client.post("/api/scenarios/import", json=bad2).status_code == 400


def test_saved_plan_goes_stale_when_rules_change_without_band_edit(client):
    """不改频段、只收紧规则，旧规划也应经 post-check 标为过期。"""
    sid, payload = _create_two_segment_scenario(client, "规则过期场景")
    client.post(f"/api/scenarios/{sid}/plans", json={"mode": "guard_only"})
    # 收紧保护间隔 1 -> 8 MHz（段一净宽 10，放不下两个 4 MHz 载波 + 8 MHz 间隔）
    changed = dict(payload, name="规则过期场景", guard_required_mhz=8.0)
    changed["allocation"] = {
        "segments": [{"low_mhz": 90, "high_mhz": 100}, {"low_mhz": 110, "high_mhz": 120}]}
    client.put(f"/api/scenarios/{sid}", json=changed)
    listing = client.get(f"/api/scenarios/{sid}/plans").json()
    assert listing["plans"][0]["stale"] is True
    reasons = " ".join(listing["plans"][0]["stale_reasons"])
    assert "规则" in reasons


def test_stale_verdict_persists_and_refresh_flag(client):
    """过期结论落库：refresh=false 返回上次结论，历史在“刷新”后仍可追溯。"""
    sid, payload = _create_two_segment_scenario(client, "持久化场景")
    r = client.post(f"/api/scenarios/{sid}/plans", json={"mode": "guard_only"})
    pid = r.json()["plan"]["id"]

    # 不刷新：生成时即已复核为有效，库里保存 stale=False
    lst = client.get(f"/api/scenarios/{sid}/plans?refresh=false").json()["plans"][0]
    assert lst["stale"] is False

    # 改频段，不刷新读取 -> 仍为上次结论（False）
    changed = dict(payload, name="持久化场景")
    changed["allocation"] = {"segments": [{"low_mhz": 90, "high_mhz": 91},
                                          {"low_mhz": 110, "high_mhz": 120}]}
    client.put(f"/api/scenarios/{sid}", json=changed)
    lst = client.get(f"/api/scenarios/{sid}/plans?refresh=false").json()["plans"][0]
    assert lst["stale"] is False
    assert lst["current_allocation_version"] == 2

    # 刷新 -> 过期，且不刷新再读保持过期（已落库）
    refreshed = client.get(f"/api/scenarios/{sid}/plans?refresh=true").json()["plans"][0]
    assert refreshed["stale"] is True
    again = client.get(f"/api/scenarios/{sid}/plans?refresh=false").json()["plans"][0]
    assert again["stale"] is True


def test_analyze_with_allocation_annotates_status(client):
    """分析接口在图数据中标注每个录入载波相对分配方案的归属状态。"""
    body = {
        "carriers": [
            {"name": "IN", "center_mhz": 95, "bandwidth_mhz": 4, "power_dbm": 20,
             "polarization": "H", "mask_name": "strict"},
            {"name": "HOLE", "center_mhz": 105, "bandwidth_mhz": 4, "power_dbm": 20,
             "polarization": "H", "mask_name": "strict"},
        ],
        "rules": {"guard_required_mhz": 1.0},
        "allocation": {"segments": [{"low_mhz": 90, "high_mhz": 100},
                                    {"low_mhz": 110, "high_mhz": 120}]},
    }
    r = client.post("/api/analyze", json=body).json()
    status = {b["name"]: b["allocation_status"] for b in r["bands"]}
    assert status["IN"] == "inside"
    assert status["HOLE"] in ("spans_gap", "outside_segments")
    assert len(r["allocation"]["segments"]) == 2
