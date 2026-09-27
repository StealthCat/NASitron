from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class WebUser(Base):
    __tablename__ = "web_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(120))
    auth_type: Mapped[str] = mapped_column(String(20), default="key")
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    smart_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    sudo_for_smart: Mapped[bool] = mapped_column(Boolean, default=True)
    strict_host_key: Mapped[bool] = mapped_column(Boolean, default=False)
    host_key_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zpool_status_json_supported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expected_pools_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_smart_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_full_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_collection_state: Mapped[str] = mapped_column(String(20), default="never")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    current_state: Mapped["CurrentState | None"] = relationship(
        back_populates="server", cascade="all, delete-orphan", uselist=False
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="server", cascade="all, delete-orphan")
    metrics: Mapped[list["Metric"]] = relationship(back_populates="server", cascade="all, delete-orphan")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="server", cascade="all, delete-orphan")
    maintenance_actions: Mapped[list["MaintenanceAction"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class RemoteEnrollment(Base):
    __tablename__ = "remote_enrollments"
    __table_args__ = (Index("ix_remote_enrollment_expires", "expires_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_enc: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text)
    private_key_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CurrentState(Base):
    __tablename__ = "current_states"

    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    payload_json: Mapped[str] = mapped_column(Text)

    server: Mapped[Server] = relationship(back_populates="current_state")


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        Index("ix_snapshot_server_time", "server_id", "captured_at"),
        Index("ix_snapshot_time", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    payload_json: Mapped[str] = mapped_column(Text)

    server: Mapped[Server] = relationship(back_populates="snapshots")


class Metric(Base):
    __tablename__ = "metrics"
    __table_args__ = (
        Index("ix_metric_lookup", "server_id", "name", "scope", "captured_at"),
        Index("ix_metric_time", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(255), default="")
    value: Mapped[float] = mapped_column(Float)

    server: Mapped[Server] = relationship(back_populates="metrics")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("server_id", "key", name="uq_alert_server_key"),
        Index("ix_alert_server_active_seen", "server_id", "active", "last_seen"),
        Index("ix_alert_notification_due", "active", "last_notified_at", "next_notification_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_notification_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notification_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    server: Mapped[Server] = relationship(back_populates="alerts")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MaintenanceAction(Base):
    __tablename__ = "maintenance_actions"
    __table_args__ = (Index("ix_maintenance_server_time", "server_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(30), default="accepted")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    pool: Mapped[str] = mapped_column(String(255), default="")
    old_device: Mapped[str] = mapped_column(Text, default="")
    new_device: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    server: Mapped[Server] = relationship(back_populates="maintenance_actions")


class DriveLabel(Base):
    __tablename__ = "drive_labels"
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    identity: Mapped[str] = mapped_column(String(255), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"
    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(255))
    actor: Mapped[str] = mapped_column(String(120))
