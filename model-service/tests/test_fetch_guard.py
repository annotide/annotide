"""Caller-supplied media URLs may not reach cloud metadata endpoints."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from app.backends.heuristic import fetch_image_bytes, refuse_metadata_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254:80/metadata/instance?api-version=2021-02-01",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fe80::1]/",
        "http://100.100.100.200/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://METADATA.google.internal./computeMetadata/v1/",
    ],
)
async def test_metadata_endpoints_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="metadata"):
        await refuse_metadata_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a.png", "http:///a.png"])
async def test_only_http_urls_with_a_host(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        await refuse_metadata_url(url)


async def test_a_name_resolving_to_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    with pytest.raises(ValueError, match="metadata"):
        await refuse_metadata_url("http://innocent.example/a.png")


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000/api/v1/storage/local/x/a.png",  # in-cluster backend
        "http://172.18.0.4/a.png",
        "http://127.0.0.1:8000/a.png",
        "https://203.0.113.7/a.png",
    ],
)
async def test_private_and_public_addresses_stay_allowed(url: str) -> None:
    await refuse_metadata_url(url)


async def test_fetch_refuses_before_connecting() -> None:
    with pytest.raises(ValueError, match="metadata"):
        await fetch_image_bytes("http://169.254.169.254/latest/meta-data/")
