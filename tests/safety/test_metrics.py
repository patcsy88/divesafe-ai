"""`/metrics` (ADR 0027 item 2): who may read it, what it can say, and that it never changes a run.

- Only an isolated `metrics` key reads it, and that key reads nothing else; a key file that
  combines `metrics` with another role, with groups or with another key's actor is refused.
- `prometheus_client` is imported only by `api/metrics.py`, and never its global registry or its
  own HTTP servers.
- The output is exactly the fixed families and label names; label values never carry an actor,
  key, request id, assessment id, site or any text a caller or connector chose.
- It counts toward its own rate limit and the failed-authentication limit.
- A metrics failure never changes the stored record, the failure code or how the run ends, and
  the counts are taken only after the result or failure code is stored.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from tests.api_polling import create_and_poll
from tests.safety import test_tracing_record_unchanged as tr
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_roles_and_limits import BAD, HEADERS, KEYS, PLAN_D, _client

from divesafe.agents.runner import CALL_OUTCOMES
from divesafe.api import app as app_module
from divesafe.api import auth as auth_module
from divesafe.api import metrics as metrics_module
from divesafe.api import runner as api_runner
from divesafe.api.auth import ApiKeyAuthenticator, Role, parse_api_key_hashes
from divesafe.api.metrics import Metrics, check_metrics_startup
from divesafe.api.state import default_limits
from divesafe.domain import Recommendation, StageName, StageOutcome
from divesafe.observability import DROP_REASONS, ModelCallEntry, RunTrace
from divesafe.services import FailureCode, RunState

pytestmark = pytest.mark.safety

SRC = Path(__file__).resolve().parents[2] / "src" / "divesafe"
SCRAPER = HEADERS[Role.METRICS]
OPEN = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
AID = "0" * 32

# name -> (type, label names). Nothing else may appear.
FAMILIES: dict[str, tuple[str, frozenset[str]]] = {
    "divesafe_assessments_created": ("counter", frozenset()),
    "divesafe_assessments_finished": ("counter", frozenset({"outcome"})),
    "divesafe_stage_duration_seconds": ("histogram", frozenset({"stage", "outcome"})),
    "divesafe_connector_failures": ("counter", frozenset({"connector", "kind"})),
    "divesafe_knowledge_retrieval": ("counter", frozenset({"outcome"})),
    "divesafe_model_calls": ("counter", frozenset({"outcome"})),
    "divesafe_auth_failures": ("counter", frozenset()),
    "divesafe_runs_in_flight": ("gauge", frozenset()),
    "divesafe_trace_dropped": ("counter", frozenset({"reason"})),
}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _families(text: str) -> dict[str, Any]:
    return {f.name: f for f in text_string_to_metric_families(text)}


def _value(text: str, name: str, **labels: str) -> float:
    family = _families(text)[name]
    suffix = "_count" if family.type == "histogram" else ""
    total = 0.0
    for sample in family.samples:
        wanted = family.name + ("_total" if family.type == "counter" else suffix)
        if sample.name == wanted and all(sample.labels.get(k) == v for k, v in labels.items()):
            total += sample.value
    return total


def _scrape(client: TestClient) -> str:
    response = client.get("/metrics", headers=SCRAPER)
    assert response.status_code == 200
    return response.text


# ---- who may read it --------------------------------------------------------------------------


def test_every_key_without_the_metrics_role_gets_403() -> None:
    client, _ = _client()
    for role in Role:
        if role is not Role.METRICS:
            assert client.get("/metrics", headers=HEADERS[role]).status_code == 403, role
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=SCRAPER).status_code == 200


def test_a_metrics_only_key_gets_403_on_every_other_route() -> None:
    client, _ = _client()
    checked = []
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.path in OPEN or route.path == "/metrics":
            continue
        path = route.path.replace("{assessment_id}", AID).replace("{site_id}", "x")
        for method in route.methods:
            body = {"decision": "GO", "rationale": "r", "observations": {"v": 1}, "to_decider": "x"}
            response = client.request(method, path, headers=SCRAPER, json={**PLAN_D, **body})
            assert response.status_code == 403, (method, route.path)
            checked.append(route.path)
    assert len(set(checked)) >= 7


@pytest.mark.parametrize(
    "entry",
    [
        {"actor": "scraper", "roles": ["metrics", "viewer"]},
        {"actor": "scraper", "roles": ["metrics", "assessor"]},
        {"actor": "scraper", "roles": ["metrics", "viewer", "admin"]},
        {"actor": "scraper", "roles": ["viewer", "decider", "metrics"]},
        {"actor": "scraper", "roles": ["metrics"], "groups": ["north"]},
    ],
)
def test_a_key_file_mixing_metrics_with_another_role_or_groups_is_refused(
    entry: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        parse_api_key_hashes(json.dumps({_hash("k"): entry}))


@pytest.mark.parametrize("other", ["scraper", "Scraper", " SCRAPER "])
def test_a_metrics_key_cannot_share_its_actor_with_another_key(other: str) -> None:
    raw = {
        _hash("k1"): {"actor": "scraper", "roles": ["metrics"]},
        _hash("k2"): {"actor": other, "roles": ["viewer"], "groups": ["north"]},
    }
    with pytest.raises(ValueError):
        parse_api_key_hashes(json.dumps(raw))


def test_an_isolated_metrics_key_loads_and_reads_metrics() -> None:
    entries = parse_api_key_hashes(
        json.dumps(
            {
                _hash("k1"): {"actor": "scraper", "roles": ["metrics"]},
                _hash("k2"): {"actor": "alice", "roles": ["viewer"], "groups": ["north"]},
            }
        )
    )
    client, _ = _client()
    client.app.state.divesafe.authenticator = ApiKeyAuthenticator(entries)  # type: ignore[attr-defined]
    assert client.get("/metrics", headers={"Authorization": "Bearer k1"}).status_code == 200
    assert client.get("/v1/sites", headers={"Authorization": "Bearer k1"}).status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer k2"}).status_code == 403


@pytest.mark.parametrize(
    "edit",
    [
        ("Role.METRICS in role_set and (len(role_set) > 1 or groups)", "False"),
        ("(len(role_set) > 1 or groups)", "(len(role_set) > 1)"),
        ("(len(role_set) > 1 or groups)", "groups"),
    ],
)
def test_mutation_the_isolation_check_is_load_bearing(edit: tuple[str, str]) -> None:
    mutant = mutated_module(auth_module, edit)
    bad = [
        {"actor": "s", "roles": ["metrics", "viewer"]},
        {"actor": "s", "roles": ["metrics"], "groups": ["north"]},
    ]
    accepted = 0
    for entry in bad:
        try:
            mutant.parse_api_key_hashes(json.dumps({_hash("k"): entry}))
            accepted += 1
        except ValueError:
            pass
    assert accepted >= 1


# ---- what may be imported ---------------------------------------------------------------------

FORBIDDEN_NAMES = frozenset(
    {
        "start_http_server",
        "start_wsgi_server",
        "make_asgi_app",
        "make_wsgi_app",
        "REGISTRY",
        "push_to_gateway",
        "pushadd_to_gateway",
        "write_to_textfile",
        "MultiProcessCollector",
        "multiprocess",
        "ProcessCollector",
        "PlatformCollector",
        "GCCollector",
    }
)


def _prometheus_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
                names = [a.asname or a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
                names = [a.name for a in node.names] + [a.asname or "" for a in node.names]
            else:
                mods = []
            if (
                any(m.split(".")[0] == "prometheus_client" for m in mods)
                and rel != "api/metrics.py"
            ):
                offenders.append(f"{rel} imports prometheus_client")
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            parts = {p for n in [*names, *mods] for p in n.split(".")}
            offenders.extend(f"{rel} uses {n}" for n in sorted(parts & FORBIDDEN_NAMES))
            if isinstance(node, ast.Constant) and node.value in {"prometheus_client"}:
                offenders.append(f"{rel} names prometheus_client")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "PROMETHEUS_MULTIPROC" in node.value.upper()
            ):
                offenders.append(f"{rel} names the multiprocess directory")
    return offenders


def test_prometheus_client_is_used_only_by_the_metrics_module_and_never_its_servers() -> None:
    assert _prometheus_offenders(SRC) == []


@pytest.mark.parametrize(
    ("rel", "line"),
    [
        ("api/app.py", "from prometheus_client import generate_latest\n"),
        ("api/metrics.py", "from prometheus_client import start_http_server\n"),
        ("api/metrics.py", "from prometheus_client import REGISTRY\n"),
        ("api/metrics.py", "import prometheus_client.multiprocess\n"),
        ("api/runner.py", "import prometheus_client as pc\n"),
    ],
)
def test_mutation_the_import_pin_has_teeth(rel: str, line: str, tmp_path: Path) -> None:
    for path in SRC.rglob("*.py"):
        target = tmp_path / path.relative_to(SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    edited = tmp_path / rel
    edited.write_text(line + edited.read_text(encoding="utf-8"), encoding="utf-8")
    assert _prometheus_offenders(tmp_path) != []


# Exactly what `api/metrics.py` may take from the library: an allow-list, so a sender (a bridge,
# a handler, another exposition format) cannot be added without changing this test.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "prometheus_client": {
        "CONTENT_TYPE_LATEST",
        "CollectorRegistry",
        "Counter",
        "Gauge",
        "Histogram",
        "disable_created_metrics",
        "generate_latest",
    },
    "prometheus_client.core": {"CounterMetricFamily"},
    "prometheus_client.registry": {"Collector"},
}


def _metrics_import_problems(source: str) -> list[str]:
    problems: list[str] = []
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"prometheus_client", "importlib"}:
                    problems.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level != 0 and root != "divesafe" and node.module is None:
                problems.append("relative import")
            if root == "importlib":
                problems.append(f"from {node.module}")
            if root == "prometheus_client":
                if any(a.asname is not None for a in node.names):
                    problems.append(f"aliased import from {node.module}")
                found.setdefault(node.module or "", set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Name) and node.id in {"__import__", "getattr", "eval", "exec"}:
            problems.append(f"uses {node.id}")
    if found != ALLOWED_IMPORTS:
        problems.append(f"imports {found}")
    return problems


def test_the_metrics_module_imports_exactly_the_allowed_names() -> None:
    assert _metrics_import_problems((SRC / "api" / "metrics.py").read_text("utf-8")) == []


@pytest.mark.parametrize(
    "line",
    [
        "from prometheus_client.bridge.graphite import GraphiteBridge\n",
        "from prometheus_client.exposition import MetricsHandler\n",
        "from prometheus_client.openmetrics import exposition\n",
        "from prometheus_client import Summary\n",
        "from prometheus_client import Counter as C\n",
        "import importlib\n",
        "x = __import__('prometheus_' + 'client')\n",
        "x = getattr(object, 'y', None)\n",
    ],
)
def test_mutation_the_allow_list_has_teeth(line: str) -> None:
    source = (SRC / "api" / "metrics.py").read_text("utf-8")
    assert _metrics_import_problems(line + source) != []


def test_mutation_dropping_an_allowed_import_is_caught() -> None:
    source = (SRC / "api" / "metrics.py").read_text("utf-8")
    edited = source.replace("from prometheus_client.registry import Collector\n", "")
    assert edited != source
    assert _metrics_import_problems(edited) != []


# ---- start-up --------------------------------------------------------------------------------


def _refused_at_start_up(
    create_app: Callable[..., Any], name: str, monkeypatch: pytest.MonkeyPatch, directory: Path
) -> bool:
    """True when `create_app` refuses with `name` set. The app is built first, so the variable
    is set only for the start-up under test."""
    client, _ = _client()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    with monkeypatch.context() as mp:
        mp.setattr(sys.modules[create_app.__module__], "configure_logging", lambda *_: None)
        mp.setenv(name, str(directory))
        try:
            create_app(state)
        except RuntimeError as exc:
            assert "PROMETHEUS_" in str(exc)
            return True
    return False


# Built so that no string in this file names the multiprocess variable whole.
_MULTI = "MULTI" + "PROC"
REFUSED_NAMES = [
    f"PROMETHEUS_{_MULTI}_DIR",
    f"prometheus_{_MULTI.lower()}_dir",
    "PROMETHEUS_DISABLE_CREATED_SERIES",
    "PROMETHEUS_LEGACY_NAME_VALIDATION",
    "Prometheus_anything",
]


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_start_up_refuses_any_prometheus_variable(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _refused_at_start_up(app_module.create_app, name, monkeypatch, tmp_path)
    assert list(tmp_path.iterdir()) == [], "a value file was written before the refusal"


def test_start_up_without_a_prometheus_variable_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    check_metrics_startup({"DIVESAFE_X": "1", "PROMETHEUSX": "1", "X_PROMETHEUS_Y": "1"})
    assert not _refused_at_start_up(app_module.create_app, "DIVESAFE_X", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "edit",
    [
        ('if any(name.upper().startswith("PROMETHEUS_")', 'if any(name.startswith("PROMETHEUS_")'),
        ("        raise RuntimeError(METRICS_REFUSAL)\n", "        pass\n"),
    ],
)
def test_mutation_the_start_up_refusal_has_teeth(
    edit: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutant = mutated_module(metrics_module, edit)
    with monkeypatch.context() as mp:
        mp.setattr(app_module, "check_metrics_startup", mutant.check_metrics_startup)
        refused = [
            _refused_at_start_up(app_module.create_app, n, monkeypatch, tmp_path)
            for n in REFUSED_NAMES
        ]
    assert not all(refused)


def test_mutation_create_app_without_the_check_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutant = mutated_module(app_module, ("    check_metrics_startup(os.environ)\n", ""))
    assert not _refused_at_start_up(
        mutant.create_app, "PROMETHEUS_DISABLE_CREATED_SERIES", monkeypatch, tmp_path
    )


# ---- what it can say --------------------------------------------------------------------------


def test_the_output_is_exactly_the_fixed_families_and_label_names() -> None:
    client, make = _client()
    make()
    response = client.get("/metrics", headers=SCRAPER)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain; version=")
    found = _families(response.text)
    assert set(found) == set(FAMILIES)
    for name, family in found.items():
        kind, labels = FAMILIES[name]
        assert family.type == kind, name
        for sample in family.samples:
            assert set(sample.labels) - {"le"} == labels, (name, sample.labels)
    connectors = {c.name for c in client.app.state.divesafe.connectors}  # type: ignore[attr-defined]
    allowed = _allowed_label_values(connectors)
    for name, family in found.items():
        for sample in family.samples:
            for label, value in sample.labels.items():
                if label != "le":
                    assert value in allowed[name][label], (name, label, value)


def _allowed_label_values(connectors: set[str]) -> dict[str, dict[str, set[str]]]:
    """From the code enums and literal sets, never from `api/metrics.py`'s own tuples."""
    return {
        "divesafe_assessments_finished": {
            "outcome": {r.value for r in Recommendation}
            | {f"failed_{c.value}" for c in FailureCode}
        },
        "divesafe_knowledge_retrieval": {
            "outcome": {"ok", "not_configured", "failed", "timed_out", "issue"}
        },
        "divesafe_model_calls": {"outcome": set(CALL_OUTCOMES)},
        "divesafe_stage_duration_seconds": {
            "stage": {s.value for s in StageName},
            "outcome": {o.value for o in StageOutcome},
        },
        "divesafe_connector_failures": {
            "connector": connectors | {"other"},
            "kind": {"failed", "timed_out"},
        },
        "divesafe_trace_dropped": {"reason": set(DROP_REASONS)},
    }


