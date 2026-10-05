"""Tests for the item workflow state machine."""

from __future__ import annotations

import pytest

from app.services.workflow import (
    TERMINAL,
    TRANSITIONS,
    ItemStatus,
    Trigger,
    WorkflowConfig,
    WorkflowError,
    allowed_triggers,
    can,
    is_terminal,
    next_status,
    transitions_for,
)


class TestHappyPath:
    def test_default_flow_reaches_approved(self) -> None:
        status = ItemStatus.NEW
        for trigger, role in [
            (Trigger.ASSIGN, "annotator"),
            (Trigger.SUBMIT, "annotator"),
            (Trigger.START_REVIEW, "reviewer"),
            (Trigger.APPROVE, "reviewer"),
        ]:
            status = next_status(status, trigger, role=role)
        assert status is ItemStatus.APPROVED
        assert is_terminal(status)

    def test_prelabel_then_annotate(self) -> None:
        status = next_status(ItemStatus.NEW, Trigger.PRELABEL, role="system")
        assert status is ItemStatus.PRELABELED
        assert next_status(status, Trigger.ASSIGN, role="annotator") is ItemStatus.ANNOTATING

    def test_rejected_item_returns_to_annotation(self) -> None:
        rejected = next_status(ItemStatus.IN_REVIEW, Trigger.REJECT, role="reviewer")
        assert rejected is ItemStatus.REJECTED
        assert next_status(rejected, Trigger.ASSIGN, role="annotator") is ItemStatus.ANNOTATING

    def test_reviewer_may_approve_without_an_explicit_review_step(self) -> None:
        assert next_status(ItemStatus.SUBMITTED, Trigger.APPROVE, role="reviewer") is (
            ItemStatus.APPROVED
        )


class TestRefusals:
    def test_cannot_submit_a_new_item(self) -> None:
        with pytest.raises(WorkflowError) as exc:
            next_status(ItemStatus.NEW, Trigger.SUBMIT, role="annotator")
        assert exc.value.source is ItemStatus.NEW
        assert exc.value.trigger is Trigger.SUBMIT

    def test_refusal_message_lists_the_legal_triggers(self) -> None:
        with pytest.raises(WorkflowError, match="assign"):
            next_status(ItemStatus.NEW, Trigger.APPROVE)

    def test_annotator_cannot_approve(self) -> None:
        with pytest.raises(WorkflowError, match="may not approve"):
            next_status(ItemStatus.IN_REVIEW, Trigger.APPROVE, role="annotator")

    def test_only_an_owner_may_reopen_an_approved_item(self) -> None:
        assert next_status(ItemStatus.APPROVED, Trigger.REOPEN, role="owner") is (
            ItemStatus.ANNOTATING
        )
        with pytest.raises(WorkflowError):
            next_status(ItemStatus.APPROVED, Trigger.REOPEN, role="annotator")

    def test_role_none_skips_the_permission_check(self) -> None:
        # Background jobs pass role=None and must still be refused illegal edges.
        assert next_status(ItemStatus.IN_REVIEW, Trigger.APPROVE, role=None) is ItemStatus.APPROVED
        with pytest.raises(WorkflowError):
            next_status(ItemStatus.NEW, Trigger.APPROVE, role=None)


class TestIntrospection:
    def test_allowed_triggers_filters_by_role(self) -> None:
        assert Trigger.APPROVE in allowed_triggers(ItemStatus.IN_REVIEW, "reviewer")
        assert Trigger.APPROVE not in allowed_triggers(ItemStatus.IN_REVIEW, "annotator")

    def test_can_is_the_non_raising_form(self) -> None:
        assert can(ItemStatus.ANNOTATING, Trigger.SUBMIT, role="annotator")
        assert not can(ItemStatus.NEW, Trigger.SUBMIT, role="annotator")

    def test_approved_is_the_only_terminal_status(self) -> None:
        assert frozenset({ItemStatus.APPROVED}) == TERMINAL

    def test_every_status_except_terminal_has_an_outgoing_edge(self) -> None:
        for status in ItemStatus:
            if status in TERMINAL:
                continue
            assert allowed_triggers(status), f"{status} is a dead end but is not marked terminal"

    def test_no_duplicate_transitions(self) -> None:
        edges = [(t.source, t.trigger) for t in TRANSITIONS]
        assert len(edges) == len(set(edges)), "a (source, trigger) pair maps to two targets"


class TestProjectConfig:
    """`project.workflow` bends the default machine (WF-1)."""

    def test_default_config_is_the_default_machine(self) -> None:
        assert transitions_for(None) == TRANSITIONS
        assert transitions_for(WorkflowConfig()) == TRANSITIONS

    def test_review_none_submits_straight_to_approved(self) -> None:
        config = WorkflowConfig(review="none")
        assert next_status(ItemStatus.ANNOTATING, Trigger.SUBMIT, config=config) is (
            ItemStatus.APPROVED
        )
        for trigger in (Trigger.START_REVIEW, Trigger.APPROVE, Trigger.REJECT):
            assert not can(ItemStatus.SUBMITTED, trigger, config=config)
        # Everything else is untouched.
        assert can(ItemStatus.NEW, Trigger.ASSIGN, role="annotator", config=config)
        assert can(ItemStatus.APPROVED, Trigger.REOPEN, role="owner", config=config)

    def test_allow_skip_false_removes_skip(self) -> None:
        config = WorkflowConfig(allow_skip=False)
        with pytest.raises(WorkflowError, match="cannot skip"):
            next_status(ItemStatus.ANNOTATING, Trigger.SKIP, role="annotator", config=config)
        assert Trigger.SKIP not in allowed_triggers(ItemStatus.NEW, config=config)
        assert Trigger.SKIP in allowed_triggers(ItemStatus.NEW)

    def test_config_only_touches_the_edges_it_names(self) -> None:
        config = WorkflowConfig(review="none", allow_skip=False)
        edges = transitions_for(config)
        kept = {(t.source, t.trigger, t.target) for t in edges}
        assert (ItemStatus.ANNOTATING, Trigger.SUBMIT, ItemStatus.APPROVED) in kept
        assert all(t.trigger not in {Trigger.SKIP, Trigger.APPROVE} for t in edges)
        assert len(edges) == len(TRANSITIONS) - 2 - 5
