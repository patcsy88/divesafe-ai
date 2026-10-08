"""Typed connector failures. Connectors raise these instead of returning partial data."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class. The source could not provide trustworthy evidence."""


class ConnectorTransportError(ConnectorError):
    """Network failure, timeout, or a non-success HTTP status."""


class ConnectorResponseError(ConnectorError):
    """The response was malformed, had unexpected units or shape, or was incomplete."""
