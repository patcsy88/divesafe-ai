from __future__ import annotations

from divesafe.api.state import build_default_state
from divesafe.config import Settings


def test_the_default_state_registers_no_open_meteo_connector() -> None:
    state = build_default_state(Settings(environment="development"))
    names = {type(c).__name__ for c in state.connectors}
    assert names == {"DataGovMyWarningConnector"}
