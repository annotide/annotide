"""Tests for the task-claiming and locking logic in `app.services.tasks`.

Most of this module needs no database (per `tests/conftest.py`):
`claim_next_task`'s query construction is exercised against a fake session
that records the statement it was asked to execute, so the race-free claim
query can be inspected by compiling it — exactly as it would run against
PostgreSQL — rather than by running it, and `release_task` / `extend_lock` /
`is_lock_expired` work against plain `Task` ORM instances or a fake session.

`open_task`, `complete_task` and the `task_type` filter on `claim_next_task`
read and filter real rows (`WHERE item_id = ...`, `WHERE type = ...`), which a
fake session that just returns whatever rows it was seeded with cannot
exercise honestly — a `task_type` bug that ignored the filter entirely would
still pass a fake-session test. Those are run against a real in-memory
SQLite `task` table instead (see `TestOpenTask`, `TestCompleteTask`, and
`TestClaimNextTaskTypeFilter`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Select, Table, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.errors import ConflictError, TaskLockedError
from app.db.base import Base
from app.models import Annotation, Task, TaskStatus, TaskType
from app.services.tasks import (
    claim_next_task,
    complete_task,
    extend_lock,
    is_lock_expired,
    open_task,
    reap_expired_locks,
    release_task,
    update_task,
)

LOCK_TTL = 1800


def _make_task(**overrides: Any) -> Task:
    """Build a bare `Task` ORM instance; no session or database involved."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "item_id": uuid4(),
        "project_id": uuid4(),
        "type": TaskType.ANNOTATE,
        "assignee_id": None,
        "status": TaskStatus.OPEN,
        "locked_by_id": None,
        "locked_until": None,
        "priority": 0,
        "deadline": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Task(**defaults)


class _FakeScalars:
    def __init__(self, rows: list[Task]) -> None:
        self._rows = rows

    def first(self) -> Task | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[Task]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


@dataclass
class FakeSession:
    """Stands in for `AsyncSession`: no engine, no I/O, just recorded calls."""

    rows: list[Task] = field(default_factory=list)
    executed: list[Select[Any]] = field(default_factory=list)
    committed: bool = False
    refreshed: list[Task] = field(default_factory=list)

    async def execute(self, stmt: Select[Any]) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult(self.rows)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: Task) -> None:
        self.refreshed.append(obj)

    def as_session(self) -> AsyncSession:
        """Narrow to `AsyncSession` for callers that are typed against it.

        `FakeSession` deliberately does not subclass `AsyncSession` (it would
        have to stub out dozens of unrelated methods); the functions under
        test only ever call `execute`, `commit` and `refresh` on it, so this
        cast documents that narrower, actually-used contract at the one
        boundary that needs it.
        """
        return cast(AsyncSession, self)


class TestClaimQueryConstruction:
    """The claim query is what makes concurrent claiming race-free, so its
    exact shape — locking clause and ordering — is asserted directly."""

    async def _compiled(self, task: Task | None) -> str:
        session = FakeSession(rows=[task] if task else [])
        await claim_next_task(session.as_session(), uuid4(), uuid4(), LOCK_TTL)
        assert len(session.executed) == 1
        # Compiled against the PostgreSQL dialect: `skip_locked` only renders
        # for dialects that support it, and this is the dialect the app
        # actually runs against in production.
        # sqlalchemy.dialects.postgresql resolves `dialect` via a lazy
        # module-level __getattr__, which mypy cannot type — same rationale
        # as the `type: ignore` on `aioredis.from_url` in api/v1/health.py.
        dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
        return str(session.executed[0].compile(dialect=dialect))

    async def test_uses_for_update_skip_locked(self) -> None:
        compiled = await self._compiled(_make_task())
        assert "FOR UPDATE" in compiled
        assert "SKIP LOCKED" in compiled

    async def test_orders_by_priority_first(self) -> None:
        compiled = await self._compiled(_make_task())
        order_by_clause = compiled.split("ORDER BY", 1)[1]
        priority_pos = order_by_clause.index("priority")
        deadline_pos = order_by_clause.index("deadline")
        created_at_pos = order_by_clause.index("created_at")
        assert priority_pos < deadline_pos < created_at_pos
        assert "priority DESC" in order_by_clause
        assert "deadline ASC NULLS LAST" in order_by_clause
        assert "created_at ASC" in order_by_clause

    async def test_filters_open_status_and_lock_state(self) -> None:
        compiled = await self._compiled(_make_task())
        assert "task.status = " in compiled
        assert "task.locked_until IS NULL" in compiled
        assert "task.locked_until <" in compiled
        assert "task.assignee_id IS NULL" in compiled


