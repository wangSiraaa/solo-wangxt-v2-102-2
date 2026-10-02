"""SQLAlchemy 模型：场景、载波、示例频谱掩模、频谱分配方案（版本化）、规划记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    band_low_mhz: Mapped[float] = mapped_column(Float, default=80.0)
    band_high_mhz: Mapped[float] = mapped_column(Float, default=220.0)
    guard_required_mhz: Mapped[float] = mapped_column(Float, default=1.0)
    leakage_limit_dbm: Mapped[float] = mapped_column(Float, default=-45.0)
    # 极化复用规则，如 {"H|V": "unknown", "RHCP|V": "allowed"}
    reuse_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")
    allocations: Mapped[list["AllocationScheme"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="AllocationScheme.version")
    plans: Mapped[list["PlanRecord"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="PlanRecord.created_at")


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


class AllocationScheme(Base):
    """频谱分配方案的一个版本：多段可用频段 + 排除窗。

    每次修改生成新版本（旧版本保留），规划记录引用 scheme_version，
    版本不一致即视为过期，保证历史结论可追溯、旧规划不被偷偷移动。
    """
    __tablename__ = "allocation_schemes"
    __table_args__ = (UniqueConstraint("scenario_id", "version",
                                       name="uq_scheme_scenario_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    scenario: Mapped[Scenario] = relationship(back_populates="allocations")
    segments: Mapped[list["AllocationSegment"]] = relationship(
        back_populates="scheme", cascade="all, delete-orphan",
        order_by="AllocationSegment.low_mhz")
    exclusions: Mapped[list["AllocationExclusion"]] = relationship(
        back_populates="scheme", cascade="all, delete-orphan",
        order_by="AllocationExclusion.low_mhz")


class AllocationSegment(Base):
    __tablename__ = "allocation_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_schemes.id", ondelete="CASCADE"))
    low_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    high_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(64), default="")

    scheme: Mapped[AllocationScheme] = relationship(back_populates="segments")


class AllocationExclusion(Base):
    __tablename__ = "allocation_exclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_schemes.id", ondelete="CASCADE"))
    low_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    high_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), default="")

    scheme: Mapped[AllocationScheme] = relationship(back_populates="exclusions")


class PlanRecord(Base):
    """保存的规划结果：按保存时的方案版本冻结，事后只做过期标记，绝不改动。"""
    __tablename__ = "plan_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    scheme_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="guard_only")
    feasible: Mapped[bool] = mapped_column(default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    assignments: Mapped[list] = mapped_column(JSON, default=list)
    post_check: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    scenario: Mapped[Scenario] = relationship(back_populates="plans")


class MaskRow(Base):
    """示例频谱发射掩模：名称 + 折线点 + 说明（教学示例）。"""
    __tablename__ = "masks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 一侧（非负偏移）折线点 [[offset_mhz, attenuation_db], ...]
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    span_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