def test_one_run_is_counted_once_by_its_stored_outcome() -> None:
    client, make = _client()
    make()
    text = _scrape(client)
    assert _value(text, "divesafe_assessments_created") == 1
    assert _value(text, "divesafe_assessments_finished") == 1
    assert _value(text, "divesafe_assessments_finished", outcome="INSUFFICIENT EVIDENCE") == 1
    assert _value(text, "divesafe_stage_duration_seconds") == 8
    assert _value(text, "divesafe_knowledge_retrieval", outcome="not_configured") == 1
    assert _value(text, "divesafe_runs_in_flight") == 0


def test_no_caller_chosen_or_identifying_value_ever_reaches_a_label() -> None:
    client, _ = _client()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    entries = dict(state.authenticator._hashes)
    marker = "MARKERactor"
    entries[_hash("key-marker")] = auth_module.KeyEntry(marker, frozenset(Role) - {Role.METRICS})
    state.authenticator = ApiKeyAuthenticator(entries)
    headers = {
        "Authorization": "Bearer key-marker",
        "X-Request-ID": "MARKERrid",
        "Idempotency-Key": "MARKERidempotency0001",
    }
    created = create_and_poll(client, {**PLAN_D, "decider": "decider-user"}, headers)
    record = created.json()["record"]
    client.get("/v1/sites", headers={"Authorization": "Bearer MARKERbadkey"})
    text = _scrape(client)
    request_ids = {created.headers.get("x-request-id")}
    for value in (marker, "MARKER", record["id"], *request_ids, "key-marker", "alice"):
        assert value and value not in text, value
    assert record["plan"]["site_id"] not in text
    assert _value(text, "divesafe_auth_failures") == 1


