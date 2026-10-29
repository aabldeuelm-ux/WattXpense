"""SQLAlchemy models.

`is_simulated` is deliberately tri-state on both devices and readings. `True` means the
data came from the simulator, `False` means it came from hardware, and `NULL` means the
source could not be trusted to declare itself. Collapsing the third case into `False`
would let an unlabelled source pass itself off as a real meter, which is exactly the
claim this project refuses to make.

Ownership is also deliberately nullable on devices and tariffs. A database written before
accounts existed keeps its rows, but those rows have no owner and are therefore invisible
to every account until an operator explicitly claims them. Nothing is assigned to a new
user as a side effect of registering.
"""

from __future__ import annotations

import json
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    devices: Mapped[list["Device"]] = relationship(back_populates="owner")
    tariffs: Mapped[list["Tariff"]] = relationship(back_populates="owner")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


class AuthSession(Base):
    """An opaque server-side session.

    Only a SHA-256 digest of the cookie value is stored. A leaked database therefore does
    not hand an attacker a working session cookie.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        # The id stays globally unique for compatibility with existing installs. The
        # owner filter is what makes a device private; see `owner_id` below.
        Index("ix_devices_owner_id", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # NULL means a legacy row that predates accounts. It is preserved but not shown to
    # any user until an operator runs the explicit claim command.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Tri-state on purpose: see the module docstring.
    is_simulated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nominal_voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    nominal_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sampling_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Hardware configuration. Nothing here is assumed: the sensor model, calibration
    # constants, GPIO assignments and reporting cadence all come from the device record.
    sensor_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    calibration_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    gpio_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    firmware_version: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Device credentials are hashed exactly like session tokens and shown once.
    api_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    api_token_prefix: Mapped[str | None] = mapped_column(String(24), nullable=True)

    # Relay control is disabled until a user deliberately enables it. The state is a
    # command state, not a claim that a physical relay moved: without an acknowledgement
    # from hardware the app only knows a command was requested.
    relay_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    relay_state: Mapped[str] = mapped_column(String(32), default="unknown", server_default="unknown", nullable=False)
    relay_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    readings: Mapped[list["Reading"]] = relationship(back_populates="device")
    owner: Mapped[User | None] = relationship(back_populates="devices")

    @staticmethod
    def _json(value: str | None) -> dict:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

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
            "sampling_interval_seconds": self.sampling_interval_seconds,
            "notes": self.notes,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "sensor_model": self.sensor_model,
            "calibration": self._json(self.calibration_json),
            "gpio_config": self._json(self.gpio_config_json),
            "firmware_version": self.firmware_version,
            "has_device_token": bool(self.api_token_hash),
            "device_token_prefix": self.api_token_prefix,
            "relay_enabled": bool(self.relay_enabled),
            "relay_state": self.relay_state,
            "relay_updated_at": self.relay_updated_at,
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
    # Free-form status from the sensor/firmware, e.g. "ok", "sensor_fault",
    # "calibration_missing". Kept separate from quality_flags because it is the device's
    # own claim, while quality_flags are WattXpense's checks.
    sensor_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    measurement_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    quality_flags: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    device: Mapped[Device] = relationship(back_populates="readings")

    def flag_list(self) -> list[str]:
        if not self.quality_flags:
            return []
        return [item for item in self.quality_flags.split("|") if item]


class Tariff(Base):
    __tablename__ = "tariffs"
    __table_args__ = (Index("ix_tariffs_owner_id", "owner_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL means a legacy tariff. It is preserved but not used by any account until an
    # operator explicitly claims it.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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

    owner: Mapped[User | None] = relationship(back_populates="tariffs")

    def slab_list(self) -> list[dict]:
        if not self.slabs:
            return []
        try:
            parsed = json.loads(self.slabs)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
