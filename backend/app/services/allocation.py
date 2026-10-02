"""频谱分配方案（spectrum allocation）：多个带版本的可用频段与排除窗。

实际教学场景常包含多段互不连续的可用频谱，中间隔着不能跨越的保护空洞
（如其他业务占用、保护频带）。单一连续频段 [low, high] 无法表达这种结构，
因此引入“频谱分配方案”：

- ``segments``：一个或多个互不重叠、按频率排序的**可用频段**；
- ``exclusions``：若干**排除窗**（保护空洞），可以落在某段可用段内（把该段
  切成有效小片），也可以跨多段/覆盖段间空洞；
- ``version``：分配方案版本号（正整数），每次分配内容被修改即递增，
  用于标注历史规划是否过期。

载波占用带宽必须**完整落在某一可用段被排除窗切出的有效小片 (piece) 内**，
不能跨越段间空洞，也不能覆盖任何排除窗。

旧场景只有单一可用范围 [band_low, band_high]，可用 :func:`allocation_from_band`
迁移成一个等价的单段分配方案。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EPS_MHZ = 1e-9


@dataclass(frozen=True)
class Segment:
    low_mhz: float
    high_mhz: float

    @property
    def width_mhz(self) -> float:
        return self.high_mhz - self.low_mhz


@dataclass(frozen=True)
class Exclusion:
    low_mhz: float
    high_mhz: float
    reason: str = ""

    @property
    def width_mhz(self) -> float:
        return self.high_mhz - self.low_mhz


@dataclass(frozen=True)
class Allocation:
    """一个版本化的频谱分配方案。"""
    segments: tuple[Segment, ...]
    exclusions: tuple[Exclusion, ...] = ()
    version: int = 1
    name: str = "默认分配方案"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "segments": [{"low_mhz": s.low_mhz, "high_mhz": s.high_mhz}
                         for s in self.segments],
            "exclusions": [{"low_mhz": e.low_mhz, "high_mhz": e.high_mhz,
                            "reason": e.reason} for e in self.exclusions],
        }


# ---- 校验与构造 -----------------------------------------------------------

def allocation_from_payload(payload: Optional[dict]) -> Allocation:
    """从已通过 pydantic 校验的 dict 构造 Allocation（排序/裁剪后规范化）。"""
    if not payload:
        raise ValueError("缺少频谱分配方案")
    raw_segs = payload.get("segments") or []
    raw_excs = payload.get("exclusions") or []
    if not raw_segs:
        raise ValueError("频谱分配方案至少要包含一个可用频段")

    segs = sorted((Segment(float(s["low_mhz"]), float(s["high_mhz"]))
                   for s in raw_segs), key=lambda s: s.low_mhz)
    # 排除窗重叠是允许的（相邻保护带可能重叠）；规范化时排序、裁剪到各段范围
    excs = sorted((Exclusion(float(e["low_mhz"]), float(e["high_mhz"]),
                             str(e.get("reason") or ""))
                   for e in raw_excs), key=lambda e: e.low_mhz)

    # 基础几何校验在构造阶段即拒绝（保证不会产生不完整区间对象）
    for sg in segs:
        if sg.high_mhz <= sg.low_mhz:
            raise ValueError(
                f"可用频段 [{sg.low_mhz:g}, {sg.high_mhz:g}] MHz 上界必须大于下界")
    for ex in excs:
        if ex.high_mhz <= ex.low_mhz:
            raise ValueError(
                f"排除窗 [{ex.low_mhz:g}, {ex.high_mhz:g}] MHz 上界必须大于下界")

    # 排除窗必须与至少一个可用段有正长度的交集，否则属于无效/不完整区间
    for e in excs:
        if not any(e.low_mhz < s.high_mhz - EPS_MHZ
                   and e.high_mhz > s.low_mhz + EPS_MHZ for s in segs):
            raise ValueError(
                f"排除窗 [{e.low_mhz:g}, {e.high_mhz:g}] MHz "
                f"不与任何可用频段相交，无法形成有效分配（拒绝部分保存）")

    # 版本号：缺省视为 1（新建）；显式给定时保留（导入历史需还原原始版本）
    version = payload.get("version")
    version = int(version) if version is not None else 1
    if version < 1:
        raise ValueError("分配方案版本号必须为正整数")
    return Allocation(segments=tuple(segs), exclusions=tuple(excs),
                      version=version, name=str(payload.get("name") or "默认分配方案"))


def allocation_from_band(low_mhz: float, high_mhz: float,
                         version: int = 1,
                         name: str = "由旧版单频段迁移") -> Allocation:
    """旧场景的单一可用范围 -> 一个等价的单段分配方案（无排除窗）。"""
    if high_mhz <= low_mhz:
        raise ValueError("可用频段上界必须大于下界")
    return Allocation(
        segments=(Segment(float(low_mhz), float(high_mhz)),),
        exclusions=(), version=version, name=name)


def effective_pieces(alloc: Allocation) -> list[tuple[Segment, list[Exclusion]]]:
    """把每个可用段按排除窗切成有效小片。

    返回 [(piece, cut_by_exclusions), ...]，按频率排序；
    宽度为 0 的小片（整段被排除窗覆盖）被丢弃。
    """
    return [(p, cuts) for p, cuts, _lo, _hi in effective_pieces_with_anchor(alloc)]


def validate_allocation(alloc: Allocation) -> None:
    """跨字段语义校验：可用段有效、互不重叠；排除窗不得把所有段完全清空。"""
    if not alloc.segments:
        raise ValueError("频谱分配方案至少要包含一个可用频段")
    prev: Optional[Segment] = None
    for s in alloc.segments:
        if s.high_mhz <= s.low_mhz:
            raise ValueError(
                f"可用频段 [{s.low_mhz:g}, {s.high_mhz:g}] MHz 上界必须大于下界")
        if prev is not None and s.low_mhz < prev.high_mhz - EPS_MHZ:
            raise ValueError(
                f"可用频段 [{prev.low_mhz:g}, {prev.high_mhz:g}] 与 "
                f"[{s.low_mhz:g}, {s.high_mhz:g}] MHz 重叠；多段可用频谱必须互不相交")
        prev = s
    for e in alloc.exclusions:
        if e.high_mhz <= e.low_mhz:
            raise ValueError(
                f"排除窗 [{e.low_mhz:g}, {e.high_mhz:g}] MHz 上界必须大于下界")
    if not effective_pieces(alloc):
        raise ValueError("排除窗覆盖了全部可用频段，分配方案没有任何有效净空")


def allocation_fingerprint(alloc: Allocation) -> str:
    """与版本无关的内容指纹：段/排除窗的几何形状（用于检测内容是否真的变化）。"""
    import hashlib
    import json
    norm = {
        "segments": [[float(round(s.low_mhz, 6)), float(round(s.high_mhz, 6))]
                     for s in sorted(alloc.segments, key=lambda s: s.low_mhz)],
        "exclusions": [[float(round(e.low_mhz, 6)), float(round(e.high_mhz, 6))]
                       for e in sorted(alloc.exclusions, key=lambda e: e.low_mhz)],
    }
    return hashlib.sha256(json.dumps(norm, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:16]


# ---- 载波归属 / 越界检查 ---------------------------------------------------

def containing_piece(alloc: Allocation, low_mhz: float, high_mhz: float
                     ) -> Optional[tuple[int, Segment]]:
    """返回完整包含 [low, high] 的有效小片（序号从 0 起）；不存在返回 None。"""
    if high_mhz <= low_mhz:
        return None
    for idx, (piece, _cuts) in enumerate(effective_pieces(alloc)):
        if low_mhz >= piece.low_mhz - EPS_MHZ and high_mhz <= piece.high_mhz + EPS_MHZ:
            return idx, piece
    return None


def crossed_exclusions(alloc: Allocation, low_mhz: float,
                       high_mhz: float) -> list[Exclusion]:
    """与占用区间有正长度交集的排除窗（即被跨越/覆盖的保护空洞）。"""
    return [e for e in alloc.exclusions
            if low_mhz < e.high_mhz - EPS_MHZ and high_mhz > e.low_mhz + EPS_MHZ]


def allocation_view(alloc: Allocation) -> dict:
    """分析/规划接口返回的分配视图：边界、排除区、有效小片。"""
    pieces = [
        {"index": i, "low_mhz": round(p.low_mhz, 6), "high_mhz": round(p.high_mhz, 6),
         "width_mhz": round(p.width_mhz, 6),
         "segment_low_mhz": seg_lo, "segment_high_mhz": seg_hi}
        for i, (p, cuts, seg_lo, seg_hi) in enumerate(effective_pieces_with_anchor(alloc))
    ]
    return {
        "name": alloc.name,
        "version": alloc.version,
        "fingerprint": allocation_fingerprint(alloc),
        "segments": [{"low_mhz": s.low_mhz, "high_mhz": s.high_mhz,
                      "width_mhz": round(s.width_mhz, 6)} for s in alloc.segments],
        "exclusions": [{"low_mhz": e.low_mhz, "high_mhz": e.high_mhz,
                        "width_mhz": round(e.width_mhz, 6), "reason": e.reason}
                       for e in alloc.exclusions],
        "pieces": pieces,
    }


def effective_pieces_with_anchor(
        alloc: Allocation) -> list[tuple[Segment, list[Exclusion], float, float]]:
    """effective_pieces 的扩展版，附带所属原始可用段的边界。"""
    out = []
    for seg in alloc.segments:
        cuts = sorted(
            (max(e.low_mhz, seg.low_mhz), min(e.high_mhz, seg.high_mhz), e)
            for e in alloc.exclusions
            if e.low_mhz < seg.high_mhz - EPS_MHZ and e.high_mhz > seg.low_mhz + EPS_MHZ)
        cur = seg.low_mhz
        used: list[Exclusion] = []
        for c_lo, c_hi, exc in cuts:
            if c_lo <= cur + EPS_MHZ:
                cur = max(cur, c_hi)
                used.append(exc)
                continue
            piece = Segment(cur, c_lo)
            if piece.width_mhz > EPS_MHZ:
                out.append((piece, list(used), seg.low_mhz, seg.high_mhz))
            cur = c_hi
            used.append(exc)
        if cur < seg.high_mhz - EPS_MHZ:
            out.append((Segment(cur, seg.high_mhz), list(used),
                        seg.low_mhz, seg.high_mhz))
    return out
