"""建表并写入示例掩模与教学演示场景（幂等：重复执行不产生重复数据）。

同时负责把旧版场景（只有单一连续可用范围 band_low/high）迁移为
等价的单段频谱分配方案（版本 1）。
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .config import DATABASE_URL
from .db import (AllocationExclusion, AllocationScheme, AllocationSegment,
                 Base, CarrierRow, MaskRow, Scenario)
from .services.masks import MASKS

DEMO_POLICY = {
    "H|V": "unknown",     # 水平/垂直：隔离度未知 -> 同频复用待评估
    "RHCP|V": "allowed",  # 右旋圆极化/垂直：已知隔离足够 -> 允许同频复用
    "H|RHCP": "forbidden",
}

# 所有载波带宽相同 (4 MHz)、功率不同；分别制造保护带不足、尾部越界与重叠
DEMO_CARRIERS = [
    # 保护带不足(0.5 MHz) + 双向掩模尾部越界，且因功率不同两个方向泄漏量不同
    dict(name="C1", position=0, center_mhz=100.0, bandwidth_mhz=4.0,
         power_dbm=30.0, polarization="H", mask_name="loose"),
    dict(name="C2", position=1, center_mhz=104.5, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="loose"),
    # 干净对照载波：间隔 11 MHz
    dict(name="C3", position=2, center_mhz=115.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="strict"),
    # 保护带不足(0.5 MHz)：弱载波 loose 尾部侵入强载波，反向不越界
    dict(name="C4", position=3, center_mhz=130.0, bandwidth_mhz=4.0,
         power_dbm=30.0, polarization="H", mask_name="strict"),
    dict(name="C5", position=4, center_mhz=134.5, bandwidth_mhz=4.0,
         power_dbm=25.0, polarization="H", mask_name="loose"),
    # 与 C1 同频段、异极化：H|V 规则未知 -> 待评估
    dict(name="C6", position=5, center_mhz=100.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="V", mask_name="strict"),
    # 保护带足够(2 MHz) 但强载波 loose 拖尾仍越界，反向不越界
    dict(name="C7", position=6, center_mhz=150.0, bandwidth_mhz=4.0,
         power_dbm=30.0, polarization="H", mask_name="loose"),
    dict(name="C8", position=7, center_mhz=156.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="strict"),
    # 真实频带重叠 0.5 MHz（同极化）
    dict(name="C9", position=8, center_mhz=170.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="strict"),
    dict(name="C10", position=9, center_mhz=173.5, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="strict"),
    # 同频段异极化：RHCP|V 规则允许复用 -> 不报冲突
    dict(name="C11", position=10, center_mhz=200.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="RHCP", mask_name="strict"),
    dict(name="C12", position=11, center_mhz=200.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="V", mask_name="strict"),
]

# 多段频谱演示：两段不连续可用频段 + 一个排除窗（保护空洞）
DEMO2_SEGMENTS = [
    dict(low_mhz=80.0, high_mhz=120.0, label="S1 低空段"),
    dict(low_mhz=140.0, high_mhz=220.0, label="S2 高空段"),
]
DEMO2_EXCLUSIONS = [
    dict(low_mhz=95.0, high_mhz=99.0, reason="保护空洞：预留无线电天文观测"),
]
DEMO2_CARRIERS = [
    dict(name="D1", position=0, center_mhz=88.0, bandwidth_mhz=4.0,
         power_dbm=25.0, polarization="H", mask_name="strict"),
    # 录入位置压在排除窗上 -> 分析报 out_of_band，规划会把它挪进空闲窗
    dict(name="D2", position=1, center_mhz=97.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="H", mask_name="loose"),
    dict(name="D3", position=2, center_mhz=110.0, bandwidth_mhz=6.0,
         power_dbm=20.0, polarization="V", mask_name="strict"),
    dict(name="D4", position=3, center_mhz=150.0, bandwidth_mhz=4.0,
         power_dbm=30.0, polarization="H", mask_name="loose"),
    dict(name="D5", position=4, center_mhz=170.0, bandwidth_mhz=8.0,
         power_dbm=20.0, polarization="RHCP", mask_name="strict"),
    dict(name="D6", position=5, center_mhz=200.0, bandwidth_mhz=4.0,
         power_dbm=20.0, polarization="V", mask_name="strict"),
]


def _scheme(scenario_id: int, version: int, note: str,
            segments: list[dict], exclusions: list[dict]) -> AllocationScheme:
    return AllocationScheme(
        scenario_id=scenario_id, version=version, note=note,
        segments=[AllocationSegment(**s) for s in segments],
        exclusions=[AllocationExclusion(**e) for e in exclusions])


def migrate_allocations(s: Session) -> None:
    """旧场景迁移：没有分配方案的场景按 band_low/high 生成等价单段 v1。"""
    for sc in s.scalars(select(Scenario)).all():
        has = s.scalar(select(AllocationScheme.id)
                       .where(AllocationScheme.scenario_id == sc.id).limit(1))
        if has is None:
            s.add(_scheme(sc.id, 1, "由旧版单一可用范围迁移为等价频段",
                          [dict(low_mhz=sc.band_low_mhz,
                                high_mhz=sc.band_high_mhz, label="S1")], []))


def seed(engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for m in MASKS.values():
            row = s.scalar(select(MaskRow).where(MaskRow.name == m.name))
            if row is None:
                s.add(MaskRow(name=m.name, points=[list(p) for p in m.points],
                              span_mhz=m.span_mhz, description=m.description))

        existing = s.scalar(select(Scenario).where(Scenario.name == "教学演示场景"))
        if existing is None:
            sc = Scenario(
                name="教学演示场景",
                description="同带宽不同功率：保护带不足、掩模尾部越界（方向性）、"
                            "频带重叠、极化复用待评估/允许，全部冲突可定位到载波对。",
                band_low_mhz=80.0, band_high_mhz=220.0,
                guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                reuse_policy=DEMO_POLICY,
            )
            sc.carriers = [CarrierRow(**kw) for kw in DEMO_CARRIERS]
            s.add(sc)

        demo2 = s.scalar(select(Scenario).where(Scenario.name == "多段频谱演示"))
        if demo2 is None:
            sc2 = Scenario(
                name="多段频谱演示",
                description="两段不连续可用频段 [80,120]/[140,220] MHz + 排除窗 "
                            "[95,99] MHz：D2 录入位置压在保护空洞上，规划时会被"
                            "挪进合法空闲窗；载波不得跨段间空洞或排除窗。",
                band_low_mhz=80.0, band_high_mhz=220.0,
                guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                reuse_policy=DEMO_POLICY,
            )
            sc2.carriers = [CarrierRow(**kw) for kw in DEMO2_CARRIERS]
            s.add(sc2)
            s.flush()
            s.add(_scheme(sc2.id, 1, "初始多段方案",
                          DEMO2_SEGMENTS, DEMO2_EXCLUSIONS))

        migrate_allocations(s)
        s.commit()


if __name__ == "__main__":
    eng = create_engine(DATABASE_URL)
    seed(eng)
    print("seed done")