class TestClaimNextTask:
    async def test_returns_none_on_empty_queue(self) -> None:
        session = FakeSession(rows=[])
        result = await claim_next_task(session.as_session(), uuid4(), uuid4(), LOCK_TTL)
        assert result is None
        assert session.committed is False

    async def test_claim_stamps_lock_and_assigns(self) -> None:
        task = _make_task()
        session = FakeSession(rows=[task])
        user_id = uuid4()

        before = datetime.now(UTC)
        claimed = await claim_next_task(session.as_session(), task.project_id, user_id, LOCK_TTL)
        after = datetime.now(UTC)

        assert claimed is task
        assert claimed.locked_by_id == user_id
        assert claimed.assignee_id == user_id
        assert claimed.status is TaskStatus.IN_PROGRESS
        assert claimed.locked_until is not None
        assert before + timedelta(seconds=LOCK_TTL) <= claimed.locked_until
        assert claimed.locked_until <= after + timedelta(seconds=LOCK_TTL)
        assert session.committed is True
        assert session.refreshed == [task]

    async def test_claim_keeps_existing_assignee_when_claiming_for_them(self) -> None:
        user_id = uuid4()
        task = _make_task(assignee_id=user_id)
        session = FakeSession(rows=[task])

        claimed = await claim_next_task(session.as_session(), task.project_id, user_id, LOCK_TTL)

        assert claimed is not None
        assert claimed.assignee_id == user_id


class TestIsLockExpired:
    def test_no_lock_is_not_expired(self) -> None:
        task = _make_task(locked_until=None)
        assert is_lock_expired(task, datetime.now(UTC)) is False

    def test_future_lock_is_not_expired(self) -> None:
        now = datetime.now(UTC)
        task = _make_task(locked_until=now + timedelta(seconds=60))
        assert is_lock_expired(task, now) is False

    def test_past_lock_is_expired(self) -> None:
        now = datetime.now(UTC)
        task = _make_task(locked_until=now - timedelta(seconds=1))
        assert is_lock_expired(task, now) is True

    def test_boundary_instant_counts_as_expired(self) -> None:
        now = datetime.now(UTC)
        task = _make_task(locked_until=now)
        assert is_lock_expired(task, now) is True

    def test_one_microsecond_before_expiry_is_not_expired(self) -> None:
        now = datetime.now(UTC)
        task = _make_task(locked_until=now + timedelta(microseconds=1))
        assert is_lock_expired(task, now) is False


class TestReleaseTask:
    async def test_holder_releases_and_reopens(self) -> None:
        holder = uuid4()
        task = _make_task(
            locked_by_id=holder,
            locked_until=datetime.now(UTC) + timedelta(seconds=60),
            assignee_id=holder,
            status=TaskStatus.IN_PROGRESS,
        )
        session = FakeSession()

        released = await release_task(session.as_session(), task, holder)

        assert released.locked_by_id is None
        assert released.locked_until is None
        assert released.assignee_id is None
        assert released.status is TaskStatus.OPEN
        assert session.committed is True

    async def test_non_holder_is_refused(self) -> None:
        holder = uuid4()
        other = uuid4()
        task = _make_task(locked_by_id=holder, status=TaskStatus.IN_PROGRESS)
        session = FakeSession()

        with pytest.raises(TaskLockedError):
            await release_task(session.as_session(), task, other)

        # Refused before any mutation or commit.
        assert task.locked_by_id == holder
        assert session.committed is False

    async def test_superuser_may_release_anyone(self) -> None:
        holder = uuid4()
        superuser = uuid4()
        task = _make_task(locked_by_id=holder, status=TaskStatus.IN_PROGRESS)
        session = FakeSession()

        released = await release_task(session.as_session(), task, superuser, is_superuser=True)

        assert released.locked_by_id is None
        assert session.committed is True

    async def test_releasing_an_unlocked_task_is_a_noop_success(self) -> None:
        task = _make_task(status=TaskStatus.OPEN)
        session = FakeSession()

        released = await release_task(session.as_session(), task, uuid4())

        assert released.locked_by_id is None
        assert released.status is TaskStatus.OPEN
        assert session.committed is True