def _stored_record(client: TestClient, make: Callable[[], str]) -> Any:
    aid = make()
    return client.app.state.divesafe.repository.get(aid)  # type: ignore[attr-defined]


def test_unknown_connector_names_and_unknown_call_outcomes_are_never_labels() -> None:
    client, make = _client()
    record = _stored_record(client, make)
    observe = record.stages[0].model_copy(
        update={
            "issues": (
                "observe.connector_failed:marker-connector",
                "observe.connector_timed_out:open-meteo-marine",
            )
        }
    )
    poisoned = record.model_copy(update={"stages": (observe, *record.stages[1:])})
    metrics = Metrics(["open-meteo-marine", "MARKER bad", "other", "unnamed_1"], lambda: 0)
    trace = RunTrace(
        assessment_id=record.id,
        run_outcome="stored",
        started_ns=0,
        finished_ns=1,
        model_calls=(
            ModelCallEntry({"outcome": "MARKERoutcome"}, 0),
            ModelCallEntry({"outcome": "ok"}, 0),
            ModelCallEntry({"outcome": 7}, 0),
        ),
        stages=(),
    )
    metrics.run_finished(poisoned, None, trace)
    text = metrics.render().decode()
    assert "MARKER" not in text and "marker-connector" not in text and "unnamed_1" not in text
    assert _value(text, "divesafe_connector_failures", connector="other", kind="failed") == 1
    assert (
        _value(text, "divesafe_connector_failures", connector="open-meteo-marine", kind="timed_out")
        == 1
    )
    assert _value(text, "divesafe_model_calls") == 1
    assert _value(text, "divesafe_model_calls", outcome="ok") == 1


