"""频谱分配方案（多段可用频段 + 排除窗）的纯函数模型。

一个分配方案 = 若干互不重叠的可用频段 (segments) + 若干排除窗 (exclusions)。
载波放置规则：载波占用带宽必须**完整**落在某一可用段内，且不得跨越
段间空洞或排除窗。等价地：把每个可用段减去落在其中的排除窗，得到一组
“空闲窗”(free windows)，载波频带 [low, high] 必须完整落入某一个空闲窗。

本模块只做几何计算与校验，不依赖数据库；校验失败抛 ValueError（中文消息），
由 API 层转成 400，保证不完整/冲突的区间不会被部分保存。
"""
from __future__ import annotations

from dataclasses import dataclass

# 频率比较容差 (MHz)：kHz 网格下的浮点噪声远小于此值
EPS_MHZ = 1e-6


@dataclass(frozen=True)
class Segment:
    low_mhz: float
    high_mhz: float
    label: str = ""

    @property
    def width_mhz(self) -> float:
        return self.high_mhz - self.low_mhz


@dataclass(frozen=True)
class Exclusion:
    low_mhz: float
    high_mhz: float
    reason: str = ""


@dataclass(frozen=True)
class Allocation:
    segments: tuple[Segment, ...]
    exclusions: tuple[Exclusion, ...] = ()


@dataclass(frozen=True)
class FreeWindow:
    """可用段减去排除窗后得到的可放置区间。"""
    segment_index: int
    segment_label: str
    low_mhz: float
    high_mhz: float

    @property
    def width_mhz(self) -> float:
        return self.high_mhz - self.low_mhz


def validate_allocation(segments: list[Segment],
                        exclusions: list[Exclusion]) -> None:
    """校验一组频段与排除窗；任何问题都抛 ValueError（调用方应整体拒绝保存）。

    - 至少一个可用段；每段 low < high；数值有限；
    - 可用段两两不得重叠（相切允许）；
    - 排除窗 low < high；排除窗不得越出所有可用段包络之外
      （允许部分搭界，但完全落在任何可用段之外的排除窗视为录入错误）。
    """
    if not segments:
        raise ValueError("可用频段不能为空：至少需要一个 [low, high) 区间")
    for s in segments:
        _check_interval(s.low_mhz, s.high_mhz, f"可用段 {s.label or ''}".strip())
    ordered = sorted(segments, key=lambda s: (s.low_mhz, s.high_mhz))
    for a, b in zip(ordered, ordered[1:]):
        if b.low_mhz < a.high_mhz - EPS_MHZ:
            raise ValueError(
                f"可用段 [{a.low_mhz}, {a.high_mhz}] 与 [{b.low_mhz}, {b.high_mhz}] "
                f"重叠，请拆分为不重叠区间")
    env_lo = min(s.low_mhz for s in segments)
    env_hi = max(s.high_mhz for s in segments)
    for e in exclusions:
        _check_interval(e.low_mhz, e.high_mhz, f"排除窗 {e.reason or ''}".strip())
        if e.high_mhz <= env_lo + EPS_MHZ or e.low_mhz >= env_hi - EPS_MHZ:
            raise ValueError(
                f"排除窗 [{e.low_mhz}, {e.high_mhz}] 完全落在可用频段包络 "
                f"[{env_lo}, {env_hi}] 之外，请检查录入")


def _check_interval(low: float, high: float, what: str) -> None:
    import math
    if not (math.isfinite(low) and math.isfinite(high)):
        raise ValueError(f"{what} 的边界必须是有限数值")
    if high <= low:
        raise ValueError(f"{what} 区间不完整：[{low}, {high}] 上界必须大于下界")


