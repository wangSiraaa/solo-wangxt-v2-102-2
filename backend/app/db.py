"""SQLAlchemy 模型：场景、载波、示例频谱掩模、规划方案快照。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 旧版单频段字段保留：新分配方案缺省时等价为单段 [band_low, band_high]
    band_low_mhz: Mapped[float] = mapped_column(Float, default=80.0)
    band_high_mhz: Mapped[float] = mapped_column(Float, default=220.0)
    guard_required_mhz: Mapped[float] = mapped_column(Float, default=1.0)
    leakage_limit_dbm: Mapped[float] = mapped_column(Float, default=-45.0)
    # 极化复用规则，如 {"H|V": "unknown", "RHCP|V": "allowed"}
    reuse_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # 频谱分配方案（版本化）：
    # {"name": ..., "version": int,
    #  "segments": [{"low_mhz","high_mhz"}],
    #  "exclusions": [{"low_mhz","high_mhz","reason"}]}
    # 旧数据该列为 NULL，读取时由 band_low/high 即时迁移成等价单段。
    allocation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")
    plans: Mapped[list["PlanSnapshotRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="PlanSnapshotRow.id")


class CarrierRow(Base):
    __tablename__ = "carriers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    center_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    bandwidth_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    power_dbm: Mapped[float] = mapped_column(Float, nullable=False)
    polarization: Mapped[str] = mapped_column(String(8), nullable=False)
    mask_name: Mapped[str] = mapped_column(String(32), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="carriers")


class MaskRow(Base):
    """示例频谱发射掩模：名称 + 折线点 + 说明（教学示例）。"""
    __tablename__ = "masks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 一侧（非负偏移）折线点 [[offset_mhz, attenuation_db], ...]
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    span_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class PlanSnapshotRow(Base):
    """一次规划结果的**不可变**快照：绑定生成时的分配方案版本。

    之后修改场景（频段边界/排除窗/载波/规则）不会移动该方案；
    读取时用 post-check 与内容指纹重新判定是否过期（stale）。
    """
    __tablename__ = "plan_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    mode: Mapped[str] = mapped_column(String(16), default="guard_only")
    # 生成时的分配方案版本与内容指纹
    allocation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    # 当时载波/规则的内容指纹
    carriers_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    rules_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    # 完整规划结果（assignments / pair_constraints / message / allocation 视图等）
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 生成时的 post-check 摘要（error/warning/pending 计数），作为历史结论
    baseline_counts: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    # 最近一次刷新判定出的过期状态（None=尚未刷新过）；快照内容本身永不改写
    last_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_stale_reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="plans")
