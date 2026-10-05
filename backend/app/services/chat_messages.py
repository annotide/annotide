"""Slack and Microsoft Teams bodies for webhook events (API-7).

A chat channel wants a sentence, not the event document: these turn one
event (`{event, project_id, data, …}`, see `webhooks.emit_event`) into a
Slack incoming-webhook message or a Teams Workflows message with one
Adaptive Card. Pure functions; `webhooks.render_body` picks one by the hook's
`format`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PRODUCT = "Annotide"


@dataclass(frozen=True)
class ChatMessage:
    """What both formats say: a headline, optional detail, optional link."""

    project: str | None
    summary: str
    detail: str | None
    link: str | None


def _clip(text: object, limit: int = 300) -> str | None:
    if text is None or text == "":
        return None
    value = str(text).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def summarise(
    document: dict[str, Any], *, project_name: str | None, frontend_url: str | None
) -> ChatMessage:
    """The message for one event document."""
    event = str(document.get("event", ""))
    data: dict[str, Any] = document.get("data") or {}
    project_id = document.get("project_id")
    item = data.get("item_path") or data.get("item_id")
    detail: str | None = None

    if event == "annotation.submitted":
        summary = f"Annotation submitted for {item} (version {data.get('version')})"
    elif event in {"annotation.approved", "annotation.rejected"}:
        verdict = "approved" if event == "annotation.approved" else "rejected"
        summary = f"Annotation {verdict} for {item}"
        detail = _clip(data.get("comment"))
    elif event == "item.approved":
        how = "after review" if data.get("via") == "review" else "on submit, no review needed"
        summary = f"{item} is done: approved {how}"
    elif event == "snapshot.created":
        summary = f"Snapshot {data.get('name')!s} created with {data.get('item_count')} items"
    elif event in {"job.succeeded", "job.failed"}:
        outcome = "succeeded" if event == "job.succeeded" else "failed"
        summary = f"{str(data.get('type', 'A')).capitalize()} job {outcome}"
        detail = _clip(data.get("error"))
    elif event == "retrain.requested":
        snapshot = data.get("snapshot") or {}
        name = snapshot.get("name") if isinstance(snapshot, dict) else None
        summary = "Retraining requested" + (f" on snapshot {name}" if name else "")
        detail = _clip(data.get("note"))
    elif event == "webhook.test":
        summary = f"Test message from {PRODUCT}: this channel is connected."
    else:
        summary = f"{PRODUCT} event: {event}"

    link: str | None = None
    if frontend_url and project_id:
        base = frontend_url.rstrip("/")
        item_id = data.get("item_id")
        link = (
            f"{base}/projects/{project_id}/annotate/{item_id}"
            if item_id
            else f"{base}/projects/{project_id}"
        )
    return ChatMessage(project=project_name, summary=summary, detail=detail, link=link)


def _slack_escape(text: str) -> str:
    """Slack mrkdwn treats `&`, `<` and `>` as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_body(message: ChatMessage) -> dict[str, Any]:
    """A Slack incoming-webhook payload: `text` for notifications, `blocks` to read."""
    headline = message.summary if not message.project else f"{message.project}: {message.summary}"
    lines = [
        f"*{_slack_escape(message.project)}*" if message.project else None,
        _slack_escape(message.summary),
        f">{_slack_escape(message.detail)}" if message.detail else None,
    ]
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(line for line in lines if line)},
        }
    ]
    if message.link:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{message.link}|Open in {PRODUCT}>"}],
            }
        )
    return {"text": headline, "blocks": blocks}


def teams_body(message: ChatMessage) -> dict[str, Any]:
    """A Teams Workflows ("Post to a channel when a webhook request is received") message."""
    body: list[dict[str, Any]] = []
    if message.project:
        body.append(
            {"type": "TextBlock", "text": message.project, "weight": "Bolder", "wrap": True}
        )
    body.append({"type": "TextBlock", "text": message.summary, "wrap": True})
    if message.detail:
        body.append({"type": "TextBlock", "text": message.detail, "wrap": True, "isSubtle": True})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if message.link:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": f"Open in {PRODUCT}", "url": message.link}
        ]
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }
