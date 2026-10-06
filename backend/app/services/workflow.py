"""Item workflow state machine (WF-1).

The item lifecycle from section 7 of the requirements lives here and nowhere
else. Routers ask this module whether a transition is legal; they never compare
statuses themselves. Keeping it in one pure module means the machine can be
unit-tested without a database and swapped for a project-configured machine
later without touching the API layer. The status enum is the ORM's own
(``app.models.ItemStatus``, re-exported here): importing it costs no
connection, and one enum means no conversion at the router boundary.

Default flow::

    new ──────────────► annotating ──submit──► submitted ──► in_review
     │                       │                                   │
     └──model──► prelabeled ─┘                    ┌──approve─────┤
                             └──skip──► skipped   │              └──reject──┐
                                                  ▼                         │
                                              approved                      │
                                                                            │
                        annotating ◄────────────────────────────────────────┘

A rejected item returns to the same annotator with a comment unless the project
overrides that (WF-1). The project's `WorkflowConfig` bends the default flow:
``review: none`` makes ``submit`` land on ``approved`` and removes the review
triggers, ``allow_skip: false`` removes ``skip``. Who a rejection goes back
to and whether self-review is allowed are not edges of the machine; they are
applied by `services/item_flow.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.models import ItemStatus
from app.schemas.project import ReviewMode, WorkflowConfig

__all__ = [
    "ItemStatus",
    "Transition",
    "Trigger",
    "WorkflowConfig",
    "WorkflowError",
    "next_status",
    "transitions_for",
]


class Trigger(StrEnum):
    """Named transitions. The API exposes these, not raw target statuses."""

    PRELABEL = "prelabel"
    ASSIGN = "assign"
    SUBMIT = "submit"
    SKIP = "skip"
    START_REVIEW = "start_review"
    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"


@dataclass(frozen=True, slots=True)
class Transition:
    """One legal edge of the machine."""

    trigger: Trigger
    source: ItemStatus
    target: ItemStatus
    #: Roles allowed to fire it. Empty means "any project member".
    roles: frozenset[str] = frozenset()


_ANNOTATOR: Final = frozenset({"owner", "annotator"})
_REVIEWER: Final = frozenset({"owner", "reviewer"})
_SYSTEM: Final = frozenset({"owner", "system"})

TRANSITIONS: Final[tuple[Transition, ...]] = (
    Transition(Trigger.PRELABEL, ItemStatus.NEW, ItemStatus.PRELABELED, _SYSTEM),
    Transition(Trigger.ASSIGN, ItemStatus.NEW, ItemStatus.ANNOTATING, _ANNOTATOR),
    Transition(Trigger.ASSIGN, ItemStatus.PRELABELED, ItemStatus.ANNOTATING, _ANNOTATOR),
    Transition(Trigger.ASSIGN, ItemStatus.REJECTED, ItemStatus.ANNOTATING, _ANNOTATOR),
    Transition(Trigger.SUBMIT, ItemStatus.ANNOTATING, ItemStatus.SUBMITTED, _ANNOTATOR),
    Transition(Trigger.SKIP, ItemStatus.ANNOTATING, ItemStatus.SKIPPED, _ANNOTATOR),
    Transition(Trigger.SKIP, ItemStatus.NEW, ItemStatus.SKIPPED, _ANNOTATOR),
    Transition(Trigger.START_REVIEW, ItemStatus.SUBMITTED, ItemStatus.IN_REVIEW, _REVIEWER),
    Transition(Trigger.APPROVE, ItemStatus.IN_REVIEW, ItemStatus.APPROVED, _REVIEWER),
    Transition(Trigger.APPROVE, ItemStatus.SUBMITTED, ItemStatus.APPROVED, _REVIEWER),
    Transition(Trigger.REJECT, ItemStatus.IN_REVIEW, ItemStatus.REJECTED, _REVIEWER),
    Transition(Trigger.REJECT, ItemStatus.SUBMITTED, ItemStatus.REJECTED, _REVIEWER),
    # An owner can pull an approved or skipped item back into the queue.
    Transition(Trigger.REOPEN, ItemStatus.APPROVED, ItemStatus.ANNOTATING, frozenset({"owner"})),
    Transition(Trigger.REOPEN, ItemStatus.SKIPPED, ItemStatus.ANNOTATING, _ANNOTATOR),
)

#: Statuses from which no trigger leads anywhere. Useful for progress metrics.
TERMINAL: Final[frozenset[ItemStatus]] = frozenset({ItemStatus.APPROVED})

_REVIEW_TRIGGERS: Final = frozenset({Trigger.START_REVIEW, Trigger.APPROVE, Trigger.REJECT})

DEFAULT_CONFIG: Final = WorkflowConfig()


def transitions_for(config: WorkflowConfig | None = None) -> tuple[Transition, ...]:
    """The machine's edges under ``config`` (the default flow when ``None``)."""
    config = config or DEFAULT_CONFIG
    edges: list[Transition] = []
    for transition in TRANSITIONS:
        if not config.allow_skip and transition.trigger is Trigger.SKIP:
            continue
        if config.review is ReviewMode.NONE:
            if transition.trigger in _REVIEW_TRIGGERS:
                continue
            if transition.trigger is Trigger.SUBMIT:
                # Nobody reviews: submitting is the last step.
                transition = Transition(
                    transition.trigger, transition.source, ItemStatus.APPROVED, transition.roles
                )
        edges.append(transition)
    return tuple(edges)


class WorkflowError(Exception):
    """A transition was refused. Carries enough detail for a 409 response."""

    def __init__(self, message: str, *, source: ItemStatus, trigger: Trigger) -> None:
        super().__init__(message)
        self.source = source
        self.trigger = trigger


def allowed_triggers(
    source: ItemStatus, role: str | None = None, *, config: WorkflowConfig | None = None
) -> frozenset[Trigger]:
    """Triggers that may fire from ``source``, optionally filtered by ``role``."""
    return frozenset(
        t.trigger
        for t in transitions_for(config)
        if t.source is source and (role is None or not t.roles or role in t.roles)
    )


def next_status(
    source: ItemStatus,
    trigger: Trigger,
    *,
    role: str | None = None,
    config: WorkflowConfig | None = None,
) -> ItemStatus:
    """Resolve the target status, or raise :class:`WorkflowError`.

    ``role`` is the caller's project role. ``None`` skips the permission check,
    which is what background jobs use. ``config`` is the project's workflow;
    ``None`` is the default flow.
    """
    for transition in transitions_for(config):
        if transition.source is not source or transition.trigger is not trigger:
            continue
        if role is not None and transition.roles and role not in transition.roles:
            raise WorkflowError(
                f"role {role!r} may not {trigger.value} an item in state {source.value!r}",
                source=source,
                trigger=trigger,
            )
        return transition.target

    legal = sorted(t.value for t in allowed_triggers(source, config=config))
    raise WorkflowError(
        f"cannot {trigger.value} an item in state {source.value!r}; "
        f"legal triggers here: {', '.join(legal) or 'none'}",
        source=source,
        trigger=trigger,
    )


def can(
    source: ItemStatus,
    trigger: Trigger,
    *,
    role: str | None = None,
    config: WorkflowConfig | None = None,
) -> bool:
    """Non-raising form of :func:`next_status`."""
    try:
        next_status(source, trigger, role=role, config=config)
    except WorkflowError:
        return False
    return True


def is_terminal(status: ItemStatus) -> bool:
    """True when the item needs no further work."""
    return status in TERMINAL
