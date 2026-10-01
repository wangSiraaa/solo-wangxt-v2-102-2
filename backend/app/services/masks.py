"""频谱发射掩模 (Spectrum Emission Mask, SEM) 的简化模型。

掩模用一组 (频率偏移 MHz, 相对衰减 dB) 折线点表示，偏移相对载波中心频率，
且关于 0 对称（先给一侧非负偏移的点，求值时按绝对值对称处理）。

- 偏移在 [0, x_span] 内：分段线性插值（dB 域）。
- 偏移超过 x_span：信号在该模型中视为已衰减到零，不再产生泄漏。

这是离线教学模型，不对应任何真实设备的发射掩模，也不会生成发射指令。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.interpolate import interp1d

# 掩模求值的频率步长 (MHz)。足够细，保证数值积分误差对教学场景可忽略。
GRID_STEP_MHZ = 0.01


@dataclass(frozen=True)
class Mask:
    name: str
    # 一侧（非负偏移）折线点，必须按 offset 升序
    points: tuple[tuple[float, float], ...]
    description: str = ""

    @property
    def span_mhz(self) -> float:
        return self.points[-1][0]

    def attenuation_db(self, offsets_mhz: np.ndarray) -> np.ndarray:
        """给定频率偏移数组，返回每点的相对衰减 (dB)。

        超出掩模跨度的位置返回 +inf（该模型里信号为零）。
        """
        off = np.abs(np.asarray(offsets_mhz, dtype=float))
        xs = np.array([p[0] for p in self.points], dtype=float)
        ys = np.array([p[1] for p in self.points], dtype=float)
        interp = interp1d(xs, ys, kind="linear", bounds_error=False, fill_value=np.inf)
        return interp(off)


def psd_on_grid(mask: Mask, f_mhz: np.ndarray, center_mhz: float,
                bandwidth_mhz: float, power_dbm: float) -> np.ndarray:
    """在给定全局频率网格上求该载波的 PSD (dBm/Hz)：带内平顶、带外按掩模衰减。"""
    off = np.asarray(f_mhz, dtype=float) - center_mhz
    psd_floor = power_dbm - 10.0 * math.log10(bandwidth_mhz * 1e6)
    att = mask.attenuation_db(off)
    in_band = np.abs(off) <= bandwidth_mhz / 2.0 + 1e-12
    psd = np.full(off.shape, -np.inf, dtype=float)
    psd[in_band] = psd_floor
    oo = (~in_band) & np.isfinite(att)
    psd[oo] = psd_floor + att[oo]
    return psd


def spectrum_curve(mask: Mask, center_mhz: float, bandwidth_mhz: float,
                   power_dbm: float, grid_step_mhz: float = GRID_STEP_MHZ
                   ) -> tuple[np.ndarray, np.ndarray]:
    """返回 (f_mhz, psd_dbm_hz)。

    带内 (|offset| <= BW/2) 平坦：psd = P_dbm - 10log10(带宽Hz)；
    带外按掩模衰减；超过掩模跨度截断。
    """
    half_bw = bandwidth_mhz / 2.0
    span = mask.span_mhz
    f = np.arange(center_mhz - span, center_mhz + span + grid_step_mhz / 2, grid_step_mhz)
    psd = psd_on_grid(mask, f, center_mhz, bandwidth_mhz, power_dbm)
    return f, psd


# --- 示例掩模（纯教学用数值，不依据任何标准或设备指标） --------------------

MASKS: dict[str, Mask] = {
    "strict": Mask(
        name="strict",
        points=(
            (2.0, 0.0),      # 带内边缘（4 MHz 载波半宽 2 MHz）
            (2.5, -25.0),
            (3.5, -50.0),
            (5.0, -70.0),
            (7.0, -90.0),
        ),
        description="陡降掩模：带外衰减快（教学示例）",
    ),
    "loose": Mask(
        name="loose",
        points=(
            (2.0, 0.0),
            (4.0, -20.0),
            (7.0, -40.0),
            (10.0, -60.0),
            (14.0, -80.0),
        ),
        description="缓降掩模：尾部拖尾较长（教学示例，用于演示尾部越界）",
    ),
    "clean": Mask(
        name="clean",
        points=(
            (2.0, 0.0),
            (2.5, -30.0),
            (3.5, -60.0),
            (5.0, -85.0),
            (7.0, -100.0),
        ),
        description="更干净的发射掩模（教学示例）",
    ),
}


def get_mask(name: str) -> Mask:
    if name not in MASKS:
        raise ValueError(f"未知掩模: {name!r}，可选: {sorted(MASKS)}")
    return MASKS[name]
