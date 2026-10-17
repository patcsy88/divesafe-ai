"""Environmental data connectors. Each returns validated `EvidenceItem`s with provenance.

Design and the verified source register: docs/data-sources.md.
"""

from divesafe.data.base import Connector, FetchResult
from divesafe.data.data_gov_my_forecast import DataGovMyForecastConnector
from divesafe.data.data_gov_my_warnings import DataGovMyWarningConnector
from divesafe.data.errors import ConnectorError, ConnectorResponseError, ConnectorTransportError
from divesafe.data.http import (
    MET_API_HOSTS,
    PHN_HOSTS,
    JsonGetter,
    UrllibFormPostJsonGetter,
    UrllibJsonGetter,
)
from divesafe.data.met_api_marine import MetApiMarineConnector
from divesafe.data.open_meteo_marine import OpenMeteoMarineConnector
from divesafe.data.open_meteo_wind import OpenMeteoWindConnector
from divesafe.data.phn_etide import PhnEtideConnector
from divesafe.data.ratelimit import (
    MET_API_QUOTA,
    OPEN_METEO_QUOTA,
    PHN_ETIDE_QUOTA,
    CachingRateLimitedGetter,
    QuotaGroup,
)
from divesafe.data.sites import SITES, TIOMAN_ISLAND
from divesafe.domain import DiveSite

__all__ = [
    "TIOMAN_ISLAND",
    "SITES",
    "OPEN_METEO_QUOTA",
    "MET_API_QUOTA",
    "MET_API_HOSTS",
    "PHN_ETIDE_QUOTA",
    "PHN_HOSTS",
    "PhnEtideConnector",
    "UrllibFormPostJsonGetter",
    "MetApiMarineConnector",
    "CachingRateLimitedGetter",
    "QuotaGroup",
    "Connector",
    "ConnectorError",
    "ConnectorResponseError",
    "ConnectorTransportError",
    "DataGovMyForecastConnector",
    "DataGovMyWarningConnector",
    "FetchResult",
    "JsonGetter",
    "OpenMeteoMarineConnector",
    "OpenMeteoWindConnector",
    "DiveSite",
    "UrllibJsonGetter",
]
