"""Source lineage: when do two pieces of evidence actually corroborate each other?

Agreement between two sources only means something if they are independent. Open-Meteo's marine
currents, for example, are Copernicus Marine's own product, so "Open-Meteo and Copernicus agree"
says nothing. `upstream` records what each item derives from, as the provider states it.

The rule is deliberately strict: two items are independent only if BOTH have a known, complete
upstream list and the lists share nothing. Unknown lineage can never corroborate, because
"unknown" does not mean "different".
"""

from __future__ import annotations

from divesafe.domain.models import EvidenceItem


def are_independent(a: EvidenceItem, b: EvidenceItem) -> bool:
    if not (a.upstream_known and b.upstream_known):
        return False
    return set(a.upstream).isdisjoint(b.upstream)


def can_corroborate(a: EvidenceItem, b: EvidenceItem) -> bool:
    """Agreement between these two may raise confidence only if this is True."""
    return a.category == b.category and are_independent(a, b)
