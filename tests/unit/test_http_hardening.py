from __future__ import annotations

import asyncio
import io
import urllib.request

import pytest

from divesafe.data import ConnectorResponseError, ConnectorTransportError, UrllibJsonGetter
from divesafe.data.http import ALLOWED_HOSTS, _SafeRedirect, read_limited


def _redirect(newurl: str):  # type: ignore[no-untyped-def]
    handler = _SafeRedirect(ALLOWED_HOSTS)
    req = urllib.request.Request("https://api.data.gov.my/weather/warning")
    return handler.redirect_request(req, io.BytesIO(), 301, "Moved", {}, newurl)


def test_redirect_to_allowed_https_host_is_followed() -> None:
    assert _redirect("https://api.data.gov.my/weather/warning/") is not None


@pytest.mark.parametrize(
    "target",
    [
        "http://api.data.gov.my/weather/warning/",
        "https://evil.example/steal",
        "ftp://api.data.gov.my/x",
        "file:///etc/passwd",
        "https://api.data.gov.my:8443/weather/warning/",
        "https://user:pw@api.data.gov.my/weather/warning/",
        "https://api.data.gov.my:notaport/x",
    ],
)
def test_redirect_to_plain_http_other_host_or_other_scheme_is_refused(target: str) -> None:
    with pytest.raises(ConnectorTransportError):
        _redirect(target)


def test_redirect_count_is_capped() -> None:
    assert _SafeRedirect.max_redirections <= 2


@pytest.mark.parametrize("url", ["http://api.data.gov.my/x", "https://evil.example/x"])
def test_initial_request_must_be_https_and_on_the_allow_list(url: str) -> None:
    with pytest.raises(ConnectorTransportError):
        asyncio.run(UrllibJsonGetter().get_json(url, {}))


def test_oversized_response_is_refused() -> None:
    with pytest.raises(ConnectorResponseError):
        read_limited(io.BytesIO(b"x" * 101), 100)
    assert read_limited(io.BytesIO(b"x" * 100), 100) == b"x" * 100


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 200_000,  # RecursionError on some Python versions, ValueError-family on others
        b"1" * 5_000,  # an integer longer than the interpreter's digit limit: ValueError
        b"\xff\xfe not json",
        b"{not json",
    ],
)
def test_a_body_that_cannot_be_parsed_is_a_response_error_never_another_exception(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Opener:
        def open(self, request: object, timeout: float) -> _Response:
            return _Response(body)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _Opener())
    with pytest.raises(ConnectorResponseError):
        asyncio.run(UrllibJsonGetter().get_json("https://api.data.gov.my/weather/forecast/", {}))


def test_an_explicit_standard_port_is_still_allowed() -> None:
    assert _redirect("https://api.data.gov.my:443/weather/warning/") is not None


def test_every_recorded_fixture_is_within_the_depth_bound() -> None:
    """A later change to MAX_JSON_DEPTH must not silently reject real provider responses."""
    import json
    from pathlib import Path

    from divesafe.data.http import MAX_JSON_DEPTH, _depth_exceeds

    fixtures = sorted((Path(__file__).parent.parent / "fixtures").glob("*.json"))
    assert fixtures
    for path in fixtures:
        assert not _depth_exceeds(json.loads(path.read_text()), MAX_JSON_DEPTH), path.name
    assert _depth_exceeds([[[0]]], 2) and not _depth_exceeds([[[0]]], 3)