def compute_free_windows(allocation: Allocation) -> list[FreeWindow]:
    """把每个可用段减去与之相交的排除窗，返回排序后的空闲窗列表。"""
    windows: list[FreeWindow] = []
    for idx, seg in enumerate(allocation.segments):
        cuts = sorted(
            (e for e in allocation.exclusions
             if e.low_mhz < seg.high_mhz - EPS_MHZ and e.high_mhz > seg.low_mhz + EPS_MHZ),
            key=lambda e: e.low_mhz)
        lo = seg.low_mhz
        for e in cuts:
            ex_lo = max(e.low_mhz, seg.low_mhz)
            ex_hi = min(e.high_mhz, seg.high_mhz)
            if ex_lo > lo + EPS_MHZ:
                windows.append(FreeWindow(idx, seg.label, lo, ex_lo))
            lo = max(lo, ex_hi)
        if seg.high_mhz > lo + EPS_MHZ:
            windows.append(FreeWindow(idx, seg.label, lo, seg.high_mhz))
    return sorted(windows, key=lambda w: (w.low_mhz, w.high_mhz))


def find_window(windows: list[FreeWindow], low_mhz: float,
                high_mhz: float) -> FreeWindow | None:
    """频带 [low, high] 完整落入的第一个空闲窗；没有则 None。"""
    for w in windows:
        if w.low_mhz <= low_mhz + EPS_MHZ and high_mhz <= w.high_mhz + EPS_MHZ:
            return w
    return None


def feasible_windows_for(windows: list[FreeWindow],
                         bandwidth_mhz: float) -> list[FreeWindow]:
    """能完整容纳该带宽的空闲窗。"""
    return [w for w in windows if w.width_mhz >= bandwidth_mhz - EPS_MHZ]


def diagnose_carrier_fit(name: str, bandwidth_mhz: float,
                         allocation: Allocation) -> str | None:
    """若该载波在任何空闲窗都放不下，返回中文原因；否则返回 None。

    区分两种教学上关键的情形：
    - 带宽大于任一可用段（段本身就装不下）；
    - 段够宽，但排除窗把所有空闲窗都切得太窄（只能跨排除窗放置）。
    """
    windows = compute_free_windows(allocation)
    if feasible_windows_for(windows, bandwidth_mhz):
        return None
    widest_seg = max(s.width_mhz for s in allocation.segments)
    widest_win = max((w.width_mhz for w in windows), default=0.0)
    if bandwidth_mhz > widest_seg + EPS_MHZ:
        return (f"载波 {name} 带宽 {bandwidth_mhz:g} MHz 大于任一可用段"
                f"（最大段宽 {widest_seg:g} MHz），无法完整放入")
    return (f"载波 {name} 带宽 {bandwidth_mhz:g} MHz 只能跨越排除窗放置："
            f"可用段内的空闲窗最大仅 {widest_win:g} MHz")


def describe_fit(low_mhz: float, high_mhz: float,
                 allocation: Allocation) -> str | None:
    """频带越界时的中文描述（用于分析结论与过期判定）；放得下返回 None。"""
    windows = compute_free_windows(allocation)
    if find_window(windows, low_mhz, high_mhz) is not None:
        return None
    for seg in allocation.segments:
        if seg.low_mhz - EPS_MHZ <= low_mhz and high_mhz <= seg.high_mhz + EPS_MHZ:
            hit = [e for e in allocation.exclusions
                   if e.low_mhz < high_mhz - EPS_MHZ and e.high_mhz > low_mhz + EPS_MHZ]
            if hit:
                e = hit[0]
                return (f"跨越排除窗 [{e.low_mhz:g}, {e.high_mhz:g}] MHz"
                        + (f"（{e.reason}）" if e.reason else ""))
            return f"未被任何空闲窗完整容纳（段 {seg.label or '?'} 内）"
    return "超出所有可用频段范围"


def allocation_view(allocation: Allocation) -> dict:
    """分配方案几何的 JSON 视图（图表画边界/排除区/归属用）。"""
    windows = compute_free_windows(allocation)
    return {
        "segments": [{"low_mhz": s.low_mhz, "high_mhz": s.high_mhz, "label": s.label}
                     for s in allocation.segments],
        "exclusions": [{"low_mhz": e.low_mhz, "high_mhz": e.high_mhz, "reason": e.reason}
                       for e in allocation.exclusions],
        "free_windows": [{"segment_index": w.segment_index,
                          "segment_label": w.segment_label,
                          "low_mhz": round(w.low_mhz, 4),
                          "high_mhz": round(w.high_mhz, 4)} for w in windows],
    }
