"""Environmental data connectors. Each returns validated `EvidenceItem`s with provenance.

Design and the verified source register: docs/data-sources.md.
"""

from divesafe.data.base import Connector, FetchResult
from divesafe.data.data_gov_my_warnings import DataGovMyWarningConnector
from divesafe.data.errors import ConnectorError, ConnectorResponseError, ConnectorTransportError
from divesafe.data.http import JsonGetter, UrllibJsonGetter
from divesafe.data.open_meteo_marine import OpenMeteoMarineConnector
from divesafe.data.ratelimit import CachingRateLimitedGetter
from divesafe.data.sites import REDANG_ISLAND, SITES
from divesafe.domain import DiveSite

__all__ = [
    "REDANG_ISLAND",
    "SITES",
    "CachingRateLimitedGetter",
    "Connector",
    "ConnectorError",
    "ConnectorResponseError",
    "ConnectorTransportError",
    "DataGovMyWarningConnector",
    "FetchResult",
    "JsonGetter",
    "OpenMeteoMarineConnector",
    "DiveSite",
    "UrllibJsonGetter",
]
