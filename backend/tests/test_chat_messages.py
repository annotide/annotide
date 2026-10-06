"""`services/chat_messages.py` (API-7): one sentence per event, for Slack and Teams."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.chat_messages import ChatMessage, slack_body, summarise, teams_body


def _event(event: str, **data: Any) -> dict[str, Any]:
    return {"event": event, "project_id": "p1", "data": data}


@pytest.mark.parametrize(
    ("document", "summary", "detail"),
    [
        (
            _event("annotation.submitted", item_path="a.jpg", version=3, item_id="i1"),
            "Annotation submitted for a.jpg (version 3)",
            None,
        ),
        (
            _event("annotation.approved", item_path="a.jpg", comment="", item_id="i1"),
            "Annotation approved for a.jpg",
            None,
        ),
        (
            _event("item.approved", item_path="a.jpg", item_id="i1", via="review"),
            "a.jpg is done: approved after review",
            None,
        ),
        (
            _event("item.approved", item_path="a.jpg", item_id="i1", via="no_review"),
            "a.jpg is done: approved on submit, no review needed",
            None,
        ),
        (
            _event("snapshot.created", name="v1", item_count=12),
            "Snapshot v1 created with 12 items",
            None,
        ),
        (_event("job.failed", type="export", error="boom"), "Export job failed", "boom"),
        (_event("job.succeeded", type="scan"), "Scan job succeeded", None),
        (
            _event("retrain.requested", snapshot={"name": "v2"}, note="weekly"),
            "Retraining requested on snapshot v2",
            "weekly",
        ),
        (_event("retrain.requested", snapshot=None), "Retraining requested", None),
        (
            _event("webhook.test"),
            "Test message from Annotide: this channel is connected.",
            None,
        ),
        (_event("something.new"), "Annotide event: something.new", None),
    ],
)
def test_each_event_has_a_sentence(
    document: dict[str, Any], summary: str, detail: str | None
) -> None:
    message = summarise(document, project_name="P", frontend_url=None)
    assert (message.summary, message.detail, message.link) == (summary, detail, None)


def test_links_go_to_the_item_or_the_project() -> None:
    item = summarise(
        _event("annotation.submitted", item_id="i1"), project_name=None, frontend_url="https://a.x/"
    )
    project = summarise(_event("snapshot.created"), project_name=None, frontend_url="https://a.x")
    assert item.link == "https://a.x/projects/p1/annotate/i1"
    assert project.link == "https://a.x/projects/p1"


def test_long_detail_is_clipped() -> None:
    message = summarise(
        _event("job.failed", error="x" * 1000), project_name=None, frontend_url=None
    )
    assert message.detail is not None
    assert len(message.detail) == 300
    assert message.detail.endswith("…")


def test_bodies_without_project_or_link() -> None:
    message = ChatMessage(project=None, summary="Hi <there>", detail=None, link=None)
    slack = slack_body(message)
    assert slack["text"] == "Hi <there>"
    assert slack["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Hi &lt;there&gt;"}}
    ]
    card = teams_body(message)["attachments"][0]["content"]
    assert card["body"] == [{"type": "TextBlock", "text": "Hi <there>", "wrap": True}]
    assert "actions" not in card
