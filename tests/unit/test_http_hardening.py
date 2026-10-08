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
