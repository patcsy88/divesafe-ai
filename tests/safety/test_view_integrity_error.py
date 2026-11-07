"""`AssessmentRecordView.of` on a stored record that cannot be presented.

A record built with `model_construct` (no validation) carries a scenario with confidence 1.5, which
the view refuses. `of()` must raise the fixed-text `RepositoryIntegrityError` with no `__cause__`
and no `__context__` (pydantic's message echoes input values), log one fixed line with only
`assessment_id`, and the app must answer 500 {"detail": "internal error"} without the input text.

KNOWN GAP (docs/risk-model.md): a decision that was stored before the view failed stays stored, so
the client sees a 500 for a decision that took effect. The test documents it.

EQUIVALENT MUTANTS (not tested): the wording of the log line beyond the pinned text.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pytest
from tests.agent.scripted_llm import fake
from tests.api_polling import create_and_poll
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build
from tests.safety.test_view_self_estimate import varied_record

from divesafe.api import schemas as schemas_module
from divesafe.domain import AssessmentRecord, Scenario
from divesafe.services import RepositoryIntegrityError

pytestmark = pytest.mark.safety

FIXED = "the record could not be presented"
LOG_LINE = "record could not be presented"
DECISION = {"decision": "INSUFFICIENT EVIDENCE"}
INPUT = "PRIVATE-MODEL-TEXT-9731"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def captured() -> Any:
    package, capture = logging.getLogger("divesafe"), Capture()
    level = package.level
    package.addHandler(capture)
    package.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        package.removeHandler(capture)
        package.setLevel(level)


def broken_of(record: AssessmentRecord) -> AssessmentRecord:
    """The record with its first scenario out of bounds (1.5) and input text in it."""
    first = Scenario.model_construct(
        **{**record.scenarios[0].model_dump(), "confidence": 1.5, "summary": INPUT}
    )
    return record.model_copy(update={"scenarios": (first, *record.scenarios[1:])})


def everything(records: list[logging.LogRecord]) -> str:
    return " ".join(str(v) for r in records for v in (r.getMessage(), *r.__dict__.values()))


def of_misses(mod: Any) -> list[str]:
    bad: list[str] = []
    good = varied_record()
    if mod.AssessmentRecordView.of(good).id != good.id:
        bad.append("a valid record did not build")
    broken = broken_of(good)
    with captured() as capture:
        try:
            mod.AssessmentRecordView.of(broken)
            bad.append("an invalid record was presented")
        except RepositoryIntegrityError as exc:
            if str(exc) != FIXED:
                bad.append(f"message {str(exc)!r}")
            if exc.__cause__ is not None:
                bad.append("__cause__ is set")
            if exc.__context__ is not None:
                bad.append("__context__ is set (the pydantic error is kept)")
            if exc.__suppress_context__ is not True and exc.__context__ is not None:
                bad.append("context is shown")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"raised {type(exc).__name__}")
    ours = [r for r in capture.records if r.getMessage() == LOG_LINE]
    if len(ours) != 1:
        bad.append(f"logged {len(ours)} times")
    else:
        standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
        extras = {k for k in ours[0].__dict__ if k not in standard}
        if extras != {"assessment_id"}:
            bad.append(f"log extras {sorted(extras)}")
        if ours[0].exc_info:
            bad.append("the log carries the exception")
    text = everything(capture.records)
    for leak in (INPUT, "1.5", "validation error", "pydantic", "Input should", "input_value"):
        if leak in text:
            bad.append(f"the captured logs contain {leak!r}")
    return bad


def test_of_raises_the_fixed_error_without_cause_or_context_and_logs_only_the_id() -> None:
    found = of_misses(schemas_module)
    assert found == []


def api_misses() -> list[str]:
    bad: list[str] = []
    client, _, repo = _build(provider=fake())
    created = create_and_poll(client, PLAN, AUTH_A)
    if created.status_code != 200:
        return [f"control: create answered {created.status_code}"]
    rid = created.json()["record"]["id"]
    stored = repo.get(rid)
    assert stored is not None
    # GET of a stored record the view refuses (out of bounds, built without validation)
    repo._records[rid] = broken_of(stored)
    with captured() as capture:
        seen = client.get(f"/v1/assessments/{rid}", headers=AUTH_A)
    responses = [("GET", seen)]
    repo._records[rid] = stored
    # A decision on a VALID stored record whose view then fails: the gate accepts and stores it
    # (it revalidates the domain record), and only presenting the result fails.
    key = {**AUTH_A, "Idempotency-Key": "view-broken-key-0123456789"}
    with pytest.MonkeyPatch.context() as m, captured() as capture2:
        real = schemas_module._renamed
        m.setattr(schemas_module, "_renamed", lambda item: {**real(item), "extra": INPUT})
        decided = client.post(f"/v1/assessments/{rid}/decision", json=DECISION, headers=key)
        replay = client.post(f"/v1/assessments/{rid}/decision", json=DECISION, headers=key)
        responses += [("decision", decided), ("replay", replay)]
    for name, response in responses:
        if response.status_code != 500 or response.json() != {"detail": "internal error"}:
            bad.append(f"{name}: {response.status_code} {response.text[:80]!r}")
        if INPUT in response.text or "1.5" in response.text or "validation" in response.text:
            bad.append(f"{name}: the body carries input text")
    if INPUT in everything(capture.records + capture2.records):
        bad.append("the logs carry the input text")
    after = repo.get(rid)
    if after is None or after.human_decision is None:
        bad.append("the decision was not stored (the documented gap no longer holds)")
    elif after.human_decision.decision.value != DECISION["decision"]:
        bad.append("a different decision was stored")
    return bad


def test_the_api_answers_500_and_the_decision_is_nonetheless_stored() -> None:
    found = api_misses()
    assert found == []


# --- mutations on copies of divesafe.api.schemas ------------------------------------------------

CATCH = "        except ValidationError:\n"
RAISE = '        raise RepositoryIntegrityError("the record could not be presented")\n'
LOG = 'logger.error("record could not be presented", extra={"assessment_id": record.id})'

RAISE_FROM = RAISE.replace("\n", " from exc\n")
LOG_KEEP = 'extra={"assessment_id": record.id}'
LOG_SUMMARY = 'extra={"assessment_id": record.id, "summary": data["scenarios"][0]["summary"]}'
LOG_EXTRA = 'extra={"assessment_id": record.id, "n": 1}'
LOG_TEXT = 'logger.error("record could not be presented: %s", exc, ' + LOG_KEEP + ")"
INSIDE = "\n            raise RepositoryIntegrityError(" + '"the record could not be presented")'

MUTANTS = [
    pytest.param(
        [(RAISE, RAISE_FROM), (CATCH, "        except ValidationError as exc:\n")],
        id="from-exc-keeps-the-cause",
    ),
    pytest.param([(LOG, LOG + INSIDE)], id="raised-inside-the-except-context-kept"),
    pytest.param(
        [(RAISE, '        raise RepositoryIntegrityError("record problem")\n')],
        id="message-changed",
    ),
    pytest.param([(LOG_KEEP, LOG_SUMMARY)], id="log-carries-input-values"),
    pytest.param(
        [(CATCH, "        except ValidationError as exc:\n"), (LOG, LOG_TEXT)],
        id="log-carries-the-pydantic-text",
    ),
    pytest.param([(LOG_KEEP, LOG_EXTRA)], id="log-extra-added"),
    pytest.param([(LOG, "pass")], id="nothing-logged"),
    pytest.param(
        [(RAISE, "        return cls.model_construct(**data)\n")], id="invalid-record-presented"
    ),
]


@pytest.mark.parametrize("edits", MUTANTS)
def test_every_mutation_of_the_presentation_error_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(schemas_module, *edits)
    misses: list[str] = []
    crashes: list[str] = []
    try:
        misses += of_misses(mutant)
    except Exception as exc:  # noqa: BLE001
        crashes.append(type(exc).__name__)
    assert misses != [], f"survived (crashes: {crashes})"
