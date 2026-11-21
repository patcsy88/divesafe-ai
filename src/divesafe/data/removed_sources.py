"""Sources that were removed, kept by name so the screens and refusals that name them still work.

PHN e-Tide (ADR 0021, WITHDRAWN 2026-11-21): the connector, its POST getter, quota and setting are
gone. Its source name stays a connector prefix for the knowledge id screen and a refused source for
evaluation sends; its host stays off every getter; its setting stops start-up.
"""

from __future__ import annotations

PHN_ETIDE_SOURCE = "phn-etide-prediction"
PHN_ETIDE_HOSTS: frozenset[str] = frozenset({"etide.hydro.gov.my"})
PHN_ETIDE_SETTING = "DIVESAFE_PHN_ETIDE_ENABLED"
PHN_ETIDE_REFUSAL = (
    "refusing to start: DIVESAFE_PHN_ETIDE_ENABLED is set, but the PHN e-Tide connector was "
    "removed (ADR 0021 WITHDRAWN); remove the variable"
)
