"""Site registry. Coordinates must carry a source; none are invented."""

from __future__ import annotations

from divesafe.domain import DiveSite

REDANG_ISLAND = DiveSite(
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

SITES: dict[str, DiveSite] = {REDANG_ISLAND.id: REDANG_ISLAND}
