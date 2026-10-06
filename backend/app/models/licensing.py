"""Install-wide licence state (LIC-6, LIC-8, LIC-25, LIC-26, LIC-27, LIC-29).

One row per installation, not per organisation: a licence covers the whole
install. See ``services/licensing/state.py`` for how it is read.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType

#: The only value ``LicenseState.slot`` takes; its unique index keeps the table to one row.
INSTALL_SLOT = "install"


class LicenseState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The stored licence key and the latest date this install has seen."""

    __tablename__ = "license_state"
    __table_args__ = (UniqueConstraint("slot", name="uq_license_state_slot"),)

    slot: Mapped[str] = mapped_column(
        String(16), nullable=False, default=INSTALL_SLOT, server_default=INSTALL_SLOT
    )
    #: A key pasted by an admin or fetched by the licence refresh (LIC-27).
    #: ``APP_LICENSE_KEY`` is the other source; the valid key expiring last wins.
    key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``admin`` or ``refresh``.
    key_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Latest UTC date seen at sign-in. Expiry is evaluated against this when
    #: the clock reads earlier, so setting the clock back has no effect.
    clock_high_water: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: First sign-in date on a host the key in force is not bound to (LIC-29).
    #: Its grace period counts from here; a sign-in on a bound host clears it.
    host_mismatch_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Last non-loopback host seen at sign-in; the refresh reports it (LIC-27).
    last_host: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The licence refresh (LIC-27) and heartbeat (LIC-6): when each was last
    # tried and succeeded, why it failed, and the body as sent (LIC-21).
    refresh_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_succeeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_payload: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    heartbeat_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_payload: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    #: The latest verified revocation list from the licence refresh (LIC-8).
    revocations: Mapped[str | None] = mapped_column(Text, nullable=True)
