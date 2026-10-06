"""Tests for the SQLAlchemy ORM layer.

Everything here runs against ``Base.metadata`` and the mapper configuration
only: no engine, no connection, no live PostgreSQL. This suite exists to catch
drift between three things that must always agree:

1. ``docs/CONTRACTS.md`` (the "Data model" section, authoritative)
2. ``app/models/*.py`` (the SQLAlchemy models)
3. ``alembic/versions/0001_initial.py`` (the initial migration)
"""

from __future__ import annotations

import ast
import enum
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import Table

from app.db.base import Base
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    AuditEvent,
    Comment,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    ItemStatus,
    Job,
    JobStatus,
    JobType,
    LabelSchema,
    LabelSchemaVersion,
    MediaType,
    Membership,
    MembershipSource,
    MlPlatformKind,
    Model,
    ModelTask,
    ModelVersion,
    Notification,
    NotificationType,
    Organization,
    OutboxEvent,
    Project,
    ProjectRole,
    Snapshot,
    Task,
    TaskStatus,
    TaskType,
    User,
    Webhook,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookFormat,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MIGRATION_PATH = MIGRATIONS_DIR / "0001_initial.py"

# All model classes re-exported from ``app.models``, by the public name used
# in ``from app.models import X``.
MODEL_CLASSES: dict[str, type] = {
    "Annotation": Annotation,
    "AuditEvent": AuditEvent,
    "Comment": Comment,
    "Connector": Connector,
    "Item": Item,
    "Job": Job,
    "LabelSchema": LabelSchema,
    "LabelSchemaVersion": LabelSchemaVersion,
    "Membership": Membership,
    "Model": Model,
    "ModelVersion": ModelVersion,
    "Notification": Notification,
    "Organization": Organization,
    "OutboxEvent": OutboxEvent,
    "Project": Project,
    "Snapshot": Snapshot,
    "Task": Task,
    "User": User,
    "Webhook": Webhook,
    "WebhookDelivery": WebhookDelivery,
}

# Contract enum values (docs/CONTRACTS.md, "Data model" §9), written as
# literals so drift in the enum is actually caught rather than re-derived.
EXPECTED_ENUM_VALUES: dict[type[enum.Enum], frozenset[str]] = {
    ProjectRole: frozenset({"owner", "annotator", "reviewer", "viewer"}),
    MembershipSource: frozenset({"manual", "idp"}),
    ConnectorType: frozenset(
        {"azure_blob", "s3", "gcs", "local", "http", "sharepoint", "databricks_volume"}
    ),
    ConnectorIdentity: frozenset(
        {
            "managed_identity",
            "service_principal",
            "account_key",
            "sas_token",
            "iam_role",
            "access_key",
            "none",
        }
    ),
    MediaType: frozenset({"image", "video", "audio", "text", "pdf", "llm", "timeseries"}),
    ItemStatus: frozenset(
        {
            "new",
            "prelabeled",
            "annotating",
            "submitted",
            "in_review",
            "approved",
            "rejected",
            "skipped",
        }
    ),
    TaskType: frozenset({"annotate", "review"}),
    TaskStatus: frozenset({"open", "in_progress", "done", "cancelled"}),
    AnnotationSource: frozenset({"human", "model"}),
    AnnotationStatus: frozenset({"draft", "submitted", "approved", "rejected"}),
    AnnotationKind: frozenset({"primary", "consensus", "gold"}),
    JobType: frozenset(
        {
            "scan_source",
            "tile_image",
            "prelabel",
            "export",
            "snapshot",
            "import",
            "thumbnail",
            "rebuild_cache",
            "extract_text",
        }
    ),
    JobStatus: frozenset({"queued", "running", "succeeded", "failed", "cancelled"}),
    ModelTask: frozenset({"detect", "segment", "classify", "ner", "llm", "ocr"}),
    MlPlatformKind: frozenset({"mlflow", "databricks", "azureml"}),
    NotificationType: frozenset({"mention", "reply", "review"}),
    WebhookDeliveryStatus: frozenset({"pending", "succeeded", "failed"}),
    WebhookFormat: frozenset({"json", "slack", "teams"}),
}

