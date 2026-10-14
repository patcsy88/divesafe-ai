"""Helpers for the asynchronous create (ADR 0019): create, then poll until the run has finished."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi.testclient import TestClient


def started(client: TestClient) -> TestClient:
    """Keep one event loop open for the client's life, so a background run survives its POST."""
    client.__enter__()
    return client


def poll(
    client: TestClient, location: str, headers: Mapping[str, str], *, seconds: float = 10.0
) -> httpx.Response:
    deadline = time.monotonic() + seconds
    while True:
        response = client.get(location, headers=headers)
        if response.status_code != 200 or response.json().get("status") not in {"RUNNING"}:
            return response
        if time.monotonic() > deadline:
            raise AssertionError("the run did not finish in time")
        time.sleep(0.02)


def create_and_poll(
    client: TestClient,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    extra_headers: Mapping[str, str] | None = None,
    seconds: float = 10.0,
) -> httpx.Response:
    """POST, require 202 with a Location, then return the final GET."""
    first = client.post(
        "/v1/assessments", json=dict(body), headers={**headers, **(extra_headers or {})}
    )
    assert first.status_code == 202, first.text
    location = first.headers["Location"]
    assert first.json()["status"] == "RUNNING"
    return poll(client, location, headers, seconds=seconds)
