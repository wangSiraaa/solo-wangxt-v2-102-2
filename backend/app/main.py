"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .assemble import (bands_view, build_spectrum, to_allocation, to_domain,
                       to_rules, validate_masks)
from .config import CORS_ORIGINS, DATABASE_URL
from .db import (AllocationExclusion, AllocationScheme, AllocationSegment,
                 Base, CarrierRow, MaskRow, PlanRecord, Scenario)
from .schemas import (AllocationIn, AllocationOut, AnalyzeRequest,
                      ExclusionOut, MaskOut, PlanRecordOut, PlanRequest,
                      PlanSaveIn, RulesIn, ScenarioExport, ScenarioImport,
                      ScenarioIn, ScenarioOut, ScenarioSummary, SegmentIn,
                      SegmentOut)
from .seed import seed
from .services.allocation import (Allocation, Exclusion, Segment,
                                  allocation_view, compute_free_windows,
                                  describe_fit, find_window,
                                  validate_allocation)
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, allocation_from_band, plan

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="1.1.0",
    description="载波频带冲突检查、掩模尾部泄漏、线性域功率汇总与 OR-Tools 频率规划；"
                "支持多段可用频段 + 排除窗的版本化频谱分配方案。"
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


# ---- 分配方案 helpers -------------------------------------------------------

def _checked_allocation(a: AllocationIn) -> Allocation:
    """schema -> 领域对象并做整体几何校验；不合格抛 ValueError（-> 400）。"""
    alloc = to_allocation(a)
    validate_allocation(list(alloc.segments), list(alloc.exclusions))
    return alloc


def _scheme_alloc(scheme: AllocationScheme) -> Allocation:
    return Allocation(
        segments=tuple(Segment(s.low_mhz, s.high_mhz, s.label)
                       for s in scheme.segments),
        exclusions=tuple(Exclusion(e.low_mhz, e.high_mhz, e.reason)
                         for e in scheme.exclusions),
    )


def _scheme_out(scheme: AllocationScheme) -> AllocationOut:
    return AllocationOut(
        version=scheme.version, note=scheme.note or "",
        created_at=scheme.created_at.isoformat() if scheme.created_at else None,
        segments=[SegmentOut(low_mhz=s.low_mhz, high_mhz=s.high_mhz, label=s.label)
                  for s in scheme.segments],
        exclusions=[ExclusionOut(low_mhz=e.low_mhz, high_mhz=e.high_mhz,
                                 reason=e.reason) for e in scheme.exclusions],
    )


def _current_scheme(s: Session, sc: Scenario) -> AllocationScheme:
    """场景当前的分配方案（最大版本号）；老场景没有方案时按单一可用范围迁移为 v1。"""
    scheme = s.scalar(select(AllocationScheme)
                      .where(AllocationScheme.scenario_id == sc.id)
                      .order_by(AllocationScheme.version.desc()).limit(1))
    if scheme is None:
        scheme = AllocationScheme(
            scenario_id=sc.id, version=1,
            note="由旧版单一可用范围迁移为等价频段",
            segments=[AllocationSegment(low_mhz=sc.band_low_mhz,
                                        high_mhz=sc.band_high_mhz, label="S1")],
            exclusions=[])
        s.add(scheme)
        s.flush()
    return scheme


def _new_scheme_version(s: Session, sc: Scenario, alloc_in: AllocationIn,
                        note: str = "") -> AllocationScheme:
    """把一组新几何存为下一个版本（旧版本保留，规划记录按版本判过期）。"""
    current = _current_scheme(s, sc)
    scheme = AllocationScheme(scenario_id=sc.id, version=current.version + 1,
                              note=note)
    scheme.segments = [AllocationSegment(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                         label=x.label) for x in alloc_in.segments]
    scheme.exclusions = [AllocationExclusion(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                             reason=x.reason) for x in alloc_in.exclusions]
    s.add(scheme)
    s.flush()
    return scheme


def _same_geometry(scheme: AllocationScheme, alloc_in: AllocationIn) -> bool:
    seg = sorted((round(x.low_mhz, 6), round(x.high_mhz, 6), x.label or "")
                 for x in alloc_in.segments)
    cur_seg = sorted((round(x.low_mhz, 6), round(x.high_mhz, 6), x.label or "")
                     for x in scheme.segments)
    exc = sorted((round(x.low_mhz, 6), round(x.high_mhz, 6), x.reason or "")
                 for x in alloc_in.exclusions)
    cur_exc = sorted((round(x.low_mhz, 6), round(x.high_mhz, 6), x.reason or "")
                     for x in scheme.exclusions)
    return seg == cur_seg and exc == cur_exc