class TestUpdateTask:
    async def test_sets_only_given_fields(self) -> None:
        deadline = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
        task = _make_task(priority=1, deadline=deadline)
        session = FakeSession()

        updated = await update_task(session.as_session(), task, priority=5)

        assert updated.priority == 5
        assert updated.deadline == deadline
        assert session.committed is True

    async def test_none_clears_deadline_and_assignee(self) -> None:
        task = _make_task(deadline=datetime.now(UTC), assignee_id=uuid4())
        session = FakeSession()

        updated = await update_task(session.as_session(), task, deadline=None, assignee_id=None)

        assert updated.deadline is None
        assert updated.assignee_id is None

    async def test_done_task_is_refused(self) -> None:
        task = _make_task(status=TaskStatus.DONE)
        session = FakeSession()

        with pytest.raises(ConflictError):
            await update_task(session.as_session(), task, priority=5)
        assert session.committed is False

    async def test_reassigning_in_progress_task_is_refused(self) -> None:
        holder = uuid4()
        task = _make_task(status=TaskStatus.IN_PROGRESS, locked_by_id=holder, assignee_id=holder)
        session = FakeSession()

        with pytest.raises(ConflictError):
            await update_task(session.as_session(), task, assignee_id=uuid4())
        # Same assignee, or a priority bump, is fine while in progress.
        await update_task(session.as_session(), task, assignee_id=holder, priority=9)
        assert task.priority == 9


class TestExtendLock:
    async def test_holder_extends(self) -> None:
        holder = uuid4()
        task = _make_task(
            locked_by_id=holder, locked_until=datetime.now(UTC) + timedelta(seconds=10)
        )
        session = FakeSession()
        original_expiry = task.locked_until
        assert original_expiry is not None

        extended = await extend_lock(session.as_session(), task, holder, LOCK_TTL)

        assert extended.locked_until is not None
        assert extended.locked_until > original_expiry
        assert session.committed is True

    async def test_non_holder_is_refused(self) -> None:
        holder = uuid4()
        other = uuid4()
        task = _make_task(
            locked_by_id=holder, locked_until=datetime.now(UTC) + timedelta(seconds=10)
        )
        session = FakeSession()

        with pytest.raises(TaskLockedError):
            await extend_lock(session.as_session(), task, other, LOCK_TTL)

        assert session.committed is False

    async def test_unlocked_task_is_refused(self) -> None:
        task = _make_task(locked_by_id=None)
        session = FakeSession()

        with pytest.raises(TaskLockedError):
            await extend_lock(session.as_session(), task, uuid4(), LOCK_TTL)


# --------------------------------------------------------------------------- #
# open_task / complete_task / claim_next_task(task_type=...) — real SQLite
# --------------------------------------------------------------------------- #

_TASK_TABLE = cast("Table", Task.__table__)
#: `claim_next_task`'s query also excludes slot tasks by an `annotation` EXISTS
#: subquery (QA-1), so the table must exist even though these tests seed no rows.
_ANNOTATION_TABLE = cast("Table", Annotation.__table__)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A real, empty `task` table on in-memory SQLite for one test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[_TASK_TABLE, _ANNOTATION_TABLE])
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _count_tasks(session: AsyncSession, item_id: Any, task_type: TaskType) -> int:
    rows = await session.scalars(
        select(Task).where(Task.item_id == item_id, Task.type == task_type)
    )
    return len(list(rows))


