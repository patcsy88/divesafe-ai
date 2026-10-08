"""Site registry. Coordinates must carry a source; none are invented."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Site(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    coordinate_source: str = Field(min_length=1)
    area_names: tuple[str, ...] = ()


REDANG_ISLAND = Site(
    id="my-terengganu-pulau-redang",
    name="Pulau Redang (island reference point, not a dive site)",
    latitude=5.77736,
    longitude=103.00759,
    coordinate_source=(
        "Open-Meteo geocoding API (GeoNames-derived), 'Pulau Redang', Terengganu, MY, "
        "queried 2026-10-09. Island-level point only; specific dive sites are not yet registered."
    ),
    area_names=("Terengganu",),
)

SITES: dict[str, Site] = {REDANG_ISLAND.id: REDANG_ISLAND}