def _allocation_check(assignments: list[dict], alloc: Allocation) -> dict:
    """post-check：每条载波占用带宽必须完整落在某一空闲窗内。"""
    windows = compute_free_windows(alloc)
    violations = []
    for a in assignments:
        if find_window(windows, a["low_mhz"], a["high_mhz"]) is None:
            violations.append({
                "name": a["name"], "low_mhz": a["low_mhz"], "high_mhz": a["high_mhz"],
                "reason": describe_fit(a["low_mhz"], a["high_mhz"], alloc),
            })
    return {"all_inside": not violations, "violations": violations}


def _plan_out(rec: PlanRecord, current: AllocationScheme) -> PlanRecordOut:
    """规划记录视图：版本不一致或几何越界即标记过期（记录本身永不改动）。"""
    stale_reasons: list[str] = []
    if rec.scheme_version != current.version:
        stale_reasons.append(
            f"分配方案已更新：该规划基于 v{rec.scheme_version}，当前为 v{current.version}")
    alloc = _scheme_alloc(current)
    windows = compute_free_windows(alloc)
    for a in rec.assignments or []:
        try:
            low, high = float(a["low_mhz"]), float(a["high_mhz"])
        except (KeyError, TypeError, ValueError):
            stale_reasons.append(f"载波 {a.get('name', '?')} 的记录不完整，无法复核")
            continue
        if find_window(windows, low, high) is None:
            why = describe_fit(low, high, alloc)
            stale_reasons.append(
                f"载波 {a.get('name', '?')} 频带 [{low:g}, {high:g}] MHz "
                f"在当前方案下越界：{why}")
    return PlanRecordOut(
        id=rec.id, scheme_version=rec.scheme_version,
        current_version=current.version, mode=rec.mode, feasible=rec.feasible,
        message=rec.message or "",
        created_at=rec.created_at.isoformat() if rec.created_at else None,
        stale=bool(stale_reasons), stale_reasons=stale_reasons,
        assignments=rec.assignments or [], post_check=rec.post_check or {},
    )


def _get_scenario_or_404(s: Session, scenario_id: int) -> Scenario:
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise HTTPException(404, "场景不存在")
    return sc