# Every table from docs/CONTRACTS.md §9, mapped to {column_name: nullable}.
CONTRACT_COLUMNS: dict[str, dict[str, bool]] = {
    "organization": {
        "id": False,
        "name": False,
        "slug": False,
        "scim_token_hash": True,
        "created_at": False,
        "updated_at": False,
    },
    "user": {
        "id": False,
        "organization_id": False,
        "email": False,
        "display_name": False,
        "idp_subject": True,
        "password_hash": True,
        "is_active": False,
        "is_superuser": False,
        "is_service": False,
        "last_seen_at": True,
        "totp_secret": True,
        "totp_enabled_at": True,
        "totp_last_step": True,
        "mfa_recovery_codes": True,
        "erased_at": True,
        "scim_external_id": True,
        "scim_deleted_at": True,
        "email_notifications": False,
        "created_at": False,
        "updated_at": False,
    },
    "membership": {
        "id": False,
        "user_id": False,
        "project_id": False,
        "role": False,
        "source": False,
        "path_prefixes": True,
        "created_at": False,
    },
    "scim_group": {
        "id": False,
        "organization_id": False,
        "display_name": False,
        "external_id": True,
        "created_at": False,
        "updated_at": False,
    },
    "scim_group_member": {
        "id": False,
        "group_id": False,
        "user_id": False,
        "created_at": False,
    },
    "license_state": {
        "id": False,
        "slot": False,
        "key": True,
        "key_source": True,
        "clock_high_water": True,
        "host_mismatch_since": True,
        "last_host": True,
        "refresh_attempted_at": True,
        "refresh_succeeded_at": True,
        "refresh_error": True,
        "refresh_payload": True,
        "heartbeat_attempted_at": True,
        "heartbeat_sent_at": True,
        "heartbeat_error": True,
        "heartbeat_payload": True,
        "revocations": True,
        "created_at": False,
        "updated_at": False,
    },
    "idempotency_key": {
        "id": False,
        "organization_id": False,
        "endpoint": False,
        "key": False,
        "target_id": False,
        "created_at": False,
    },
    "api_key": {
        "id": False,
        "organization_id": False,
        "user_id": False,
        "name": False,
        "scopes": False,
        "token_prefix": True,
        "token_hash": True,
        "expires_at": True,
        "last_used_at": True,
        "revoked_at": True,
        "created_by": True,
        "created_at": False,
    },
    "connector": {
        "id": False,
        "organization_id": False,
        "name": False,
        "type": False,
        "identity_type": False,
        "secret_ref": True,
        "config": False,
        "event_token_hash": True,
        "created_at": False,
        "updated_at": False,
    },
    "label_schema": {
        "id": False,
        "project_id": False,
        "name": False,
        "created_at": False,
        "updated_at": False,
    },
    "label_schema_version": {
        "id": False,
        "label_schema_id": False,
        "version": False,
        "definition": False,
        "created_at": False,
    },
    "project": {
        "id": False,
        "organization_id": False,
        "name": False,
        "description": True,
        "label_schema_id": True,
        "source_connector_id": True,
        "result_connector_id": True,
        "cache_connector_id": True,
        "source_prefix": True,
        "source_glob": True,
        "workflow": False,
        "settings": False,
        "created_at": False,
        "updated_at": False,
    },
    "item": {
        "id": False,
        "project_id": False,
        "connector_id": False,
        "path": False,
        "media_type": False,
        "etag": True,
        "size_bytes": False,
        "width": True,
        "height": True,
        "meta": False,
        "status": False,
        "thumbnail_path": True,
        "created_at": False,
        "updated_at": False,
    },
    "task": {
        "id": False,
        "item_id": False,
        "project_id": False,
        "type": False,
        "assignee_id": True,
        "status": False,
        "locked_by_id": True,
        "locked_until": True,
        "priority": False,
        "deadline": True,
        "slot": True,
        "region": True,
        "gold": False,
        "created_at": False,
        "updated_at": False,
    },
    "annotation": {
        "id": False,
        "item_id": False,
        "task_id": True,
        "version": False,
        "author_user_id": True,
        "author_model_version_id": True,
        "source": False,
        "label_schema_version_id": False,
        "result": False,
        "status": False,
        "duration_ms": True,
        "blob_path": True,
        "kind": False,
        "created_at": False,
    },
    "comment": {
        "id": False,
        "project_id": False,
        "item_id": True,
        "annotation_id": True,
        "parent_id": True,
        "author_id": False,
        "body": False,
        "anchor": True,
        "resolved_at": True,
        "created_at": False,
        "updated_at": False,
    },
    "model": {
        "id": False,
        "organization_id": False,
        "name": False,
        "task": False,
        "endpoint_url": True,
        "identity_type": False,
        "secret_ref": True,
        "identity_config": False,
        "created_at": False,
        "deleted_at": True,
    },
    "ml_platform": {
        "id": False,
        "organization_id": False,
        "name": False,
        "kind": False,
        "tracking_uri": False,
        "identity_type": False,
        "secret_ref": True,
        "config": False,
        "created_at": False,
        "updated_at": False,
    },
    "model_version": {
        "id": False,
        "model_id": False,
        "version": False,
        "class_mapping": False,
        "metrics": False,
        "snapshot_id": True,
        "snapshot_digest": True,
        "training_run": True,
        "parent_version_id": True,
        "derivation": True,
        "created_at": False,
    },
    "job": {
        "id": False,
        "project_id": True,
        "type": False,
        "status": False,
        "progress": False,
        "payload": False,
        "result": True,
        "error": True,
        "attempts": False,
        "started_at": True,
        "finished_at": True,
        "created_at": False,
        "updated_at": False,
    },
    "snapshot": {
        "id": False,
        "project_id": False,
        "name": False,
        "filter": False,
        "split": True,
        "label_schema_version_id": False,
        "item_count": False,
        "blob_path": False,
        "digest": False,
        "created_by_id": False,
        "created_at": False,
    },
    "webhook": {
        "id": False,
        "organization_id": False,
        "project_id": True,
        "url": False,
        "description": True,
        "events": False,
        "secret": False,
        "is_active": False,
        "created_by_id": True,
        "last_delivery_at": True,
        "last_response_status": True,
        "format": False,
        "created_at": False,
        "updated_at": False,
    },
    "webhook_delivery": {
        "id": False,
        "webhook_id": False,
        "event": False,
        "payload": False,
        "status": False,
        "attempts": False,
        "next_attempt_at": False,
        "response_status": True,
        "error": True,
        "delivered_at": True,
        "created_at": False,
    },
    "audit_event": {
        "id": False,
        "organization_id": False,
        "actor_id": True,
        "action": False,
        "target_type": False,
        "target_id": True,
        "ip": True,
        "before": True,
        "after": True,
        "created_at": False,
    },
    "outbox_event": {
        "id": False,
        "aggregate_type": False,
        "aggregate_id": False,
        "type": False,
        "payload": False,
        "published_at": True,
        "attempts": False,
        "created_at": False,
    },
    "notification": {
        "id": False,
        "user_id": False,
        "type": False,
        "payload": False,
        "read_at": True,
        "emailed_at": True,
        "email_attempts": False,
        "created_at": False,
    },
}

