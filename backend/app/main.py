"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .assemble import bands_view, build_spectrum, to_domain, to_rules, validate_masks
from .config import CORS_ORIGINS, DATABASE_URL
from .db import Base, CarrierRow, MaskRow, Scenario
from .schemas import (AnalyzeRequest, MaskOut, PlanRequest, ScenarioIn,
                      ScenarioOut, ScenarioSummary)
from .seed import seed
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, plan

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="1.0.0",
    description="载波频带冲突检查、掩模尾部泄漏、线性域功率汇总与 OR-Tools 频率规划。"
                "不连接无线电设备，不生成发射指令。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


@app.on_event("startup")
def _startup() -> None:
    Base.metadata.create_all(engine)
    seed(engine)


# ---- 计算接口（无状态，数据由前端提交） -----------------------------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        result = analyze(carriers, to_rules(req.rules))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["bands"] = bands_view(carriers)
    result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
    return result


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        req.validate_band()
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        result = plan(carriers, to_rules(req.rules),
                      BandLimits(req.band_low_mhz, req.band_high_mhz), mode=req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        result["post_check"] = analyze(planned, to_rules(req.rules))
        # 规划后的频段视图与发射谱（用于前端叠加对照）
        result["bands"] = bands_view(planned)
        result["spectrum"] = build_spectrum(planned, 0.05)
    return result


# ---- 掩模 ------------------------------------------------------------------

@app.get("/api/masks", response_model=list[MaskOut])
def list_masks() -> list[MaskRow]:
    with Session(engine) as s:
        return list(s.scalars(select(MaskRow).order_by(MaskRow.name)))


# ---- 场景持久化 ------------------------------------------------------------

def _carrier_out(c: CarrierRow):
    from .schemas import CarrierOut
    return CarrierOut(id=c.id, name=c.name, center_mhz=c.center_mhz,
                      bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                      polarization=c.polarization, mask_name=c.mask_name)


def _row_to_out(sc: Scenario) -> ScenarioOut:
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
    )


@app.get("/api/scenarios", response_model=list[ScenarioSummary])
def list_scenarios() -> list[ScenarioSummary]:
    with Session(engine) as s:
        rows = list(s.scalars(select(Scenario).order_by(Scenario.id)))
        return [ScenarioSummary(id=r.id, name=r.name, description=r.description,
                                carrier_count=len(r.carriers),
                                created_at=r.created_at.isoformat() if r.created_at else None)
                for r in rows]


@app.get("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(scenario_id: int) -> ScenarioOut:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        return _row_to_out(sc)


@app.post("/api/scenarios", response_model=ScenarioOut)
def create_scenario(req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=req.band_low_mhz, band_high_mhz=req.band_high_mhz,
            guard_required_mhz=req.guard_required_mhz,
            leakage_limit_dbm=req.leakage_limit_dbm,
            reuse_policy=dict(req.reuse_policy),
            carriers=[
                CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                           bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                           polarization=c.polarization, mask_name=c.mask_name)
                for i, c in enumerate(req.carriers)
            ],
        )
        s.add(sc)
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        s.delete(sc)
        s.commit()
        return {"deleted": scenario_id}


@app.put("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: int, req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        other = s.scalar(select(Scenario).where(
            Scenario.name == req.name.strip(), Scenario.id != scenario_id))
        if other is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc.name = req.name.strip()
        sc.description = req.description
        sc.band_low_mhz = req.band_low_mhz
        sc.band_high_mhz = req.band_high_mhz
        sc.guard_required_mhz = req.guard_required_mhz
        sc.leakage_limit_dbm = req.leakage_limit_dbm
        sc.reuse_policy = dict(req.reuse_policy)
        sc.carriers = [
            CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                       bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                       polarization=c.polarization, mask_name=c.mask_name)
            for i, c in enumerate(req.carriers)
        ]
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
