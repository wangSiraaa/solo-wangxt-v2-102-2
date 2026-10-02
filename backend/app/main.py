"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from . import plan_records
from .assemble import (bands_view, build_spectrum, to_allocation, to_domain,
                       to_rules, validate_masks)
from .config import CORS_ORIGINS, DATABASE_URL
from .db import Base, CarrierRow, MaskRow, PlanSnapshotRow, Scenario
from .schemas import (AllocationOut, AnalyzeRequest, MaskOut, PlanRequest,
                      ScenarioIn, ScenarioOut, ScenarioSummary)
from .seed import seed
from .services import allocation as alloc_mod
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, plan

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="2.0.0",
    description="载波频带冲突检查、掩模尾部泄漏、线性域功率汇总、多段可用频段/"
                "排除窗的版本化频谱分配与 OR-Tools 频率规划。"
                "不连接无线电设备，不生成发射指令。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


# 轻量 schema 兼容：为旧库补齐新列（教学项目无独立迁移工具）。
def _ensure_columns(eng) -> None:
    insp = inspect(eng)
    if "scenarios" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("scenarios")}
    with eng.begin() as conn:
        if "allocation" not in have:
            conn.execute(text("ALTER TABLE scenarios ADD COLUMN allocation JSON"))


@app.on_event("startup")
def _startup() -> None:
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    seed(engine)


# ---- 计算接口（无状态，数据由前端提交） -----------------------------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        validate_masks(req.carriers)
        allocation = to_allocation(req.allocation)
        carriers = [to_domain(c) for c in req.carriers]
        result = analyze(carriers, to_rules(req.rules))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["bands"] = bands_view(carriers, allocation)
    result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
    if allocation is not None:
        result["allocation"] = alloc_mod.allocation_view(allocation)
    return result


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        req.validate_band()
        validate_masks(req.carriers)
        allocation = to_allocation(req.allocation, req.band_low_mhz, req.band_high_mhz)
        carriers = [to_domain(c) for c in req.carriers]
        rules = to_rules(req.rules)
        result = plan(carriers, rules, allocation, mode=req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["allocation"] = alloc_mod.allocation_view(allocation)
    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        result["post_check"] = analyze(planned, rules)
        # 规划后的频段视图与发射谱（用于前端叠加对照），并带归属标注
        result["bands"] = bands_view(planned, allocation)
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


def _stored_allocation(sc: Scenario) -> alloc_mod.Allocation:
    """读取场景的分配方案；旧格式（无 allocation 列数据）即时迁移为等价单段。"""
    if sc.allocation:
        return alloc_mod.allocation_from_payload(sc.allocation)
    return alloc_mod.allocation_from_band(sc.band_low_mhz, sc.band_high_mhz)


def _allocation_out(alloc: alloc_mod.Allocation) -> AllocationOut:
    view = alloc_mod.allocation_view(alloc)
    return AllocationOut(**view)


def _row_to_out(sc: Scenario) -> ScenarioOut:
    migrated = sc.allocation is None
    alloc = _stored_allocation(sc)
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        # 旧字段填充为分配方案的总体外边界，兼容旧客户端
        band_low_mhz=alloc.segments[0].low_mhz,
        band_high_mhz=alloc.segments[-1].high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        allocation=_allocation_out(alloc),
        allocation_migrated=migrated,
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
    )


def _build_candidate_allocation(req: ScenarioIn,
                                existing: Scenario | None,
                                preserve_client_version: bool = False
                                ) -> alloc_mod.Allocation:
    """构造待保存的分配方案；更新时按内容指纹决定版本是否递增。

    preserve_client_version=True（导入历史场景）时，内容变化也保留客户端给的
    显式版本号；普通更新则由服务端按内容变化权威 +1，旧规划据此标过期。
    """
    candidate = to_allocation(req.allocation, req.band_low_mhz, req.band_high_mhz)
    if existing is None:
        return candidate
    old = _stored_allocation(existing)
    if alloc_mod.allocation_fingerprint(candidate) == alloc_mod.allocation_fingerprint(old):
        # 内容未变：保留原版本（即便客户端回传了旧版本号）
        return alloc_mod.Allocation(
            segments=candidate.segments, exclusions=candidate.exclusions,
            version=old.version, name=candidate.name)
    if preserve_client_version:
        return candidate
    # 内容变化：版本权威 +1——旧规划据此被标为过期，绝不被偷偷移动
    return alloc_mod.Allocation(
        segments=candidate.segments, exclusions=candidate.exclusions,
        version=old.version + 1, name=candidate.name)


