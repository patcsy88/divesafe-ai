"""Dive site definition. Coordinates must carry a source; none are invented."""

from __future__ import annotations

from pydantic import Field

from divesafe.domain.base import Frozen
from divesafe.domain.provenance import GeoPoint
from divesafe.domain.risk_types import RiskFactorKind, ThresholdStatus


class SiteConstraint(Frozen):
    """A site-specific limit (for example exposure to a swell direction). Placeholder only:
    a constraint with status other than VALIDATED must not support a GO."""

    id: str = Field(min_length=1)
    factor: RiskFactorKind = RiskFactorKind.SITE_CONSTRAINT
    description: str = Field(min_length=1)
    threshold_status: ThresholdStatus = ThresholdStatus.TBD
    source: str | None = None


class DiveSite(Frozen):
    id: str = Field(min_length=1)
    name: str
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    coordinate_source: str = Field(min_length=1)
    area_names: tuple[str, ...] = ()
    constraints: tuple[SiteConstraint, ...] = ()

    @property
    def location(self) -> GeoPoint:
        return GeoPoint(latitude=self.latitude, longitude=self.longitude, description=self.name)
