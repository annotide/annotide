"""A demo project of audio and time-series items (§5), part of `app.demo seed`.

The audio is generated, not recorded: a few seconds of tones and silence
(16 kHz mono WAV, standard library only), so there is something to segment
without shipping a recording. The time series is a synthetic three-channel
sensor log with one obvious anomaly. Idempotent, like the rest of the seed.
"""

from __future__ import annotations

import io
import math
import wave
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.connectors.registry import build_connector
from app.db.session import get_sessionmaker
from app.models import (
    Connector,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Project,
    ProjectRole,
)
from app.services.scanning import scan_source

SEGMENTS_PROJECT_NAME = "Demo: audio and time series"
SEGMENTS_PREFIX = "audio-series/"
SAMPLE_RATE = 16_000

SEGMENTS_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "tone",
            "display_name": "Tone",
            "color": "#2563eb",
            "tools": ["segment"],
            "attributes": [],
        },
        {
            "name": "silence",
            "display_name": "Silence",
            "color": "#64748b",
            "tools": ["segment"],
            "attributes": [],
        },
        {
            "name": "anomaly",
            "display_name": "Anomaly",
            "color": "#dc2626",
            "tools": ["segment"],
            "attributes": [{"name": "severity", "type": "select", "options": ["low", "high"]}],
        },
    ],
    "classification": [{"name": "usable", "type": "boolean", "required": False}],
}


def tones_wav() -> bytes:
    """Six seconds: 1 s of 440 Hz, 1 s silence, 2 s of 660 Hz, 2 s silence."""
    plan = [(440.0, 1.0), (0.0, 1.0), (660.0, 2.0), (0.0, 2.0)]
    frames = bytearray()
    for frequency, seconds in plan:
        for n in range(int(seconds * SAMPLE_RATE)):
            sample = 0.4 * math.sin(2 * math.pi * frequency * n / SAMPLE_RATE) if frequency else 0
            frames += int(sample * 32767).to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(bytes(frames))
    return buffer.getvalue()


def sensor_csv() -> bytes:
    """200 samples of temperature, vibration and pressure; a spike at t = 120-135 s."""
    lines = ["seconds,temperature,vibration,pressure"]
    for t in range(200):
        spike = 25.0 if 120 <= t <= 135 else 0.0
        temperature = 20 + 2 * math.sin(t / 15)
        vibration = 0.3 * math.sin(t / 2) + spike / 5
        pressure = 101.3 + 0.2 * math.cos(t / 30) + (spike / 50 if spike else 0)
        lines.append(f"{t},{temperature:.3f},{vibration:.3f},{pressure:.3f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def seed_segments_project(
    org_id: UUID, owner_id: UUID | None, connector: Connector, secret: str
) -> None:
    """Upload the two files, create the project and scan it."""
    storage = build_connector(connector.type.value, dict(connector.config), secret)
    try:
        await storage.write(f"{SEGMENTS_PREFIX}tones.wav", tones_wav(), "audio/wav")
        await storage.write(f"{SEGMENTS_PREFIX}pump-7.timeseries.csv", sensor_csv(), "text/csv")

        async with get_sessionmaker()() as session:
            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == org_id, Project.name == SEGMENTS_PROJECT_NAME
                )
            )
            if project is None:
                project = Project(
                    organization_id=org_id,
                    name=SEGMENTS_PROJECT_NAME,
                    description="Seeded by `python -m app.demo seed`: mark segments on a "
                    "waveform and intervals on sensor channels. Safe to delete.",
                    source_connector_id=connector.id,
                    result_connector_id=connector.id,
                    source_prefix=SEGMENTS_PREFIX,
                    workflow={},
                    settings={},
                )
                session.add(project)
                await session.flush()
                print(f"Created project {SEGMENTS_PROJECT_NAME!r}")
            if project.label_schema_id is None:
                schema = LabelSchema(project_id=project.id, name="Segments")
                session.add(schema)
                await session.flush()
                session.add(
                    LabelSchemaVersion(
                        label_schema_id=schema.id, version=1, definition=SEGMENTS_SCHEMA
                    )
                )
                project.label_schema_id = schema.id
            if owner_id is not None and not await session.scalar(
                select(Membership).where(
                    Membership.project_id == project.id, Membership.user_id == owner_id
                )
            ):
                session.add(
                    Membership(user_id=owner_id, project_id=project.id, role=ProjectRole.OWNER)
                )
            result = await scan_source(
                session,
                project_id=project.id,
                connector=connector,
                storage=storage,
                prefix=SEGMENTS_PREFIX,
            )
            await session.commit()
        print(f"Audio and time series: {result.created} item(s) created, {result.updated} updated")
    finally:
        await storage.aclose()
