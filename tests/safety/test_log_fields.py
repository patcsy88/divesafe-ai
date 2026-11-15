"""The log leak scan ignores only the runtime numbers `logging` adds (regression for PR #9).

CI on PR #9 failed because `relativeCreated` was `84435.197371`, which holds the port `8443`.
These records are built by hand, so the numbers are fixed and the test is deterministic.
"""

from __future__ import annotations

import logging

import pytest
from tests.safety.log_fields import RUNTIME_NUMBERS, scanned_records, scanned_text

pytestmark = pytest.mark.safety

PORT = "8443"


def _record(msg: str = "agent stage is ON", args: tuple[object, ...] = ()) -> logging.LogRecord:
    record = logging.LogRecord("divesafe.api.state", logging.WARNING, "s.py", 1, msg, args, None)
    record.created = 1784438443.8443
    record.msecs = 518.8443
    record.relativeCreated = 84435.197371
    record.thread = 84438443
    record.process = 8443
    return record


def test_exactly_the_five_runtime_numbers_are_dropped() -> None:
    assert {"created", "msecs", "relativeCreated", "thread", "process"} == RUNTIME_NUMBERS


def test_a_port_in_the_runtime_numbers_is_not_reported() -> None:
    record = _record()
    for name in ("created", "msecs", "relativeCreated", "thread", "process"):
        assert PORT in str(getattr(record, name))
    assert PORT not in scanned_text(record)
    assert PORT not in scanned_records([record, _record()])


def test_a_port_in_msg_args_or_an_extra_field_is_reported() -> None:
    assert PORT in scanned_text(_record(msg=f"server at llm.example.org:{PORT}"))
    assert PORT in scanned_text(_record(msg="server port %s", args=(PORT,)))
    assert PORT in scanned_text(_record(msg="server port %d", args=(8443,)))
    with_extra = _record()
    with_extra.model_host = f"llm.example.org:{PORT}"
    assert PORT in scanned_text(with_extra)
    assert PORT in scanned_records([_record(), with_extra])


@pytest.mark.parametrize("name", ["name", "exc_text", "stack_info", "exc_info"])
def test_a_port_in_the_other_record_fields_is_reported(name: str) -> None:
    record = _record()
    value: object = f"x:{PORT}"
    if name == "exc_info":
        try:
            raise ValueError(f"x:{PORT}")
        except ValueError as error:
            value = (ValueError, error, None)
    setattr(record, name, value)
    assert PORT in scanned_text(record)