def _carrier_rows(req: ScenarioIn) -> list[CarrierRow]:
    return [
        CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                   bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                   polarization=c.polarization, mask_name=c.mask_name)
        for i, c in enumerate(req.carriers)
    ]


@app.get("/api/scenarios", response_model=list[ScenarioSummary])
def list_scenarios() -> list[ScenarioSummary]:
    with Session(engine) as s:
        rows = list(s.scalars(select(Scenario).order_by(Scenario.id)))
        out = []
        for r in rows:
            alloc = _stored_allocation(r)
            out.append(ScenarioSummary(
                id=r.id, name=r.name, description=r.description,
                carrier_count=len(r.carriers),
                created_at=r.created_at.isoformat() if r.created_at else None,
                allocation_version=alloc.version, has_plans=bool(r.plans)))
        return out


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
        allocation = _build_candidate_allocation(req, None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=allocation.segments[0].low_mhz,
            band_high_mhz=allocation.segments[-1].high_mhz,
            guard_required_mhz=req.guard_required_mhz,
            leakage_limit_dbm=req.leakage_limit_dbm,
            reuse_policy=dict(req.reuse_policy),
            allocation=allocation.to_dict(),
            carriers=_carrier_rows(req),
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
        try:
            # 先完成全部校验再写库：分配方案非法时不得部分保存
            allocation = _build_candidate_allocation(req, sc)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        sc.name = req.name.strip()
        sc.description = req.description
        sc.band_low_mhz = allocation.segments[0].low_mhz
        sc.band_high_mhz = allocation.segments[-1].high_mhz
        sc.guard_required_mhz = req.guard_required_mhz
        sc.leakage_limit_dbm = req.leakage_limit_dbm
        sc.reuse_policy = dict(req.reuse_policy)
        sc.allocation = allocation.to_dict()
        sc.carriers = _carrier_rows(req)
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


# ---- 规划方案快照（不可变历史 + 版本/post-check 过期标记） -----------------

def _current_domain(sc: Scenario):
    carriers = [
        Carrier(id=c.id, name=c.name, center_mhz=c.center_mhz,
                bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                polarization=c.polarization, mask_name=c.mask_name)
        for c in sorted(sc.carriers, key=lambda c: c.position)]
    rules = _rules_from_scenario(sc)
    return carriers, rules, _stored_allocation(sc)


def _rules_from_scenario(sc: Scenario):
    from .services.analysis import AnalysisRules
    return AnalysisRules(
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=dict(sc.reuse_policy or {}))


def _refresh_snapshot(s: Session, sc: Scenario, row: PlanSnapshotRow,
                      carriers, rules, allocation, carriers_fp, rules_fp) -> dict:
    verdict = plan_records.evaluate_staleness(
        row.result, row.allocation_version, allocation, rules, carriers,
        row.carriers_fingerprint, row.rules_fingerprint,
        carriers_fp, rules_fp, row.baseline_counts)
    row.last_stale = verdict["stale"]
    row.last_stale_reasons = verdict["reasons"]
    row.last_checked_at = datetime.now(timezone.utc)
    return verdict


def _snapshot_payload(row: PlanSnapshotRow, verdict: dict | None) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "mode": row.mode,
        "allocation_version": row.allocation_version,
        "allocation_fingerprint": row.allocation_fingerprint,
        "stale": verdict["stale"] if verdict else row.last_stale,
        "stale_reasons": verdict["reasons"] if verdict else (row.last_stale_reasons or []),
        "boundary_violations": verdict["boundary_violations"] if verdict else [],
        "version_ok": verdict["version_ok"] if verdict else None,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "baseline_counts": row.baseline_counts,
        "post_check": verdict["post_check"] if verdict else None,
        "note": row.note or "",
        "result": row.result,
    }


class PlanRunIn(BaseModel):
    mode: str = "guard_only"
    note: str = ""


