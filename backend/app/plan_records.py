"""规划方案快照服务：内容指纹、post-check 过期判定。

快照一旦保存就**不可变**——修改分配方案（频段边界、排除窗）或载波、规则后，
旧规划不会被偷偷移动，而是通过：

1. **版本**：分配方案内容变化时版本号递增，快照记录其生成时的版本；
2. **post-check**：读取/刷新时把快照的载波位置在*当前*场景上重新分析，
   并核对每条载波是否仍完整落在当前某个有效净空小片内。

任一项不满足即把快照标记为 ``stale=True`` 并给出原因；原始频率位置、
历史 post-check 结论与生成时版本始终保留可追溯。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .services import allocation as alloc_mod
from .services.analysis import AnalysisRules, Carrier, analyze


def _stable_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def carriers_fingerprint(carriers: list[Carrier]) -> str:
    return _stable_hash([
        [c.name, round(c.center_mhz, 6), round(c.bandwidth_mhz, 6),
         round(c.power_dbm, 6), c.polarization, c.mask_name]
        for c in sorted(carriers, key=lambda c: c.name)
    ])


def rules_fingerprint(rules: AnalysisRules) -> str:
    return _stable_hash([
        round(rules.guard_required_mhz, 6),
        round(rules.leakage_limit_dbm, 6),
        dict(sorted((rules.reuse_policy or {}).items())),
    ])


def assignments_to_carriers(result: dict) -> list[Carrier]:
    return [
        Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                polarization=a["polarization"], mask_name=a["mask_name"])
        for a in result.get("assignments", [])
    ]


def evaluate_staleness(result: dict, snapshot_version: int,
                       current_allocation: alloc_mod.Allocation,
                       current_rules: AnalysisRules,
                       current_carriers: list[Carrier],
                       snap_carriers_fp: str, snap_rules_fp: str,
                       current_carriers_fp: str, current_rules_fp: str,
                       baseline_counts: dict) -> dict:
    """对一份历史规划结果在当前场景上做完整复核。

    返回 {"stale": bool, "reasons": [...], "post_check": {...},
          "boundary_violations": [...], "version_ok": bool}。
    """
    reasons: list[str] = []
    planned = assignments_to_carriers(result)
    by_name = {c.name: c for c in current_carriers}

    version_ok = snapshot_version == current_allocation.version
    if not version_ok:
        reasons.append(
            f"分配方案版本已变化：规划生成于 v{snapshot_version}，"
            f"当前为 v{current_allocation.version}（频段边界或排除窗被编辑过）")

    if snap_carriers_fp != current_carriers_fp:
        reasons.append("载波集合已变化（名称/中心/带宽/功率/极化/掩模被增删或修改）")
    if snap_rules_fp != current_rules_fp:
        reasons.append("规划规则已变化（保护间隔/泄漏限值/极化复用规则）")

    # 边界复核：每条载波必须仍完整落在当前某个有效净空小片内
    boundary_violations: list[dict] = []
    for a in result.get("assignments", []):
        low = a["center_mhz"] - a["bandwidth_mhz"] / 2.0
        high = a["center_mhz"] + a["bandwidth_mhz"] / 2.0
        contained = alloc_mod.containing_piece(current_allocation, low, high)
        crossed = alloc_mod.crossed_exclusions(current_allocation, low, high)
        gone = a["name"] not in by_name
        if contained is None or crossed or gone:
            detail = {
                "carrier": a["name"],
                "occupied_mhz": [round(low, 4), round(high, 4)],
                "planned_segment_index": a.get("segment_index"),
                "crossed_exclusions": [
                    {"low_mhz": e.low_mhz, "high_mhz": e.high_mhz, "reason": e.reason}
                    for e in crossed],
            }
            if gone:
                detail["reason"] = "carrier_removed"
            elif crossed:
                detail["reason"] = "crosses_exclusion"
            else:
                detail["reason"] = "outside_effective_piece"
            boundary_violations.append(detail)
    if boundary_violations:
        reasons.append(
            f"有 {len(boundary_violations)} 条载波的规划位置越出当前可用段/跨越排除窗")

    # post-check：把规划位置在当前规则上重新完整分析
    post_check = analyze(planned, current_rules)
    counts = post_check["counts"]
    base = baseline_counts or {}
    regressions = []
    if counts.get("error", 0) > 0:
        regressions.append(f"当前分析出现 {counts['error']} 条冲突")
    if counts.get("warning", 0) > (base.get("warning") or 0):
        regressions.append(
            f"警告数 {counts['warning']} 多于生成时的 {base.get('warning') or 0}")
    if counts.get("pending", 0) > (base.get("pending") or 0):
        regressions.append(
            f"待评估数 {counts['pending']} 多于生成时的 {base.get('pending') or 0}")
    if regressions:
        reasons.append("post-check 不再满足生成时的结论：" + "；".join(regressions))

    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "version_ok": version_ok,
        "boundary_violations": boundary_violations,
        "post_check": post_check,
    }


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