class TestOpenTask:
    async def test_creates_an_open_task(self, db_session: AsyncSession) -> None:
        item_id = uuid4()
        project_id = uuid4()

        task = await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.ANNOTATE
        )
        # `status`'s default is applied by SQLAlchemy on flush, not on
        # construction — flush to see the value the row will actually carry.
        await db_session.flush()

        assert task is not None
        assert task.item_id == item_id
        assert task.project_id == project_id
        assert task.type is TaskType.ANNOTATE
        assert task.status is TaskStatus.OPEN

    async def test_does_not_commit(self, db_session: AsyncSession) -> None:
        item_id = uuid4()
        await open_task(
            db_session, item_id=item_id, project_id=uuid4(), task_type=TaskType.ANNOTATE
        )
        # A caller-owned transaction: the row exists in this session (flush is
        # implicit on the following query) but nothing forced a commit.
        assert await _count_tasks(db_session, item_id, TaskType.ANNOTATE) == 1
        await db_session.rollback()
        assert await _count_tasks(db_session, item_id, TaskType.ANNOTATE) == 0

    async def test_second_call_for_same_item_and_type_is_a_noop(
        self, db_session: AsyncSession
    ) -> None:
        item_id = uuid4()
        project_id = uuid4()
        first = await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()

        second = await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()

        assert first is not None
        assert second is None
        assert await _count_tasks(db_session, item_id, TaskType.ANNOTATE) == 1

    async def test_a_different_type_is_allowed(self, db_session: AsyncSession) -> None:
        item_id = uuid4()
        project_id = uuid4()
        await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()

        review = await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.REVIEW
        )
        await db_session.commit()

        assert review is not None
        assert await _count_tasks(db_session, item_id, TaskType.ANNOTATE) == 1
        assert await _count_tasks(db_session, item_id, TaskType.REVIEW) == 1

    async def test_assignee_id_is_stored(self, db_session: AsyncSession) -> None:
        assignee_id = uuid4()

        task = await open_task(
            db_session,
            item_id=uuid4(),
            project_id=uuid4(),
            task_type=TaskType.ANNOTATE,
            assignee_id=assignee_id,
        )

        assert task is not None
        assert task.assignee_id == assignee_id


class TestOpenTaskDeadline:
    async def test_stores_priority_and_deadline(self, db_session: AsyncSession) -> None:
        deadline = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)

        task = await open_task(
            db_session,
            item_id=uuid4(),
            project_id=uuid4(),
            task_type=TaskType.ANNOTATE,
            priority=3,
            deadline=deadline,
        )
        await db_session.flush()

        assert task is not None
        assert task.priority == 3
        assert task.deadline is not None
        assert task.deadline.replace(tzinfo=UTC) == deadline  # SQLite drops tz


class TestCompleteTask:
    async def test_marks_open_task_done_and_clears_lock(self, db_session: AsyncSession) -> None:
        item_id = uuid4()
        holder = uuid4()
        opened = await open_task(
            db_session, item_id=item_id, project_id=uuid4(), task_type=TaskType.ANNOTATE
        )
        assert opened is not None
        opened.status = TaskStatus.IN_PROGRESS
        opened.locked_by_id = holder
        opened.locked_until = datetime.now(UTC) + timedelta(minutes=30)
        await db_session.commit()

        completed = await complete_task(db_session, item_id=item_id, task_type=TaskType.ANNOTATE)
        await db_session.commit()

        assert completed is not None
        assert completed.id == opened.id
        assert completed.status is TaskStatus.DONE
        assert completed.locked_by_id is None
        assert completed.locked_until is None

    async def test_returns_none_when_nothing_is_open(self, db_session: AsyncSession) -> None:
        result = await complete_task(db_session, item_id=uuid4(), task_type=TaskType.ANNOTATE)
        assert result is None

    async def test_does_not_touch_a_different_type(self, db_session: AsyncSession) -> None:
        item_id = uuid4()
        project_id = uuid4()
        await open_task(
            db_session, item_id=item_id, project_id=project_id, task_type=TaskType.REVIEW
        )
        await db_session.commit()

        result = await complete_task(db_session, item_id=item_id, task_type=TaskType.ANNOTATE)

        assert result is None


