"""Site registry. Coordinates must carry a source; none are invented."""

from __future__ import annotations

from divesafe.domain import DiveSite

TIOMAN_ISLAND = DiveSite(
    id="my-pahang-pulau-tioman",
    name="Pulau Tioman (island reference point, not a dive site)",
    latitude=2.7972,
    longitude=104.166,
    coordinate_source=(
        "Open-Meteo geocoding API (GeoNames-derived), 'Tioman Island', feature type island, "
        "Pahang, MY, GeoNames id 1734910, queried 2026-10-10. Island-level point only; specific "
        "dive sites are not yet registered."
    ),
    area_names=("Pahang", "Tioman"),
)

SITES: dict[str, DiveSite] = {TIOMAN_ISLAND.id: TIOMAN_ISLAND}
