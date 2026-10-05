"""Reusable column types shared across ORM models."""

from __future__ import annotations

import enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import INET as PGINET
from sqlalchemy.dialects.postgresql import JSONB as PGJSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.types import TypeEngine

#: JSONB on PostgreSQL, falling back to plain JSON on SQLite so models can be
#: exercised (metadata-only) in tests without a live PostgreSQL instance.
JSONBType: TypeEngine[Any] = PGJSONB().with_variant(JSON(), "sqlite")

#: Standard UUID column type, stored as a native Python ``uuid.UUID``.
UUIDType: TypeEngine[Any] = PGUUID(as_uuid=True)

#: INET on PostgreSQL, falling back to a plain string on SQLite (which has no
#: native network-address type) so audit_event can be created in the SQLite
#: test suite. Behaviour on PostgreSQL is unchanged.
InetType: TypeEngine[Any] = PGINET().with_variant(String(45), "sqlite")


def pg_enum(enum_cls: type[enum.Enum], name: str) -> sa.Enum:
    """Build a native PostgreSQL enum type named ``name`` backed by ``enum_cls``.

    Stores the enum members' ``.value`` (not their Python attribute name) in
    the database, matching the value lists in ``docs/CONTRACTS.md``.
    """
    values: list[str] = [member.value for member in enum_cls]
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda _: values,
    )