class TestClaimNextTaskTypeFilter:
    async def test_task_type_review_skips_a_higher_priority_annotate_task(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        user_id = uuid4()
        await open_task(
            db_session,
            item_id=uuid4(),
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            priority=10,
        )
        review = await open_task(
            db_session,
            item_id=uuid4(),
            project_id=project_id,
            task_type=TaskType.REVIEW,
            priority=1,
        )
        await db_session.commit()
        assert review is not None

        claimed = await claim_next_task(
            db_session, project_id, user_id, LOCK_TTL, task_type=TaskType.REVIEW
        )

        assert claimed is not None
        assert claimed.id == review.id
        assert claimed.type is TaskType.REVIEW

    async def test_task_type_none_keeps_old_behaviour(self, db_session: AsyncSession) -> None:
        """Omitting `task_type` still claims the highest-priority task of any type."""
        project_id = uuid4()
        user_id = uuid4()
        annotate = await open_task(
            db_session,
            item_id=uuid4(),
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            priority=10,
        )
        await open_task(
            db_session,
            item_id=uuid4(),
            project_id=project_id,
            task_type=TaskType.REVIEW,
            priority=1,
        )
        await db_session.commit()
        assert annotate is not None

        claimed = await claim_next_task(db_session, project_id, user_id, LOCK_TTL)

        assert claimed is not None
        assert claimed.id == annotate.id


class TestClaimResumesOwnInProgressTask:
    """A reload skips the client's release-on-unmount; `claim_next_task` must
    hand the caller's own `in_progress` task back instead of "Queue is empty"
    (Known debt: "A reload loses your own task until the lock expires")."""

    async def test_own_in_progress_task_is_returned_before_open_ones(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        user_id = uuid4()
        mine = await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await open_task(
            db_session,
            item_id=uuid4(),
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            priority=100,
        )
        await db_session.commit()
        assert mine is not None

        first = await claim_next_task(db_session, project_id, user_id, LOCK_TTL)
        assert first is not None
        # The higher-priority task was claimed first; now "reload" and claim again.
        again = await claim_next_task(db_session, project_id, user_id, LOCK_TTL)

        assert again is not None
        assert again.id == first.id
        assert again.status is TaskStatus.IN_PROGRESS
        assert again.locked_by_id == user_id

    async def test_reclaim_renews_an_expired_lock(self, db_session: AsyncSession) -> None:
        project_id = uuid4()
        user_id = uuid4()
        task = await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()
        assert task is not None

        claimed = await claim_next_task(db_session, project_id, user_id, LOCK_TTL)
        assert claimed is not None
        claimed.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()

        again = await claim_next_task(db_session, project_id, user_id, LOCK_TTL)

        assert again is not None
        assert again.id == claimed.id
        assert again.locked_until is not None
        # SQLite hands the column back naive; compare on the UTC wall clock.
        renewed = again.locked_until.replace(tzinfo=UTC)
        assert renewed > datetime.now(UTC)

    async def test_another_users_in_progress_task_is_not_handed_out(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()

        theirs = await claim_next_task(db_session, project_id, uuid4(), LOCK_TTL)
        assert theirs is not None

        mine = await claim_next_task(db_session, project_id, uuid4(), LOCK_TTL)

        assert mine is None

    async def test_task_type_filter_applies_to_own_in_progress_task(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        user_id = uuid4()
        await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()

        claimed = await claim_next_task(
            db_session, project_id, user_id, LOCK_TTL, task_type=TaskType.ANNOTATE
        )
        assert claimed is not None

        as_reviewer = await claim_next_task(
            db_session, project_id, user_id, LOCK_TTL, task_type=TaskType.REVIEW
        )

        assert as_reviewer is None


class TestReapExpiredLocks:
    """`reap_expired_locks` returns timed-out `in_progress` tasks to the queue
    so another user can claim them (Known debt: a closed tab held an item
    forever)."""

    async def _claim_and_expire(self, session: AsyncSession, project_id: Any) -> Task:
        await open_task(
            session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await session.commit()
        task = await claim_next_task(session, project_id, uuid4(), LOCK_TTL)
        assert task is not None
        task.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        return task

    async def test_expired_in_progress_task_is_reopened_and_cleared(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        task = await self._claim_and_expire(db_session, project_id)

        reopened = await reap_expired_locks(db_session)
        await db_session.refresh(task)

        assert reopened == 1
        assert task.status is TaskStatus.OPEN
        assert task.locked_by_id is None
        assert task.locked_until is None
        assert task.assignee_id is None

    async def test_reopened_task_is_claimable_by_another_user(
        self, db_session: AsyncSession
    ) -> None:
        project_id = uuid4()
        task = await self._claim_and_expire(db_session, project_id)
        other = uuid4()
        assert await claim_next_task(db_session, project_id, other, LOCK_TTL) is None

        await reap_expired_locks(db_session)
        claimed = await claim_next_task(db_session, project_id, other, LOCK_TTL)

        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.locked_by_id == other

    async def test_live_locks_and_open_tasks_are_left_alone(self, db_session: AsyncSession) -> None:
        project_id = uuid4()
        holder = uuid4()
        await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await open_task(
            db_session, item_id=uuid4(), project_id=project_id, task_type=TaskType.ANNOTATE
        )
        await db_session.commit()
        live = await claim_next_task(db_session, project_id, holder, LOCK_TTL)
        assert live is not None

        reopened = await reap_expired_locks(db_session)
        await db_session.refresh(live)

        assert reopened == 0
        assert live.status is TaskStatus.IN_PROGRESS
        assert live.locked_by_id == holder

    async def test_nothing_to_reap_returns_zero(self, db_session: AsyncSession) -> None:
        assert await reap_expired_locks(db_session) == 0