CONTRACT_TABLES = tuple(sorted(CONTRACT_COLUMNS))

# Tables using TimestampMixin (timezone-aware created_at + updated_at).
TIMESTAMPED_TABLES = frozenset(
    {
        "organization",
        "user",
        "connector",
        "project",
        "label_schema",
        "item",
        "task",
        "comment",
        "job",
    }
)

# (table, column) pairs the contract marks as JSON, which must map to JSONB.
JSONB_COLUMNS: tuple[tuple[str, str], ...] = (
    ("connector", "config"),
    ("project", "workflow"),
    ("project", "settings"),
    ("item", "meta"),
    ("annotation", "result"),
    ("comment", "anchor"),
    ("ml_platform", "config"),
    ("model_version", "class_mapping"),
    ("model_version", "metrics"),
    ("model_version", "training_run"),
    ("job", "payload"),
    ("job", "result"),
    ("snapshot", "filter"),
    ("audit_event", "before"),
    ("audit_event", "after"),
    ("outbox_event", "payload"),
    ("label_schema_version", "definition"),
    ("notification", "payload"),
)

# (table, column, target_table) for every foreign key in the schema.
FOREIGN_KEY_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("user", "organization_id", "organization"),
    ("membership", "user_id", "user"),
    ("membership", "project_id", "project"),
    ("connector", "organization_id", "organization"),
    ("project", "organization_id", "organization"),
    ("project", "label_schema_id", "label_schema"),
    ("project", "source_connector_id", "connector"),
    ("project", "result_connector_id", "connector"),
    ("project", "cache_connector_id", "connector"),
    ("label_schema", "project_id", "project"),
    ("label_schema_version", "label_schema_id", "label_schema"),
    ("item", "project_id", "project"),
    ("item", "connector_id", "connector"),
    ("task", "item_id", "item"),
    ("task", "project_id", "project"),
    ("task", "assignee_id", "user"),
    ("task", "locked_by_id", "user"),
    ("annotation", "item_id", "item"),
    ("annotation", "task_id", "task"),
    ("annotation", "author_user_id", "user"),
    ("annotation", "author_model_version_id", "model_version"),
    ("annotation", "label_schema_version_id", "label_schema_version"),
    ("comment", "project_id", "project"),
    ("comment", "item_id", "item"),
    ("comment", "annotation_id", "annotation"),
    ("comment", "parent_id", "comment"),
    ("comment", "author_id", "user"),
    ("model", "organization_id", "organization"),
    ("ml_platform", "organization_id", "organization"),
    ("model_version", "model_id", "model"),
    ("model_version", "snapshot_id", "snapshot"),
    ("job", "project_id", "project"),
    ("snapshot", "project_id", "project"),
    ("snapshot", "label_schema_version_id", "label_schema_version"),
    ("snapshot", "created_by_id", "user"),
    ("audit_event", "organization_id", "organization"),
    ("audit_event", "actor_id", "user"),
    ("notification", "user_id", "user"),
)