@app.post("/api/scenarios/{scenario_id}/plans")
def create_plan_snapshot(scenario_id: int, req: PlanRunIn) -> dict:
    """按场景当前数据跑一次规划并保存为不可变快照。"""
    if req.mode not in ("guard_only", "mask_aware"):
        raise HTTPException(400, "mode 只能是 guard_only / mask_aware")
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        carriers, rules, allocation = _current_domain(sc)
        result = plan(carriers, rules, allocation, mode=req.mode)
        if not result["feasible"]:
            # 不可行不保存快照，但返回完整原因（哪条载波放不下、跨了哪个排除窗）
            return {"saved": False, **result}
        planned = plan_records.assignments_to_carriers(result)
        post = analyze(planned, rules)
        stored_result = {k: v for k, v in result.items() if k != "spectrum"}
        row = PlanSnapshotRow(
            scenario_id=sc.id, mode=req.mode,
            allocation_version=allocation.version,
            allocation_fingerprint=alloc_mod.allocation_fingerprint(allocation),
            carriers_fingerprint=plan_records.carriers_fingerprint(carriers),
            rules_fingerprint=plan_records.rules_fingerprint(rules),
            result=stored_result,
            baseline_counts=dict(post["counts"]), note=req.note or "")
        s.add(row)
        s.commit()
        s.refresh(row)
        verdict = _refresh_snapshot(
            s, sc, row, carriers, rules, allocation,
            row.carriers_fingerprint, row.rules_fingerprint)
        s.commit()
        return {"saved": True, "plan": _snapshot_payload(row, verdict)}


@app.get("/api/scenarios/{scenario_id}/plans")
def list_plan_snapshots(scenario_id: int, refresh: bool = True) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        carriers, rules, allocation = _current_domain(sc)
        cfp = plan_records.carriers_fingerprint(carriers)
        rfp = plan_records.rules_fingerprint(rules)
        rows = list(s.scalars(
            select(PlanSnapshotRow).where(PlanSnapshotRow.scenario_id == sc.id)
            .order_by(PlanSnapshotRow.id)))
        out = []
        changed = False
        for row in rows:
            if refresh:
                _refresh_snapshot(s, sc, row, carriers, rules, allocation, cfp, rfp)
                changed = True
            out.append({
                "id": row.id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "mode": row.mode,
                "allocation_version": row.allocation_version,
                "current_allocation_version": allocation.version,
                "stale": row.last_stale,
                "stale_reasons": row.last_stale_reasons or [],
                "last_checked_at": row.last_checked_at.isoformat()
                if row.last_checked_at else None,
                "baseline_counts": row.baseline_counts,
                "note": row.note or "",
                "objective_khz": row.result.get("objective_khz"),
            })
        if changed:
            s.commit()
        return {"current_allocation_version": allocation.version, "plans": out}


@app.get("/api/scenarios/{scenario_id}/plans/{plan_id}")
def get_plan_snapshot(scenario_id: int, plan_id: int, refresh: bool = True) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        row = s.get(PlanSnapshotRow, plan_id)
        if row is None or row.scenario_id != sc.id:
            raise HTTPException(404, "规划方案不存在")
        carriers, rules, allocation = _current_domain(sc)
        verdict = None
        if refresh:
            verdict = _refresh_snapshot(
                s, sc, row, carriers, rules, allocation,
                plan_records.carriers_fingerprint(carriers),
                plan_records.rules_fingerprint(rules))
            s.commit()
        return _snapshot_payload(row, verdict)


@app.delete("/api/scenarios/{scenario_id}/plans/{plan_id}")
def delete_plan_snapshot(scenario_id: int, plan_id: int) -> dict:
    with Session(engine) as s:
        row = s.get(PlanSnapshotRow, plan_id)
        if row is None or row.scenario_id != scenario_id:
            raise HTTPException(404, "规划方案不存在")
        s.delete(row)
        s.commit()
        return {"deleted": plan_id}


# ---- 导出 / 导入（原子：校验失败不写入任何内容） ---------------------------

