"""Unit helpers.

频率在内部统一用 MHz(浮点)，功率在用户界面用 dBm。

教学要点：功率的算术汇总必须在线性域 (W) 进行，不能直接对 dBm 求和；
显示时再换算回 dBm。
"""
from __future__ import annotations

import math

# 单位约定：内部频率 = MHz，内部功率 = W（线性域）
DBM_REF_WATT = 1e-3


def dbm_to_watt(p_dbm: float) -> float:
    """dBm -> W，线性域。"""
    return DBM_REF_WATT * 10.0 ** (p_dbm / 10.0)


def watt_to_dbm(p_w: float) -> float:
    """W -> dBm。非正值返回 -inf 表示无功率。"""
    if p_w <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(p_w / DBM_REF_WATT)


def total_power_watt(powers_dbm: list[float]) -> float:
    """多个载波的总功率：逐个转到 W 再相加（线性域求和）。"""
    return sum(dbm_to_watt(p) for p in powers_dbm)


def total_power_dbm(powers_dbm: list[float]) -> float:
    """总功率的 dBm 显示值。"""
    return watt_to_dbm(total_power_watt(powers_dbm))