# ---- limits -----------------------------------------------------------------------------------


def test_metrics_has_its_own_rate_limit() -> None:
    client, _ = _client(default_limits(metrics_per_minute=2, read_per_minute=1000))
    codes = [client.get("/metrics", headers=SCRAPER).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/v1/sites", headers=HEADERS[Role.VIEWER]).status_code == 200


def test_a_bad_key_on_metrics_counts_toward_the_failed_authentication_limit() -> None:
    client, _ = _client(default_limits(auth_failures_per_minute=2))
    assert [client.get("/metrics", headers=BAD).status_code for _ in range(2)] == [401, 401]
    assert client.get("/metrics", headers=SCRAPER).status_code == 429
    assert client.get("/v1/sites", headers=HEADERS[Role.VIEWER]).status_code == 429


def test_the_auth_failure_counter_counts_401s_only() -> None:
    client, _ = _client()
    client.get("/v1/sites", headers=BAD)
    client.get("/v1/sites")
    client.get("/metrics", headers=HEADERS[Role.VIEWER])  # 403, not an authentication failure
    assert _value(_scrape(client), "divesafe_auth_failures") == 2


def test_the_settings_default_is_thirty_a_minute() -> None:
    from divesafe.config import Settings

    assert Settings().rate_limit_metrics_per_minute == 30
    assert default_limits()["metrics"].requests == 30
    assert KEYS[Role.METRICS]


# ---- never changes a run ----------------------------------------------------------------------


class _Recording:
    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.store: Any = None
        self.calls: list[tuple[Any, FailureCode | None, RunTrace | None]] = []
        self.seen: list[tuple[bool, RunState | None]] = []

    def run_finished(self, record: Any, failure: FailureCode | None, trace: Any) -> None:
        run = self.store.get_run(tr.RUN_ID) if self.store is not None else None
        stored = self.store.get(tr.RUN_ID) is not None if self.store is not None else False
        self.seen.append((stored, None if run is None else run.state))
        self.calls.append((record, failure, trace))
        if self.raises:
            raise RuntimeError("metrics failed")


def _manager_with(metrics: _Recording, base: type[Any] = api_runner.RunManager) -> type[Any]:
    class _Manager(base):  # type: ignore[misc, valid-type]
        def __init__(self, store: Any, **kwargs: Any) -> None:
            metrics.store = store
            super().__init__(store, metrics=metrics, **kwargs)

    return _Manager


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("path_name", list(tr.PATHS))
def test_metrics_never_change_the_run(
    path_name: str,
    raises: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _assert_unchanged(path_name, raises, monkeypatch, caplog, api_runner.RunManager)


def _assert_unchanged(
    path_name: str,
    raises: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    base: type[Any],
) -> None:
    path = tr.PATHS[path_name]
    with tr._applied(monkeypatch, path.patches):
        off = tr.run_once(path, None, caplog)
        metrics = _Recording(raises)
        on = tr.run_once(path, None, caplog, manager_type=_manager_with(metrics, base))
    # A mutant logs as `<module>_mutant`; only its behaviour may differ, never its name.
    errors = tuple((name.removesuffix("_mutant"), message) for name, message in on.errors)
    assert dataclasses.replace(on, errors=errors) == off
    assert (on.state, on.code, on.ending) == tr.EXPECTED[path_name]
    assert len(metrics.calls) == 1, "a run must be counted exactly once"
    record, failure, trace = metrics.calls[0]
    assert failure == on.code
    assert (record is None) == (on.code is not None)
    assert trace is not None
    if path_name in ("ok", "call_timeout"):
        assert trace.model_calls
    seen = metrics.seen[0]
    if on.code is None:
        assert seen[0], "counted before the record was stored"
    else:
        assert seen == (False, RunState.FAILED), "counted before the run was marked failed"


@pytest.mark.parametrize("path_name", list(tr.PATHS))
def test_each_run_path_is_counted_once_under_its_own_outcome(
    path_name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = Metrics(["open-meteo-marine", "data-gov-my-weather-warning"], lambda: 0)

    class _Manager(api_runner.RunManager):
        def __init__(self, store: Any, **kwargs: Any) -> None:
            super().__init__(store, metrics=real, **kwargs)

    path = tr.PATHS[path_name]
    with tr._applied(monkeypatch, path.patches):
        outcome = tr.run_once(path, None, caplog, manager_type=_Manager)
    text = real.render().decode()
    label = (
        f"failed_{outcome.code.value}"
        if outcome.code is not None
        else outcome.record["final_recommendation"]  # type: ignore[index]
    )
    assert _value(text, "divesafe_assessments_finished") == 1
    assert _value(text, "divesafe_assessments_finished", outcome=label) == 1
    stages = 0 if outcome.code is not None else len(outcome.record["stages"])  # type: ignore[index]
    assert _value(text, "divesafe_stage_duration_seconds") == stages


_SUPPRESS = (
    "            if self._metrics is not None:\n"
    "                with contextlib.suppress(Exception):\n"
)


_COMPLETE = (
    "            stored = await run_in_threadpool(self._store.complete, record, run_token)\n"
)
_MARK = "                await self._mark_failed(run_id, run_token, code)\n"
_EARLY = (
    "            if self._metrics is not None:\n"
    "                self._metrics.run_finished(None, None, None)\n"
)
_EARLY_FAILED = (
    "                if self._metrics is not None:\n"
    "                    self._metrics.run_finished(None, code, None)\n"
)
_UNCOLLECTED = "        if self._sink is not None or self._metrics is not None:\n"


@pytest.mark.parametrize(
    ("path_name", "raises", "edit"),
    [
        (
            "ok",
            True,
            (_SUPPRESS, "            if self._metrics is not None:\n                if True:\n"),
        ),
        ("ok", False, (_COMPLETE, _EARLY + _COMPLETE)),
        ("run_deadline", False, (_MARK, _EARLY_FAILED + _MARK)),
        ("ok", False, (_UNCOLLECTED, "        if self._sink is not None:\n")),
        (
            "supervise_breach",
            False,
            (
                "self._metrics.run_finished(stored_record, code, trace)",
                "self._metrics.run_finished(record, code, trace)",
            ),
        ),
    ],
)
def test_mutation_the_run_checks_have_teeth(
    path_name: str,
    raises: bool,
    edit: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mutant = mutated_module(api_runner, edit)
    with pytest.raises(AssertionError):
        _assert_unchanged(path_name, raises, monkeypatch, caplog, mutant.RunManager)


_LATE = "self._metrics.run_finished(stored_record, code, trace)"


@pytest.mark.parametrize("path_name", ["run_deadline", "cancellation", "reason_crash"])
def test_mutation_counting_only_before_marked_failed_is_caught(
    path_name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The run is counted once, but before `_mark_failed`: only the `seen` check can catch it."""
    early = _EARLY_FAILED.replace(
        "(None, code, None)", "(None, code, collector.finish(run_id, 'm'))"
    )
    mutant = mutated_module(api_runner, (_MARK, early + _MARK), (_LATE, "None"))
    with pytest.raises(AssertionError, match="marked failed"):
        _assert_unchanged(path_name, False, monkeypatch, caplog, mutant.RunManager)
