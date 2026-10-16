"""Minimal HTTP seam so connectors can be tested with recorded fixtures (no live calls).

Hardening: https only, host allow-list (applied to the first request and every redirect), at
most two redirects, a response size cap and a total deadline. Error text never includes query
strings.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection, Mapping
from typing import IO, Any, Protocol

from divesafe.data.errors import ConnectorResponseError, ConnectorTransportError

USER_AGENT = "divesafe-ai/0.1 (non-commercial research; decision support only)"
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"marine-api.open-meteo.com", "api.open-meteo.com", "api.data.gov.my"}
)
# Kept out of ALLOWED_HOSTS on purpose: only a getter built with a credential may call it.
MET_API_HOSTS: frozenset[str] = frozenset({"api.met.gov.my"})
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 2


class JsonGetter(Protocol):
    async def get_json(self, url: str, params: Mapping[str, str]) -> Any: ...


def _label(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.hostname}{parsed.path}"


def check_url(url: str, allowed_hosts: Collection[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ConnectorTransportError(f"refusing non-https URL for {_label(url)}")
    if parsed.hostname not in allowed_hosts:
        raise ConnectorTransportError(f"host not allowed: {parsed.hostname}")


def read_limited(response: IO[bytes], max_bytes: int) -> bytes:
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ConnectorResponseError(f"response exceeds the {max_bytes}-byte limit")
    return body


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, allowed_hosts: Collection[str]) -> None:
        self._allowed = allowed_hosts

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        check_url(newurl, self._allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """For requests that carry a credential: a redirect is never followed, so the header cannot
    follow it. urllib then raises HTTPError for the 3xx, which is reported as an HTTP status."""

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        return None


class UrllibJsonGetter:
    def __init__(
        self,
        timeout_seconds: float = 15.0,
        deadline_seconds: float = 30.0,
        allowed_hosts: Collection[str] = ALLOWED_HOSTS,
        max_bytes: int = MAX_BYTES,
        *,
        auth_header: tuple[str, str] | None = None,
    ) -> None:
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a collection of host names")
        hosts = frozenset(allowed_hosts)
        if auth_header is not None and (not hosts or not hosts <= MET_API_HOSTS):
            raise ValueError("a getter with a credential needs its own host list")
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._allowed = hosts
        self._max_bytes = max_bytes
        self._auth_header = auth_header

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        check_url(url, self._allowed)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._get, url, params), timeout=self._deadline
            )
        except TimeoutError as exc:
            raise ConnectorTransportError(f"request to {_label(url)} timed out") from exc

    def _get(self, url: str, params: Mapping[str, str]) -> Any:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._auth_header is not None:
            opener = urllib.request.build_opener(_NoRedirect())
        else:
            opener = urllib.request.build_opener(_SafeRedirect(self._allowed))
        request = urllib.request.Request(  # noqa: S310 (scheme and host checked above)
            full, headers=headers
        )
        if self._auth_header is not None:
            request.add_unredirected_header(*self._auth_header)
        try:
            with opener.open(request, timeout=self._timeout) as response:
                body = read_limited(response, self._max_bytes)
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(OSError):
                exc.close()
            raise ConnectorTransportError(f"HTTP {exc.code} from {_label(url)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectorTransportError(f"request to {_label(url)} failed") from exc
        except (http.client.HTTPException, ValueError):
            # The text of these can echo a header value, so it is dropped.
            echoed = True
        else:
            echoed = False
        if echoed:
            raise ConnectorTransportError(f"request to {_label(url)} failed")
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as exc:
            raise ConnectorResponseError(f"{_label(url)} did not return valid JSON") from exc
