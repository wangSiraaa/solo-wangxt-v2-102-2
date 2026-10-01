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