# Every foreign-key column, per the contract, must be indexed for join
# performance. Derived from FOREIGN_KEY_TARGETS: the (table, column) pairs.
INDEXED_FK_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (table, column) for table, column, _ in FOREIGN_KEY_TARGETS
)


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _flatten_contract_columns() -> list[tuple[str, str, bool]]:
    return [
        (table, column, nullable)
        for table, columns in CONTRACT_COLUMNS.items()
        for column, nullable in columns.items()
    ]


def _parse_migration_module(path: Path = MIGRATION_PATH) -> ast.Module:
    source = path.read_text()
    return ast.parse(source, filename=str(path))


def _all_migration_paths() -> list[Path]:
    """Every migration file in `alembic/versions/`, in filename order."""
    return sorted(MIGRATIONS_DIR.glob("*.py"))


def _find_upgrade_function(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    raise AssertionError("0001_initial.py has no upgrade() function to parse")


def _is_call_to(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    )


def _tables_created_by_migration(upgrade: ast.FunctionDef) -> set[str]:
    tables: set[str] = set()
    for node in ast.walk(upgrade):
        if (
            _is_call_to(node, "create_table")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            tables.add(node.args[0].value)
    return tables


def _pgcrypto_extension_created(upgrade: ast.FunctionDef) -> bool:
    for node in ast.walk(upgrade):
        if not _is_call_to(node, "execute"):
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and "pgcrypto" in arg.value
            ):
                return True
    return False


class TestModelsImportAndMap:
    """Every model imports cleanly and the mapper graph configures without error."""

    @pytest.mark.parametrize("model_name", sorted(MODEL_CLASSES))
    def test_model_importable_from_app_models(self, model_name: str) -> None:
        model_cls = MODEL_CLASSES[model_name]
        assert model_cls.__name__ == model_name, (
            f"app.models.{model_name} resolves to a class named {model_cls.__name__!r}"
        )

    def test_configure_mappers_succeeds(self) -> None:
        # This is the check that actually catches a broken relationship: it
        # only fails at first query otherwise, never at import time.
        configure_mappers()

    def test_every_mapped_class_has_a_table_in_metadata(self) -> None:
        for name, model_cls in MODEL_CLASSES.items():
            table_name = model_cls.__tablename__  # type: ignore[attr-defined]
            assert table_name in Base.metadata.tables, (
                f"{name}.__tablename__ = {table_name!r} has no entry in Base.metadata.tables"
            )


class TestEnums:
    """Enum value sets must match docs/CONTRACTS.md exactly, in both directions."""

    @pytest.mark.parametrize(
        ("enum_cls", "expected"),
        EXPECTED_ENUM_VALUES.items(),
        ids=[cls.__name__ for cls in EXPECTED_ENUM_VALUES],
    )
    def test_enum_values_match_contract(
        self, enum_cls: type[enum.Enum], expected: frozenset[str]
    ) -> None:
        actual = frozenset(member.value for member in enum_cls)
        missing = expected - actual
        extra = actual - expected
        assert actual == expected, (
            f"{enum_cls.__name__} values drifted from CONTRACTS.md: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    @pytest.mark.parametrize(
        "enum_cls", EXPECTED_ENUM_VALUES, ids=[cls.__name__ for cls in EXPECTED_ENUM_VALUES]
    )
    def test_enum_is_a_str_enum(self, enum_cls: type[enum.Enum]) -> None:
        assert issubclass(enum_cls, str), f"{enum_cls.__name__} must be a StrEnum per CONTRACTS.md"


class TestTables:
    """Tables, columns, nullability, and JSONB typing must match the contract."""

    @pytest.mark.parametrize("table_name", CONTRACT_TABLES)
    def test_table_exists_with_contract_name(self, table_name: str) -> None:
        assert table_name in Base.metadata.tables, (
            f"table {table_name!r} from CONTRACTS.md is missing from Base.metadata"
        )

    def test_no_undocumented_tables(self) -> None:
        actual = set(Base.metadata.tables)
        expected = set(CONTRACT_TABLES)
        extra = actual - expected
        assert extra == set(), f"tables present in models but not in CONTRACTS.md: {sorted(extra)}"

    @pytest.mark.parametrize("table_name", CONTRACT_TABLES)
    def test_table_has_id_primary_key(self, table_name: str) -> None:
        table = _table(table_name)
        assert "id" in table.columns, f"{table_name} has no id column"
        assert table.c.id.primary_key, f"{table_name}.id is not the primary key"

    @pytest.mark.parametrize(
        ("table_name", "column_name", "expected_nullable"),
        _flatten_contract_columns(),
        ids=[f"{t}.{c}" for t, c, _ in _flatten_contract_columns()],
    )
    def test_column_exists_with_contract_nullability(
        self, table_name: str, column_name: str, expected_nullable: bool
    ) -> None:
        table = _table(table_name)
        assert column_name in table.columns, f"{table_name}.{column_name} is missing from the model"
        actual_nullable = table.columns[column_name].nullable
        assert actual_nullable is expected_nullable, (
            f"{table_name}.{column_name}.nullable = {actual_nullable}, "
            f"CONTRACTS.md requires {expected_nullable}"
        )

    @pytest.mark.parametrize("table_name", sorted(CONTRACT_COLUMNS))
    def test_no_undocumented_columns(self, table_name: str) -> None:
        table = _table(table_name)
        actual = set(table.columns.keys())
        expected = set(CONTRACT_COLUMNS[table_name])
        extra = actual - expected
        assert extra == set(), f"{table_name} has columns not in CONTRACTS.md: {sorted(extra)}"

    @pytest.mark.parametrize("table_name", sorted(TIMESTAMPED_TABLES))
    def test_timestamped_tables_have_timezone_aware_timestamps(self, table_name: str) -> None:
        table = _table(table_name)
        for column_name in ("created_at", "updated_at"):
            column = table.columns[column_name]
            assert isinstance(column.type, sa.DateTime), (
                f"{table_name}.{column_name} is {column.type!r}, expected DateTime"
            )
            assert column.type.timezone is True, (
                f"{table_name}.{column_name} is not timezone-aware (TIMESTAMP WITH TIME ZONE)"
            )
            assert column.nullable is False, f"{table_name}.{column_name} must be NOT NULL"

    @pytest.mark.parametrize(
        ("table_name", "column_name"), JSONB_COLUMNS, ids=[f"{t}.{c}" for t, c in JSONB_COLUMNS]
    )
    def test_json_columns_are_jsonb(self, table_name: str, column_name: str) -> None:
        table = _table(table_name)
        column_type = table.columns[column_name].type
        # JSONBType is PGJSONB().with_variant(JSON(), "sqlite"): the type
        # object itself is a JSONB instance (with a variant registered for
        # sqlite), so a direct isinstance check reflects what PostgreSQL sees.
        assert isinstance(column_type, JSONB), (
            f"{table_name}.{column_name} is {column_type!r}, expected JSONB"
        )

    def test_user_email_is_citext(self) -> None:
        assert isinstance(_table("user").c.email.type, CITEXT), (
            "user.email must be CITEXT per CONTRACTS.md ('email (unique, citext)')"
        )


class TestConstraints:
    """Constraints that protect data integrity."""

    def test_item_unique_on_project_connector_path(self) -> None:
        table = _table("item")
        indexes = {idx.name: idx for idx in table.indexes}
        idx = indexes.get("uq_item_project_connector_path")
        assert idx is not None, (
            "item is missing the unique index on (project_id, connector_id, path)"
        )
        assert idx.unique, "uq_item_project_connector_path must be unique"
        assert [c.name for c in idx.columns] == ["project_id", "connector_id", "path"], (
            f"uq_item_project_connector_path covers {[c.name for c in idx.columns]}, "
            "expected [project_id, connector_id, path]"
        )

    def test_annotation_unique_on_item_and_version(self) -> None:
        table = _table("annotation")
        indexes = {idx.name: idx for idx in table.indexes}
        idx = indexes.get("uq_annotation_item_version")
        assert idx is not None, "annotation is missing the unique index on (item_id, version)"
        assert idx.unique, "uq_annotation_item_version must be unique"
        assert [c.name for c in idx.columns] == ["item_id", "version"], (
            f"uq_annotation_item_version covers {[c.name for c in idx.columns]}, "
            "expected [item_id, version]"
        )

    def test_label_schema_version_unique_on_schema_and_version(self) -> None:
        table = _table("label_schema_version")
        indexes = {idx.name: idx for idx in table.indexes}
        idx = indexes.get("uq_label_schema_version_schema_version")
        assert idx is not None, (
            "label_schema_version is missing the unique index on (label_schema_id, version)"
        )
        assert idx.unique, "uq_label_schema_version_schema_version must be unique"
        assert [c.name for c in idx.columns] == ["label_schema_id", "version"], (
            f"uq_label_schema_version_schema_version covers {[c.name for c in idx.columns]}, "
            "expected [label_schema_id, version]"
        )

    def test_user_email_is_unique(self) -> None:
        assert _table("user").c.email.unique is True, "user.email must be UNIQUE per CONTRACTS.md"

    def test_organization_slug_is_unique(self) -> None:
        assert _table("organization").c.slug.unique is True, (
            "organization.slug must be UNIQUE per CONTRACTS.md"
        )

    def test_annotation_author_xor_check_constraint(self) -> None:
        table = _table("annotation")
        checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
        named = {c.name: c for c in checks}
        constraint = named.get("ck_annotation_author_xor")
        assert constraint is not None, (
            "annotation is missing the ck_annotation_author_xor CHECK constraint"
        )
        sqltext = str(constraint.sqltext)
        assert "author_user_id" in sqltext and "author_model_version_id" in sqltext, (
            f"ck_annotation_author_xor does not reference both author columns: {sqltext!r}"
        )
        # XOR, not "at least one": both a plain AND and a plain OR would let
        # zero-or-both authors slip through.
        assert "<>" in sqltext or "!=" in sqltext, (
            f"ck_annotation_author_xor does not look like an XOR (exactly-one) check: {sqltext!r}"
        )

    @pytest.mark.parametrize(
        ("table_name", "column_name", "target_table"),
        FOREIGN_KEY_TARGETS,
        ids=[f"{t}.{c}->{tgt}" for t, c, tgt in FOREIGN_KEY_TARGETS],
    )
    def test_foreign_key_points_at_expected_table(
        self, table_name: str, column_name: str, target_table: str
    ) -> None:
        column = _table(table_name).columns[column_name]
        fks = list(column.foreign_keys)
        assert fks, (
            f"{table_name}.{column_name} has no ForeignKey, expected one to {target_table!r}"
        )
        actual_targets = {fk.column.table.name for fk in fks}
        assert actual_targets == {target_table}, (
            f"{table_name}.{column_name} points at {sorted(actual_targets)}, "
            f"expected [{target_table}]"
        )


class TestIndexes:
    """Indexes the contract calls out for query performance."""

    @pytest.mark.parametrize(
        ("table_name", "column_name"),
        INDEXED_FK_COLUMNS,
        ids=[f"{t}.{c}" for t, c in INDEXED_FK_COLUMNS],
    )
    def test_foreign_key_column_is_indexed(self, table_name: str, column_name: str) -> None:
        table = _table(table_name)
        column = table.columns[column_name]
        covered_by_composite = any(
            column.name == idx.columns[0].name for idx in table.indexes if len(idx.columns) > 1
        )
        assert column.index or covered_by_composite, (
            f"{table_name}.{column_name} is a foreign key but has no index"
        )

    def test_item_project_status_index(self) -> None:
        table = _table("item")
        names = {idx.name: idx for idx in table.indexes}
        idx = names.get("ix_item_project_id_status")
        assert idx is not None, "item is missing the (project_id, status) index"
        assert [c.name for c in idx.columns] == ["project_id", "status"]

    def test_task_project_status_assignee_index(self) -> None:
        table = _table("task")
        names = {idx.name: idx for idx in table.indexes}
        idx = names.get("ix_task_project_id_status_assignee_id")
        assert idx is not None, "task is missing the (project_id, status, assignee_id) index"
        assert [c.name for c in idx.columns] == ["project_id", "status", "assignee_id"]

    def test_annotation_item_version_index(self) -> None:
        table = _table("annotation")
        names = {idx.name: idx for idx in table.indexes}
        idx = names.get("uq_annotation_item_version")
        assert idx is not None, "annotation is missing the (item_id, version) index"
        assert [c.name for c in idx.columns] == ["item_id", "version"]

    def test_audit_event_organization_created_at_index(self) -> None:
        table = _table("audit_event")
        names = {idx.name: idx for idx in table.indexes}
        idx = names.get("ix_audit_event_organization_id_created_at")
        assert idx is not None, "audit_event is missing the (organization_id, created_at) index"
        assert [c.name for c in idx.columns] == ["organization_id", "created_at"]

    def test_notification_user_id_read_at_index(self) -> None:
        table = _table("notification")
        names = {idx.name: idx for idx in table.indexes}
        idx = names.get("ix_notification_user_id_read_at")
        assert idx is not None, "notification is missing the (user_id, read_at) index"
        assert [c.name for c in idx.columns] == ["user_id", "read_at"]

    def test_outbox_event_published_at_index(self) -> None:
        column = _table("outbox_event").c.published_at
        has_single_column_index = column.index is True
        has_composite_index = any(
            column.name in {c.name for c in idx.columns} for idx in _table("outbox_event").indexes
        )
        assert has_single_column_index or has_composite_index, (
            "outbox_event.published_at is not indexed, needed to find unpublished rows"
        )


class TestMigration:
    """The handwritten migrations must match the models, table-for-table.

    Tables are introduced across several revisions (0001, 0002, ...), so the
    set the models expect is compared against the *union* of tables created
    by every file in `alembic/versions/`, not just the initial one.
    """

    def test_migration_file_exists(self) -> None:
        assert MIGRATION_PATH.is_file(), f"expected migration at {MIGRATION_PATH}"

    def test_migration_tables_match_model_metadata(self) -> None:
        migration_tables: set[str] = set()
        for path in _all_migration_paths():
            tree = _parse_migration_module(path)
            upgrade = _find_upgrade_function(tree)
            migration_tables |= _tables_created_by_migration(upgrade)
        model_tables = set(Base.metadata.tables)
        missing_from_migration = model_tables - migration_tables
        missing_from_models = migration_tables - model_tables
        assert migration_tables == model_tables, (
            f"migration/model table mismatch: "
            f"in models but not migration={sorted(missing_from_migration)}, "
            f"in migration but not models={sorted(missing_from_models)}"
        )

    def test_migration_creates_pgcrypto_extension(self) -> None:
        # pgcrypto only needs to exist once; the initial migration is the one
        # that creates it, so this check stays pinned to 0001.
        tree = _parse_migration_module(MIGRATION_PATH)
        upgrade = _find_upgrade_function(tree)
        assert _pgcrypto_extension_created(upgrade), (
            "0001_initial.py never creates the pgcrypto extension, "
            "but UUIDPrimaryKeyMixin's server_default depends on gen_random_uuid()"
        )
