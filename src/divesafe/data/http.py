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
# Also kept out of ALLOWED_HOSTS: only the POST getter below may call it.
PHN_HOSTS: frozenset[str] = frozenset({"etide.hydro.gov.my"})
PHN_MAX_BYTES = 512 * 1024
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 2
MAX_JSON_DEPTH = 32  # a parsing bound for hostile bodies, not a safety limit


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
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port not in (None, 443) or parsed.username is not None or parsed.password is not None:
        raise ConnectorTransportError(f"refusing a non-standard port or userinfo for {_label(url)}")


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
        if hosts & PHN_HOSTS:
            raise ValueError("the PHN host may only be called by the PHN POST getter")
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
        return _open_json(opener, request, url, self._timeout, self._max_bytes)


def _open_json(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    url: str,
    timeout: float,
    max_bytes: int,
) -> Any:
    try:
        with opener.open(request, timeout=timeout) as response:
            body = read_limited(response, max_bytes)
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
    # The body is parsed whatever the Content-Type says: PHN sends JSON labelled text/html.
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise ConnectorResponseError(f"{_label(url)} did not return valid JSON") from exc
    if _depth_exceeds(parsed, MAX_JSON_DEPTH):
        raise ConnectorResponseError(f"{_label(url)} returned JSON nested too deeply")
    return parsed


def _depth_exceeds(value: Any, limit: int) -> bool:
    """Iterative, so a hostile body cannot overflow the stack here or in a later deep copy."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > limit:
                return True
            children = item.values() if isinstance(item, dict) else item
            stack.extend((c, depth + 1) for c in children if isinstance(c, dict | list))
    return False


class UrllibFormPostJsonGetter:
    """POSTs `params` as a form and reads JSON, for the PHN e-Tide endpoint only.

    It sends no credential, keeps no cookie (the session cookie PHN sets is ignored), follows no
    redirect, and may only be built for the PHN host. `params` is the form body, not a query.
    """

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        deadline_seconds: float = 30.0,
        allowed_hosts: Collection[str] = PHN_HOSTS,
        max_bytes: int = PHN_MAX_BYTES,
    ) -> None:
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a collection of host names")
        hosts = frozenset(allowed_hosts)
        if not hosts or not hosts <= PHN_HOSTS:
            raise ValueError("this getter may only call the PHN e-Tide host")
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._allowed = hosts
        self._max_bytes = max_bytes

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        check_url(url, self._allowed)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._post, url, params), timeout=self._deadline
            )
        except TimeoutError as exc:
            raise ConnectorTransportError(f"request to {_label(url)} timed out") from exc

    def _post(self, url: str, params: Mapping[str, str]) -> Any:
        request = urllib.request.Request(  # noqa: S310 (scheme and host checked above)
            url,
            data=urllib.parse.urlencode(params).encode("ascii"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                # The e-Tide page's own script sends this; it is what the one recorded call used.
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        return _open_json(opener, request, url, self._timeout, self._max_bytes)