# ---- 计算接口（无状态，数据由前端提交） -----------------------------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        alloc = _checked_allocation(req.allocation) if req.allocation else None
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        result = analyze(carriers, to_rules(req.rules), alloc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["bands"] = bands_view(carriers)
    result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
    if alloc is not None:
        result["allocation"] = allocation_view(alloc)
    return result


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        alloc = None
        if req.allocation is not None:
            alloc = _checked_allocation(req.allocation)
        else:
            req.validate_band()
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        rules = to_rules(req.rules)
        result = plan(carriers, rules,
                      BandLimits(req.band_low_mhz, req.band_high_mhz),
                      mode=req.mode, allocation=alloc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        eff_alloc = alloc or allocation_from_band(
            BandLimits(req.band_low_mhz, req.band_high_mhz))
        post = analyze(planned, rules, eff_alloc)
        post["allocation_check"] = _allocation_check(result["assignments"], eff_alloc)
        result["post_check"] = post
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


def _row_to_out(s: Session, sc: Scenario) -> ScenarioOut:
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
        allocation=_scheme_out(_current_scheme(s, sc)),
    )


def _alloc_or_migrate(req: ScenarioIn) -> AllocationIn:
    """请求未显式给分配方案时，由旧版单一可用范围构造等价单段方案。"""
    if req.allocation is not None:
        return req.allocation
    return AllocationIn(
        segments=[SegmentIn(low_mhz=req.band_low_mhz, high_mhz=req.band_high_mhz,
                            label="S1")],
        exclusions=[], note="由单一可用范围迁移")


def _envelope(alloc_in: AllocationIn) -> tuple[float, float]:
    return (min(s.low_mhz for s in alloc_in.segments),
            max(s.high_mhz for s in alloc_in.segments))


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
        sc = _get_scenario_or_404(s, scenario_id)
        out = _row_to_out(s, sc)
        s.commit()  # 落库可能的懒迁移
        return out


@app.post("/api/scenarios", response_model=ScenarioOut)
def create_scenario(req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
        alloc_in = _alloc_or_migrate(req)
        _checked_allocation(alloc_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        band_lo, band_hi = _envelope(alloc_in)
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=band_lo, band_high_mhz=band_hi,
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
        s.flush()
        scheme = AllocationScheme(scenario_id=sc.id, version=1,
                                  note=alloc_in.note or "初始方案")
        scheme.segments = [AllocationSegment(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                             label=x.label) for x in alloc_in.segments]
        scheme.exclusions = [AllocationExclusion(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                                 reason=x.reason)
                             for x in alloc_in.exclusions]
        s.add(scheme)
        s.commit()
        s.refresh(sc)
        return _row_to_out(s, sc)


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int) -> dict:
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        s.delete(sc)
        s.commit()
        return {"deleted": scenario_id}


@app.put("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: int, req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
        if req.allocation is not None:
            _checked_allocation(req.allocation)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        other = s.scalar(select(Scenario).where(
            Scenario.name == req.name.strip(), Scenario.id != scenario_id))
        if other is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc.name = req.name.strip()
        sc.description = req.description
        sc.guard_required_mhz = req.guard_required_mhz
        sc.leakage_limit_dbm = req.leakage_limit_dbm
        sc.reuse_policy = dict(req.reuse_policy)
        sc.carriers = [
            CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                       bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                       polarization=c.polarization, mask_name=c.mask_name)
            for i, c in enumerate(req.carriers)
        ]
        if req.allocation is not None:
            band_lo, band_hi = _envelope(req.allocation)
            sc.band_low_mhz, sc.band_high_mhz = band_lo, band_hi
            current = _current_scheme(s, sc)
            if not _same_geometry(current, req.allocation):
                _new_scheme_version(s, sc, req.allocation,
                                    note=req.allocation.note or "编辑分配方案")
        else:
            sc.band_low_mhz = req.band_low_mhz
            sc.band_high_mhz = req.band_high_mhz
        s.commit()
        s.refresh(sc)
        return _row_to_out(s, sc)


# ---- 频谱分配方案（版本化） -------------------------------------------------

@app.get("/api/scenarios/{scenario_id}/allocation", response_model=AllocationOut)
def get_allocation(scenario_id: int) -> AllocationOut:
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        out = _scheme_out(_current_scheme(s, sc))
        s.commit()
        return out


@app.get("/api/scenarios/{scenario_id}/allocation/versions",
         response_model=list[AllocationOut])
def get_allocation_versions(scenario_id: int) -> list[AllocationOut]:
    """全部历史版本（旧规划记录按 scheme_version 引用，可追溯）。"""
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        _current_scheme(s, sc)
        s.commit()
        schemes = list(s.scalars(select(AllocationScheme)
                                 .where(AllocationScheme.scenario_id == sc.id)
                                 .order_by(AllocationScheme.version)))
        return [_scheme_out(x) for x in schemes]


@app.put("/api/scenarios/{scenario_id}/allocation", response_model=AllocationOut)
def put_allocation(scenario_id: int, req: AllocationIn) -> AllocationOut:
    """保存分配方案修改：几何有变化才生成新版本，否则返回当前版本。"""
    try:
        _checked_allocation(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        current = _current_scheme(s, sc)
        if _same_geometry(current, req):
            s.commit()
            return _scheme_out(current)
        scheme = _new_scheme_version(s, sc, req, note=req.note or "编辑分配方案")
        band_lo, band_hi = _envelope(req)
        sc.band_low_mhz, sc.band_high_mhz = band_lo, band_hi
        s.commit()
        return _scheme_out(scheme)


# ---- 规划记录（冻结保存 + 过期判定） ----------------------------------------

@app.get("/api/scenarios/{scenario_id}/plans", response_model=list[PlanRecordOut])
def list_plans(scenario_id: int) -> list[PlanRecordOut]:
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        current = _current_scheme(s, sc)
        s.commit()
        recs = list(s.scalars(select(PlanRecord)
                              .where(PlanRecord.scenario_id == sc.id)
                              .order_by(PlanRecord.created_at)))
        return [_plan_out(r, current) for r in recs]


@app.post("/api/scenarios/{scenario_id}/plans", response_model=PlanRecordOut)
def save_plan(scenario_id: int, req: PlanSaveIn) -> PlanRecordOut:
    """把一次求解结果冻结为规划记录。

    保存前按**当前**分配方案复核：任何载波不再完整落入空闲窗即整体拒绝
    （不部分保存），提示重新求解——绝不偷偷移动旧规划。
    """
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        current = _current_scheme(s, sc)
        alloc = _scheme_alloc(current)
        try:
            planned = [
                Carrier(id=None, name=str(a["name"]),
                        center_mhz=float(a["center_mhz"]),
                        bandwidth_mhz=float(a["bandwidth_mhz"]),
                        power_dbm=float(a["power_dbm"]),
                        polarization=str(a["polarization"]),
                        mask_name=str(a["mask_name"]))
                for a in req.assignments
            ]
            for c in planned:
                from .services.masks import MASKS
                if c.mask_name not in MASKS:
                    raise ValueError(f"载波 {c.name!r} 引用了未知掩模 {c.mask_name!r}")
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(status_code=400,
                                detail=f"规划记录不完整或非法：{e}")
        check = _allocation_check(
            [{"name": a["name"], "low_mhz": a["low_mhz"], "high_mhz": a["high_mhz"]}
             for a in req.assignments], alloc)
        if not check["all_inside"]:
            names = "、".join(v["name"] for v in check["violations"])
            raise HTTPException(
                status_code=400,
                detail=f"载波 {names} 在当前分配方案 v{current.version} 下越界，"
                       f"该规划不予保存；请重新求解后再保存。")
        rules = to_rules(RulesIn(guard_required_mhz=sc.guard_required_mhz,
                                 leakage_limit_dbm=sc.leakage_limit_dbm,
                                 reuse_policy=sc.reuse_policy or {}))
        post = analyze(planned, rules, alloc)
        post["allocation_check"] = check
        rec = PlanRecord(
            scenario_id=sc.id, scheme_version=current.version, mode=req.mode,
            feasible=True,
            message=f"基于分配方案 v{current.version} 保存的规划（{len(planned)} 个载波）",
            assignments=req.assignments, post_check=post)
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return _plan_out(rec, current)


# ---- 导出 / 导入（整体校验，不部分保存） ------------------------------------

@app.get("/api/scenarios/{scenario_id}/export", response_model=ScenarioExport)
def export_scenario(scenario_id: int) -> ScenarioExport:
    with Session(engine) as s:
        sc = _get_scenario_or_404(s, scenario_id)
        _current_scheme(s, sc)
        s.commit()
        schemes = list(s.scalars(select(AllocationScheme)
                                 .where(AllocationScheme.scenario_id == sc.id)
                                 .order_by(AllocationScheme.version)))
        plans = list(s.scalars(select(PlanRecord)
                               .where(PlanRecord.scenario_id == sc.id)
                               .order_by(PlanRecord.created_at)))
        return ScenarioExport(
            name=sc.name, description=sc.description,
            band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
            guard_required_mhz=sc.guard_required_mhz,
            leakage_limit_dbm=sc.leakage_limit_dbm,
            reuse_policy=sc.reuse_policy or {},
            carriers=[_carrier_out(c) for c in
                      sorted(sc.carriers, key=lambda c: c.position)],
            allocations=[_scheme_out(x) for x in schemes],
            plans=[{"scheme_version": p.scheme_version, "mode": p.mode,
                    "feasible": p.feasible, "message": p.message,
                    "assignments": p.assignments, "post_check": p.post_check,
                    "created_at": p.created_at.isoformat() if p.created_at else None}
                   for p in plans],
        )


@app.post("/api/scenarios/import", response_model=ScenarioOut)
def import_scenario(req: ScenarioImport) -> ScenarioOut:
    """导入场景：先整体校验（掩模、区间完整性、名称冲突），全部通过才一次落库。"""
    try:
        validate_masks(req.carriers)
        if req.allocations:
            alloc_ins = req.allocations
        elif req.allocation is not None:
            alloc_ins = [req.allocation]
        else:  # 旧格式：单一可用范围迁移为等价单段
            alloc_ins = [AllocationIn(
                segments=[SegmentIn(low_mhz=req.band_low_mhz,
                                    high_mhz=req.band_high_mhz, label="S1")],
                exclusions=[], note="由旧格式导入迁移")]
        for a in alloc_ins:
            _checked_allocation(a)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name.strip())) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在，导入未做任何修改")
        band_lo, band_hi = _envelope(alloc_ins[-1])
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=band_lo, band_high_mhz=band_hi,
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
        s.flush()
        for i, a in enumerate(alloc_ins):
            scheme = AllocationScheme(scenario_id=sc.id, version=i + 1,
                                      note=a.note or ("初始方案" if i == 0 else "历史版本"))
            scheme.segments = [AllocationSegment(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                                 label=x.label) for x in a.segments]
            scheme.exclusions = [AllocationExclusion(low_mhz=x.low_mhz, high_mhz=x.high_mhz,
                                                     reason=x.reason)
                                 for x in a.exclusions]
            s.add(scheme)
        n_versions = len(alloc_ins)
        for p in req.plans:
            if not isinstance(p, dict):
                raise HTTPException(400, "plans 条目必须是对象，导入未做任何修改")
            version = p.get("scheme_version", n_versions)
            if not isinstance(version, int) or not (1 <= version <= n_versions):
                version = n_versions  # 悬空版本引用归并到当前版本，几何复核仍会标过期
            s.add(PlanRecord(
                scenario_id=sc.id, scheme_version=version,
                mode=p.get("mode", "guard_only")
                if p.get("mode") in ("guard_only", "mask_aware") else "guard_only",
                feasible=bool(p.get("feasible", True)),
                message=str(p.get("message", "")),
                assignments=p.get("assignments") or [],
                post_check=p.get("post_check") or {}))
        s.commit()
        s.refresh(sc)
        return _row_to_out(s, sc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
