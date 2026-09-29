"""SQLAlchemy models.

`is_simulated` is deliberately tri-state on both devices and readings. `True` means the
data came from the simulator, `False` means it came from hardware, and `NULL` means the
source could not be trusted to declare itself. Collapsing the third case into `False`
would let an unlabelled source pass itself off as a real meter, which is exactly the
claim this project refuses to make.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(dt_timezone.utc).replace(microsecond=0)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Tri-state on purpose: see the module docstring.
    is_simulated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nominal_voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    nominal_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    readings: Mapped[list["Reading"]] = relationship(back_populates="device")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "location": self.location,
            "source": self.source,
            "is_simulated": self.is_simulated,
            "is_primary": self.is_primary,
            "nominal_voltage_v": self.nominal_voltage_v,
            "nominal_interval_seconds": self.nominal_interval_seconds,
            "notes": self.notes,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
        }


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        # Re-sending the same device and timestamp updates in place rather than
        # double counting, which is what makes ingestion idempotent. The ts index comes
        # from `index=True` on the column below.
        UniqueConstraint("device_id", "ts", name="uq_readings_device_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(120), ForeignKey("devices.id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    voltage: Mapped[float | None] = mapped_column(Float, nullable=True)
    current: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Energy for ONE reporting interval, never a cumulative meter total.
    energy_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_simulated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    quality_flags: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    device: Mapped[Device] = relationship(back_populates="readings")

    def flag_list(self) -> list[str]:
        if not self.quality_flags:
            return []
        return [item for item in self.quality_flags.split("|") if item]


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    flat_rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    offpeak_rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_start_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_end_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    standing_charge_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    tax_percent: Mapped[float] = mapped_column(Float, default=0.0)
    billing_start_day: Mapped[int] = mapped_column(Integer, default=1)
    # Slab structure as JSON text. Added after the fact on installs that predate slab
    # support, which is why database._add_missing_columns exists.
    slabs: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def slab_list(self) -> list[dict]:
        import json

        if not self.slabs:
            return []
        try:
            parsed = json.loads(self.slabs)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