@app.get("/api/scenarios/{scenario_id}/export")
def export_scenario(scenario_id: int) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        out = _row_to_out(sc)
        allocation = _stored_allocation(sc)
        plans = []
        for row in sorted(sc.plans, key=lambda r: r.id):
            plans.append({
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "mode": row.mode,
                "allocation_version": row.allocation_version,
                "allocation_fingerprint": row.allocation_fingerprint,
                "carriers_fingerprint": row.carriers_fingerprint,
                "rules_fingerprint": row.rules_fingerprint,
                "baseline_counts": row.baseline_counts,
                "note": row.note or "",
                "result": row.result,
            })
        return {
            "format": "spectrum-workbench-scenario",
            "schema_version": 2,
            "scenario": {
                "name": sc.name, "description": sc.description or "",
                "guard_required_mhz": sc.guard_required_mhz,
                "leakage_limit_dbm": sc.leakage_limit_dbm,
                "reuse_policy": sc.reuse_policy or {},
                "allocation": allocation.to_dict(),
                "carriers": [
                    {"name": c.name, "center_mhz": c.center_mhz,
                     "bandwidth_mhz": c.bandwidth_mhz, "power_dbm": c.power_dbm,
                     "polarization": c.polarization, "mask_name": c.mask_name}
                    for c in sorted(sc.carriers, key=lambda c: c.position)],
            },
            "plans": plans,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }


class ImportIn(BaseModel):
    format: str = "spectrum-workbench-scenario"
    schema_version: int = 2
    scenario: dict
    plans: list[dict] = []


@app.post("/api/scenarios/import")
def import_scenario(doc: ImportIn) -> dict:
    if doc.format != "spectrum-workbench-scenario":
        raise HTTPException(400, "无法识别的导入格式")
    try:
        req = ScenarioIn(**doc.scenario)
        validate_masks(req.carriers)
        allocation = _build_candidate_allocation(req, None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"导入内容无效：{e}")
    except Exception as e:  # pydantic ValidationError 等
        raise HTTPException(status_code=400, detail=f"导入内容不完整或格式错误：{e}")

    # 规划快照的轻量校验（结构完整才允许导入），任何一条失败都整体拒绝
    normalized_plans = []
    for p in doc.plans:
        try:
            result = p["result"]
            assignments = result["assignments"]
            assert isinstance(assignments, list) and assignments
            assert result.get("feasible") is True
            for a in assignments:
                for k in ("name", "center_mhz", "bandwidth_mhz", "power_dbm",
                          "polarization", "mask_name", "segment_index"):
                    assert k in a, f"规划缺少字段 {k}"
            normalized_plans.append({
                "mode": p.get("mode", result.get("mode", "guard_only")),
                "allocation_version": int(p.get("allocation_version", allocation.version)),
                "allocation_fingerprint": str(p.get(
                    "allocation_fingerprint",
                    alloc_mod.allocation_fingerprint(allocation))),
                "carriers_fingerprint": str(p["carriers_fingerprint"]),
                "rules_fingerprint": str(p["rules_fingerprint"]),
                "baseline_counts": dict(p["baseline_counts"]),
                "note": str(p.get("note") or ""),
                "result": result,
                "created_at": p.get("created_at"),
            })
        except (KeyError, AssertionError, TypeError, ValueError) as e:
            raise HTTPException(400, f"规划快照不完整或与场景不一致，已拒绝整份导入：{e}")

    with Session(engine) as s:
        # 名称冲突 -> 409，且此前未做任何写入（不部分保存）
        if s.scalar(select(Scenario).where(Scenario.name == req.name.strip())) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在，导入已取消（未写入任何内容）")
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=allocation.segments[0].low_mhz,
            band_high_mhz=allocation.segments[-1].high_mhz,
            guard_required_mhz=req.guard_required_mhz,
            leakage_limit_dbm=req.leakage_limit_dbm,
            reuse_policy=dict(req.reuse_policy),
            allocation=allocation.to_dict(),
            carriers=_carrier_rows(req),
        )
        s.add(sc)
        s.flush()  # 拿到 sc.id；若后续失败统一 rollback，不产生半成品
        for p in normalized_plans:
            created = None
            if p["created_at"]:
                try:
                    created = datetime.fromisoformat(p["created_at"])
                except ValueError:
                    created = None
            s.add(PlanSnapshotRow(
                scenario_id=sc.id, mode=p["mode"],
                allocation_version=p["allocation_version"],
                allocation_fingerprint=p["allocation_fingerprint"],
                carriers_fingerprint=p["carriers_fingerprint"],
                rules_fingerprint=p["rules_fingerprint"],
                result=p["result"], baseline_counts=p["baseline_counts"],
                note=p["note"], created_at=created or datetime.now(timezone.utc)))
        s.commit()
        s.refresh(sc)
        imported = _row_to_out(sc)
        return {"imported": imported.model_dump(), "plans_imported": len(normalized_plans)}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
