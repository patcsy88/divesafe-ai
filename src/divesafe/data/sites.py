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
        "Pahang, MY, GeoNames id 1734910, queried 2026-10-10. Island-level point only, not a "
        "dive site."
    ),
    area_names=("Pahang", "Tioman"),
)

TIGER_REEF = DiveSite(
    id="my-pahang-tiger-reef",
    name="Tiger Reef (dive site)",
    latitude=2.89302,
    longitude=104.05593,
    coordinate_source=(
        "PADI dive-site page 'Tiger Reef' (padi.com/dive-site/malaysia/tiger-reef-2/, read "
        "2026-11-20; contributed by a PADI dive centre; PADI does not warrant its accuracy). "
        "It matches the owner's boat GPS reading (undated; supplied 2026-11-20) exactly, "
        "probably a common origin, not an independent check; not surveyed."
    ),
    area_names=("Pahang", "Tioman"),
)

SITES: dict[str, DiveSite] = {TIOMAN_ISLAND.id: TIOMAN_ISLAND, TIGER_REEF.id: TIGER_REEF}
