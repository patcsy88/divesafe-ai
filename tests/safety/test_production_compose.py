"""The production compose file (ADR 0024) must keep secrets out of the environment and the owner
credentials out of the API container. A text scan, because YAML is not a dependency: it reads the
file by indentation, which holds for this file's layout and fails loudly if the layout changes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.production.yml"
SECRET_WORDS = ("PASSWORD", "API_KEY", "TOKEN", "KEY_HASHES", "DATABASE_URL")
TOKEN_BUDGET_KEY = "DIVESAFE_LLM_MAX_OUTPUT_TOKENS"


def _blocks(text: str, top: str) -> dict[str, list[str]]:
    """The children (two-space indent) of a top-level key, each with its own lines."""
    lines = text.splitlines()
    start = lines.index(f"{top}:")
    out: dict[str, list[str]] = {}
    name: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([a-z0-9_-]+):\s*(\{\})?", line)
        if match:
            name = match.group(1)
            out[name] = []
        elif name is not None:
            out[name].append(line)
    return out


def _strip_comments(raw: str) -> str:
    """Drop whole-line and trailing comments so a comment can never satisfy a check. The compose
    file holds no '#' inside a value."""
    return "\n".join(
        re.sub(r"\s+#.*$", "", line) for line in raw.splitlines() if line.strip()[:1] != "#"
    )


@pytest.fixture(scope="module")
def text() -> str:
    return _strip_comments(COMPOSE.read_text())


@pytest.fixture(scope="module")
def services(text: str) -> dict[str, list[str]]:
    found = _blocks(text, "services")
    assert set(found) == {"db-tls", "db", "db-init", "api"}, "a service was added or removed"
    return found


def _environment(block: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    inside = False
    for line in block:
        if re.fullmatch(r"    environment:\s*", line):
            inside = True
            continue
        if inside:
            if not line.startswith("      "):
                inside = False
                continue
            if line.lstrip().startswith("#"):
                continue
            key, _, value = line.strip().partition(":")
            env[key] = value.strip()
    return env


def _secret_targets(block: list[str]) -> set[str]:
    joined = "\n".join(block)
    names = set(re.findall(r"- source: (\w+)", joined))
    names |= set(re.findall(r"^      - (\w+)\s*$", joined, re.MULTILINE))
    return names


def test_no_service_takes_the_whole_env_file(text: str) -> None:
    assert not re.search(r"^\s*env_file:", text, re.MULTILINE)


def _secret_name_problems(environments: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    """Settings named like a secret that are not a file path under /run/secrets. One exact name,
    on the api service only, is let through: "TOKEN" in DIVESAFE_LLM_MAX_OUTPUT_TOKENS means model
    output tokens, and only a plain 3 or 4 digit default is accepted for it."""
    problems: list[tuple[str, str]] = []
    pattern = rf"\$\{{{TOKEN_BUDGET_KEY}:-[0-9]{{3,4}}\}}"
    for name, environment in environments.items():
        for key, value in environment.items():
            if key == TOKEN_BUDGET_KEY:
                if name != "api" or not re.fullmatch(pattern, value):
                    problems.append((name, key))
            elif any(word in key.upper() for word in SECRET_WORDS) and not (
                key.endswith("_FILE") and value.startswith("/run/secrets/")
            ):
                problems.append((name, key))
    return problems


def test_no_secret_value_is_in_any_environment(services: dict[str, list[str]]) -> None:
    """A setting named like a secret may only be a file path under /run/secrets (a *_FILE setting);
    one exact key, the model output token budget, is checked separately (never a literal value)."""
    assert _secret_name_problems({n: _environment(b) for n, b in services.items()}) == []


def test_the_secret_name_check_rejects_each_way_round_the_one_exception() -> None:
    """The check itself must fail for each of these, so loosening the exception is noticed."""
    good = f"${{{TOKEN_BUDGET_KEY}:-3000}}"
    assert _secret_name_problems({"api": {TOKEN_BUDGET_KEY: good}}) == []
    bad = {
        "a literal value": {"api": {TOKEN_BUDGET_KEY: "sk-literal"}},
        "no default": {"api": {TOKEN_BUDGET_KEY: f"${{{TOKEN_BUDGET_KEY}}}"}},
        "a non-numeric default": {"api": {TOKEN_BUDGET_KEY: f"${{{TOKEN_BUDGET_KEY}:-abc}}"}},
        "a default with a suffix": {"api": {TOKEN_BUDGET_KEY: good + "x"}},
        "a different variable inside": {"api": {TOKEN_BUDGET_KEY: "${DIVESAFE_LLM_API_KEY:-3000}"}},
        "a quoted value": {"api": {TOKEN_BUDGET_KEY: '"' + good + '"'}},
        "the key on db-init": {"db-init": {TOKEN_BUDGET_KEY: good}},
        "a second TOKEN-named literal": {"api": {"DIVESAFE_OTHER_TOKEN": "abc123"}},
        "the key in lower case": {"api": {TOKEN_BUDGET_KEY.lower(): "abc123"}},
        "a near-name literal": {"api": {TOKEN_BUDGET_KEY + "_X": "abc123"}},
        "a credential named like the secret files": {"api": {"DIVESAFE_MET_API_TOKEN": "abc"}},
    }
    for label, environments in bad.items():
        assert _secret_name_problems(environments), label


def test_the_api_never_sees_the_owner_credentials(services: dict[str, list[str]]) -> None:
    api = "\n".join(services["api"])
    assert "admin_database_url" not in api and "db_owner_password" not in api
    assert "ADMIN_DATABASE_URL" not in api and "OWNER" not in api.upper()
    assert _secret_targets(services["api"]) == {
        "database_url",
        "llm_api_key",
        "api_key_hashes",
        "met_api_token",
    }


def test_only_the_schema_job_sees_the_owner_url(services: dict[str, list[str]]) -> None:
    holders = {name for name, block in services.items() if "admin_database_url" in "\n".join(block)}
    assert holders == {"db-init"}


def test_the_api_runs_in_production_with_the_agent_stage_on(
    services: dict[str, list[str]],
) -> None:
    env = _environment(services["api"])
    assert env["DIVESAFE_ENVIRONMENT"] == "production"
    assert env["DIVESAFE_LLM_PROVIDER"] not in ("fake", "")
    assert env["DIVESAFE_ALLOW_EXTERNAL_LLM"] == '"true"'
    assert "DIVESAFE_LLM_SERVER_IS_LOCAL" not in env, "contradicts the external opt-in (ADR 0023)"
    assert "DIVESAFE_AUTH_MODE" not in env or "dev" not in env["DIVESAFE_AUTH_MODE"]
    assert env["DIVESAFE_SECRETS_DIR"] == "/run/secrets"


def test_the_model_host_and_the_evidence_age_have_no_default(
    services: dict[str, list[str]],
) -> None:
    """A forgotten setting must stop the stack, not fall back to a value nobody chose."""
    env = _environment(services["api"])
    for key in (
        "DIVESAFE_LLM_BASE_URL",
        "DIVESAFE_LLM_ALLOWED_HOSTS",
        "DIVESAFE_LLM_MODEL",
        "DIVESAFE_EVIDENCE_MAX_AGE_MINUTES",
    ):
        assert env[key].startswith("${") and ":?" in env[key], key


def test_the_database_is_not_published_and_the_data_network_has_no_route_out(
    text: str, services: dict[str, list[str]]
) -> None:
    for name in ("db", "db-tls", "db-init"):
        assert not any(line.strip() == "ports:" for line in services[name]), name
    networks = _blocks(text, "networks")
    assert "internal: true" in "\n".join(networks["data"])
    assert "internal: true" not in "\n".join(networks["egress"])
    assert "egress" not in "\n".join(services["db"] + services["db-init"] + services["db-tls"])


def test_the_api_port_is_bound_to_loopback_only(services: dict[str, list[str]]) -> None:
    ports: list[str] = []
    inside = False
    for line in services["api"]:
        if line == "    ports:":
            inside = True
        elif inside and line.startswith("      - "):
            ports.append(line.strip())
        elif inside:
            inside = False
    assert ports, "the API publishes no port; update the scan"
    assert all(p.startswith('- "127.0.0.1:') for p in ports), ports


def test_every_service_drops_capabilities_and_blocks_privilege_gain(
    services: dict[str, list[str]],
) -> None:
    for name, block in services.items():
        joined = "\n".join(block)
        assert "cap_drop: [ALL]" in joined, name
        assert "no-new-privileges:true" in joined, name


def test_the_api_and_the_schema_job_have_a_read_only_filesystem(
    services: dict[str, list[str]],
) -> None:
    for name in ("api", "db-init"):
        assert "read_only: true" in "\n".join(services[name]), name


def test_the_database_accepts_only_tls_connections() -> None:
    hba = (COMPOSE.parent / "db" / "pg_hba.conf").read_text()
    rules = [line.split() for line in hba.splitlines() if line.strip() and line[0] != "#"]
    assert rules, "no rules"
    for rule in rules:
        assert rule[0] in ("local", "hostssl"), rule
        assert rule[-1] == "scram-sha-256", rule


def test_the_app_role_is_created_without_privileges() -> None:
    script = (COMPOSE.parent / "db" / "10-create-app-role.sh").read_text()
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in script
    assert "GRANT SELECT" not in script and "GRANT ALL" not in script


def test_the_secrets_script_never_prints_or_overwrites_a_secret() -> None:
    script = (COMPOSE.parent / "make-secrets.sh").read_text()
    assert '[ -e "$target" ]' in script
    assert "umask 077" in script
    assert not re.search(r"echo[^\n]*(\$owner_password|\$app_password|\$loader_password)", script)
    assert not re.search(r"\b(set -x|set -.*x)\b", script)


def test_generated_secrets_and_the_filled_in_settings_are_git_ignored() -> None:
    ignore = (COMPOSE.parents[1] / ".gitignore").read_text().splitlines()
    assert "/docker/secrets/" in ignore and "/docker/production.env" in ignore


def _value(block: list[str], key: str) -> str | None:
    for line in block:
        match = re.fullmatch(rf"    {re.escape(key)}:\s*(.*)", line)
        if match:
            return match.group(1).strip()
    return None


def test_the_file_uses_no_yaml_feature_the_scan_cannot_see(text: str) -> None:
    for token in ("<<:", "extends:", "include:", "network_mode: host", "privileged", "pid:"):
        assert token not in text.replace("network_mode: none", ""), token
    assert not re.search(r"(^|\s)[&*][A-Za-z]", text, re.MULTILINE)
    assert not re.search(r"^x-", text, re.MULTILINE)


def test_each_service_is_on_exactly_the_networks_it_should_be(
    services: dict[str, list[str]],
) -> None:
    assert _value(services["db"], "networks") == "[data]"
    assert _value(services["db-init"], "networks") == "[data]"
    assert _value(services["api"], "networks") == "[data, egress]"
    assert _value(services["db-tls"], "network_mode") == "none"
    assert _value(services["db-tls"], "networks") is None


def test_no_database_service_publishes_a_port_in_any_form(services: dict[str, list[str]]) -> None:
    for name in ("db", "db-tls", "db-init"):
        assert _value(services[name], "ports") is None, name
        assert _value(services[name], "expose") is None, name


def test_capabilities_are_the_expected_ones_and_nothing_adds_more(
    services: dict[str, list[str]],
) -> None:
    assert _value(services["api"], "cap_add") is None
    assert _value(services["db-init"], "cap_add") is None
    assert _value(services["db"], "cap_add") == "[CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]"
    assert _value(services["db-tls"], "cap_add") == "[CHOWN, DAC_OVERRIDE, FOWNER]"
    for name, block in services.items():
        assert _value(block, "cap_drop") == "[ALL]", name
        assert _value(block, "privileged") is None, name
        assert _value(block, "pid") is None, name
        assert "docker.sock" not in "\n".join(block), name


def test_the_api_and_the_schema_job_do_not_run_as_root_or_write_to_the_image(
    services: dict[str, list[str]],
) -> None:
    for name in ("api", "db-init"):
        assert _value(services[name], "read_only") == "true", name
        assert _value(services[name], "volumes") is None, name


def test_the_api_has_resource_limits_and_a_non_executable_tmp(
    services: dict[str, list[str]],
) -> None:
    assert _value(services["api"], "mem_limit")
    assert _value(services["api"], "pids_limit")
    assert "noexec" in (_value(services["api"], "tmpfs") or "")


def test_the_api_gets_exactly_these_settings(services: dict[str, list[str]]) -> None:
    """No other setting (for example the PHN connector or a declaration) may be added silently."""
    assert set(_environment(services["api"])) == {
        "DIVESAFE_ENVIRONMENT",
        "DIVESAFE_LOG_LEVEL",
        "DIVESAFE_SECRETS_DIR",
        "DIVESAFE_LLM_PROVIDER",
        "DIVESAFE_ALLOW_EXTERNAL_LLM",
        "DIVESAFE_LLM_BASE_URL",
        "DIVESAFE_LLM_ALLOWED_HOSTS",
        "DIVESAFE_LLM_MODEL",
        "DIVESAFE_LLM_MAX_OUTPUT_TOKENS",
        "DIVESAFE_LLM_CALL_TIMEOUT_SECONDS",
        "DIVESAFE_LLM_STAGE_TIMEOUT_SECONDS",
        "DIVESAFE_EVIDENCE_MAX_AGE_MINUTES",
        "DIVESAFE_DECISION_MAX_AGE_MINUTES",
        "DIVESAFE_TRUSTED_PROXIES",
    }


def test_the_output_token_budget_defaults_to_the_value_the_live_runs_used(
    services: dict[str, list[str]],
) -> None:
    """The 1500 default lost four agent calls in the first real run. The value is a budget for
    one model call, not a safety limit; the schema ceiling and the refusal of long replies stay."""
    value = _environment(services["api"])["DIVESAFE_LLM_MAX_OUTPUT_TOKENS"]
    assert value == "${DIVESAFE_LLM_MAX_OUTPUT_TOKENS:-3000}"
    example = (COMPOSE.parent / "production.env.example").read_text()
    assert "DIVESAFE_LLM_MAX_OUTPUT_TOKENS=3000" in example.splitlines()


def test_the_model_time_budgets_travel_with_the_token_budget(
    services: dict[str, list[str]],
) -> None:
    """Evaluation runs 2 and 3 used 3000 tokens together with a 120 s call and a 300 s stage.
    At the code defaults (60 s and 150 s) a longer reply could time out instead of finishing."""
    environment = _environment(services["api"])
    assert (
        environment["DIVESAFE_LLM_CALL_TIMEOUT_SECONDS"]
        == "${DIVESAFE_LLM_CALL_TIMEOUT_SECONDS:-120}"
    )
    assert (
        environment["DIVESAFE_LLM_STAGE_TIMEOUT_SECONDS"]
        == "${DIVESAFE_LLM_STAGE_TIMEOUT_SECONDS:-300}"
    )
    lines = (COMPOSE.parent / "production.env.example").read_text().splitlines()
    assert "DIVESAFE_LLM_CALL_TIMEOUT_SECONDS=120" in lines
    assert "DIVESAFE_LLM_STAGE_TIMEOUT_SECONDS=300" in lines


def test_the_secret_names_point_at_the_files_they_are_named_for(text: str) -> None:
    mapping = {
        name: re.search(r"file: \./secrets/(\w+)", "\n".join(lines)).group(1)  # type: ignore[union-attr]
        for name, lines in _blocks(text, "secrets").items()
    }
    assert mapping == {
        "db_bootstrap_password": "db_bootstrap_password",
        "db_owner_password": "db_owner_password",
        "db_app_password": "db_app_password",
        "admin_database_url": "divesafe_admin_database_url",
        "database_url": "divesafe_database_url",
        "llm_api_key": "divesafe_llm_api_key",
        "api_key_hashes": "divesafe_api_key_hashes",
        "met_api_token": "divesafe_met_api_token",
    }


def test_the_database_command_turns_tls_on_and_loads_the_tls_only_rules(
    services: dict[str, list[str]],
) -> None:
    command = "\n".join(services["db"])
    assert "ssl=on" in command
    assert "hba_file=/etc/postgresql/pg_hba.conf" in command
    assert "./db/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro" in command


_NETWORK_ROLES = {"divesafe_owner", "divesafe_app", "divesafe_loader"}


def _hba_rules() -> list[list[str]]:
    hba = (COMPOSE.parent / "db" / "pg_hba.conf").read_text()
    return [line.split() for line in hba.splitlines() if line.strip() and line[0] != "#"]


def test_the_database_rules_are_exactly_these_four_in_order() -> None:
    """ADR 0024 A2: the local socket rule, then one TLS rule per network role. An added `local`
    or `host` rule, a CIDR, `all`, `trust` or a rule for the bootstrap role fails here."""
    assert _hba_rules() == [
        ["local", "all", "all", "scram-sha-256"],
        ["hostssl", "divesafe", "divesafe_owner", "samenet", "scram-sha-256"],
        ["hostssl", "divesafe", "divesafe_app", "samenet", "scram-sha-256"],
        ["hostssl", "divesafe", "divesafe_loader", "samenet", "scram-sha-256"],
    ]


def test_the_bootstrap_superuser_is_not_reachable_over_the_network() -> None:
    hba = (COMPOSE.parent / "db" / "pg_hba.conf").read_text()
    rules = [line.split() for line in hba.splitlines() if line.strip() and line[0] != "#"]
    network_roles = set()
    for rule in rules:
        if rule[0] == "hostssl":
            assert rule[1] == "divesafe", rule
            assert rule[2] in _NETWORK_ROLES, rule
            assert rule[3] == "samenet", rule
            network_roles.add(rule[2])
    assert network_roles == _NETWORK_ROLES, "exactly these roles, never a pattern"
    assert not any("bootstrap" in " ".join(r) for r in rules)
    assert not any(":" in field for rule in rules for field in rule[3:-1]), "no IPv6 ranges"


_ROLE_SCRIPT_LINES = [
    "set -eu",
    'OWNER_PW="$(cat /run/secrets/db_owner_password)" '
    'APP_PW="$(cat /run/secrets/db_app_password)" \\',
    '  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \\',
    "  --set=db_name=\"$POSTGRES_DB\" <<'SQL'",
    "\\getenv owner_password OWNER_PW",
    "\\getenv app_password APP_PW",
    "SET log_min_error_statement = panic;",
    "CREATE ROLE divesafe_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
    "NOBYPASSRLS",
    "  PASSWORD :'owner_password';",
    "CREATE ROLE divesafe_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
    "  PASSWORD :'app_password';",
    'ALTER DATABASE :"db_name" OWNER TO divesafe_owner;',
    'REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;',
    'GRANT CONNECT ON DATABASE :"db_name" TO divesafe_owner, divesafe_app;',
    "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;",
    "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;",
    "SQL",
]


def _role_script_lines() -> list[str]:
    """Split on newline only (the shell does), refuse anything but printable ASCII, and drop
    comments by context: `#` lines before the heredoc, `--` lines inside it. A `--` line before
    the heredoc is a psql option, not a comment."""
    raw = (COMPOSE.parent / "db" / "10-create-app-role.sh").read_bytes()
    assert all(b == 10 or 32 <= b < 127 for b in raw), "only printable ASCII and newlines"
    out: list[str] = []
    in_sql = False
    for line in raw.decode("ascii").split("\n"):
        if not line:
            continue
        if line.startswith("#") and not in_sql:
            continue
        if line.startswith("--") and in_sql:
            continue
        out.append(line)
        in_sql = in_sql or line.endswith("<<'SQL'")
    return out


def test_the_role_script_is_exactly_these_lines() -> None:
    """Every line that is not a comment, in order, with no trailing space: the shell line that
    reads the two password files (each into its own variable), the single psql call, the two
    psql variables, and the statements. Any change (a swapped password, an extra statement or
    psql option, anything after the terminator, a second psql call) fails here."""
    assert _role_script_lines() == _ROLE_SCRIPT_LINES


def test_the_role_script_filter_keeps_a_psql_option_that_starts_with_two_dashes() -> None:
    """The filter is what is under test: a `--` line before the heredoc is shell, not a comment."""
    lines = _role_script_lines()
    assert "  --set=db_name=\"$POSTGRES_DB\" <<'SQL'" in lines
    assert not any(line.startswith("#") for line in lines)


def test_the_secrets_script_prints_no_secret_by_any_command() -> None:
    """Every use of a generated password is the start of a pipe into write_new (which writes a
    file) or the assignment that creates it. Continuation lines are joined first."""
    script = (COMPOSE.parent / "make-secrets.sh").read_text().replace("|\n", "| ")
    for line in script.splitlines():
        if re.search(r"\$\{?(bootstrap|owner|app|loader)_password", line):
            assert re.search(r"\|\s*write_new\b", line), line


def test_the_docker_build_context_excludes_the_secrets() -> None:
    ignore = (COMPOSE.parents[1] / ".dockerignore").read_text().splitlines()
    assert "docker/secrets" in ignore and "docker/production.env" in ignore


def test_decisions_have_an_age_limit_of_180_minutes_unless_overridden(
    services: dict[str, list[str]],
) -> None:
    """The owner chose 180 minutes (2026-10-18). An empty default would leave decisions on old
    assessments unlimited, as ADR 0004 allows overrides to a less severe outcome."""
    value = _environment(services["api"])["DIVESAFE_DECISION_MAX_AGE_MINUTES"]
    assert value == "${DIVESAFE_DECISION_MAX_AGE_MINUTES:-180}"
    example = (COMPOSE.parent / "production.env.example").read_text()
    assert "DIVESAFE_DECISION_MAX_AGE_MINUTES=180" in example.splitlines()


def test_no_service_loosens_its_isolation_in_a_way_the_other_checks_do_not_see(
    services: dict[str, list[str]],
) -> None:
    for name, block in services.items():
        for key in ("ipc", "userns_mode", "cgroup", "cgroup_parent", "devices", "uts", "sysctls"):
            assert _value(block, key) is None, (name, key)
        assert _value(block, "security_opt") == '["no-new-privileges:true"]', name
        if name in ("api", "db-init"):
            user = _value(block, "user")
            parts = (user or "10001").strip('"').split(":")
            assert all(part not in ("root", "0") and not part.startswith("0") for part in parts), (
                name
            )
    assert _value(services["api"], "entrypoint") is None
    assert _value(services["api"], "command") is None
    assert _value(services["db-init"], "command") == '["python", "-m", "divesafe.services.db_init"]'
    assert _value(services["db-init"], "entrypoint") is None


def test_the_database_command_is_exactly_this_list() -> None:
    """Any other server setting (for example session_replication_role) needs a review."""
    block = _blocks(_strip_comments(COMPOSE.read_text()), "services")["db"]
    start = block.index("    command:")
    command: list[str] = []
    for line in block[start + 1 :]:
        match = re.fullmatch(r"      - (.*)", line)
        if not match:
            break
        command.append(match.group(1))
    assert command == [
        "postgres",
        "-c",
        "ssl=on",
        "-c",
        "ssl_cert_file=/tls/server.crt",
        "-c",
        "ssl_key_file=/tls/server.key",
        "-c",
        "hba_file=/etc/postgresql/pg_hba.conf",
    ]


def test_the_docker_build_context_excludes_env_files_and_keys_at_any_depth() -> None:
    ignore = [x.strip() for x in (COMPOSE.parents[1] / ".dockerignore").read_text().splitlines()]
    for pattern in ("**/.env", "**/.env.*", "**/*.pem", "**/*.key"):
        assert pattern in ignore, pattern
    assert [line for line in ignore if line.startswith("!")] == ["!**/.env.example"]


def _dockerfile_instructions() -> list[str]:
    """Instruction lines with the keyword upper-cased, continuations joined, comments dropped."""
    text = (COMPOSE.parent / "Dockerfile").read_text().replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            keyword, _, rest = stripped.partition(" ")
            out.append(f"{keyword.upper()} {rest.strip()}")
    return out


def test_the_image_runs_as_the_unprivileged_user() -> None:
    """Neither service sets `user:`, so the Dockerfile decides: one stage, one USER, and no
    change to the user's groups."""
    instructions = _dockerfile_instructions()
    assert [x for x in instructions if x.startswith("FROM ")] == ["FROM python:3.14-slim"]
    assert [x for x in instructions if x.startswith("USER ")] == ["USER divesafe"]
    assert "RUN useradd --create-home --uid 10001 divesafe" in instructions
    assert not any("usermod" in x or "groupadd" in x or "adduser" in x for x in instructions)


def test_both_built_services_use_this_dockerfile_and_no_target() -> None:
    services = _blocks(_strip_comments(COMPOSE.read_text()), "services")
    for name in ("api", "db-init"):
        block = services[name]
        start = block.index("    build:")
        build = []
        for line in block[start + 1 :]:
            if not line.startswith("      "):
                break
            build.append(line.strip())
        assert build == ["context: ..", "dockerfile: docker/Dockerfile"], name
        assert _value(block, "user") is None, name


# --- the knowledge loader role script (ADR 0024 Amendment 1, A1) ---------------------------------

LOADER_SCRIPT = COMPOSE.parent / "db" / "20-create-loader-role.sql"
RUNBOOK = COMPOSE.parents[1] / "docs" / "knowledge-loader.md"
_PASSWORD_FILE = "'/run/secrets/db_loader_password'"

_LOADER_SCRIPT_LINES = [
    "\\set VERBOSITY terse",
    "\\set SHOW_CONTEXT never",
    "SET log_statement = 'none';",
    "SET log_min_duration_statement = -1;",
    "SET log_min_error_statement = panic;",
    "SET search_path = pg_catalog, pg_temp;",
    "SELECT 'divesafe_loader' IN ('divesafe_bootstrap', 'divesafe_owner', 'divesafe_app')"
    " AS loader_name_reserved \\gset",
    "\\if :loader_name_reserved",
    "\\warn 'refusing: the loader role name is reserved'",
    "SELECT 'refusing: the loader role name is reserved'::integer;",
    "\\endif",
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'divesafe_loader')"
    " AS loader_exists \\gset",
    "\\if :loader_exists",
    "SELECT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls"
    " OR NOT r.rolcanlogin OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m"
    " WHERE m.member = r.oid OR m.roleid = r.oid) OR EXISTS (SELECT 1 FROM"
    " pg_catalog.pg_db_role_setting s WHERE s.setrole = r.oid)) AS loader_role_unsafe"
    " FROM pg_catalog.pg_roles r WHERE r.rolname = 'divesafe_loader' \\gset",
    "\\if :loader_role_unsafe",
    "\\warn 'refusing: divesafe_loader exists with an attribute, a membership or a stored"
    " setting; it is not repaired'",
    "SELECT 'refusing: the existing loader role is not clean'::integer;",
    "\\endif",
    "\\else",
    "SELECT pg_catalog.pg_read_file('/run/secrets/db_loader_password') ~ '^[0-9a-f]{48}$'"
    " AS loader_password_ok \\gset",
    "\\if :loader_password_ok",
    "SELECT pg_catalog.format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
    " NOREPLICATION NOBYPASSRLS PASSWORD %L', 'divesafe_loader',"
    " pg_catalog.pg_read_file('/run/secrets/db_loader_password')) \\gexec",
    "\\else",
    "\\warn 'refusing: db_loader_password is not 48 lower-case hex characters with no newline'",
    "SELECT 'refusing: the loader password file is not in the expected form'::integer;",
    "\\endif",
    "\\endif",
    "GRANT CONNECT ON DATABASE divesafe TO divesafe_loader;",
]


def _loader_script_lines() -> list[str]:
    """Split on newline only, refuse anything but printable ASCII, drop empty lines and whole-line
    `--` comments. psql meta-commands are lines of their own."""
    raw = LOADER_SCRIPT.read_bytes()
    assert all(b == 10 or 32 <= b < 127 for b in raw), "only printable ASCII and newlines"
    return [x for x in raw.decode("ascii").split("\n") if x and not x.startswith("--")]


def test_the_loader_role_script_is_exactly_these_lines() -> None:
    """Any change (a swapped password file, an extra statement, a dropped guard or check, a table
    grant, another attribute) fails here and has to be read by both reviewers."""
    assert _loader_script_lines() == _LOADER_SCRIPT_LINES


def test_the_loader_password_is_read_by_the_server_and_never_printed() -> None:
    """The password file is read only by the server, in exactly two places: a boolean check
    (`\\gset`, never printed) and inside the one format(...) whose statement ends in `\\gexec`. A
    bare `SELECT format(...)` would print it; a DO block or EXECUTE would put it in CONTEXT."""
    lines = _loader_script_lines()
    reads = [x for x in lines if "pg_read_file" in x]
    assert len(reads) == 2, reads
    check, create = reads
    assert check.startswith("SELECT pg_catalog.pg_read_file(" + _PASSWORD_FILE + ") ~ ")
    assert check.endswith(" AS loader_password_ok \\gset")
    assert create.startswith("SELECT pg_catalog.format('CREATE ROLE %I ")
    assert create.endswith(", pg_catalog.pg_read_file(" + _PASSWORD_FILE + ")) \\gexec")
    assert all(x.count("pg_read_file") == 1 for x in reads)
    assert [x for x in lines if "format(" in x] == [create]
    assert [x for x in lines if x.endswith("\\gexec")] == [create]
    joined = "\n".join(lines).upper()
    assert not re.search(r"\bDO\b|\bEXECUTE\b|\$\$", joined)


def test_the_loader_password_file_is_checked_without_trimming() -> None:
    """A file written with `echo` ends in a newline and must be refused, not trimmed: a stored
    newline would break SCRAM logins. The pattern is anchored at both ends."""
    lines = _loader_script_lines()
    check = next(x for x in lines if "pg_read_file" in x)
    assert "~ '^[0-9a-f]{48}$' AS" in check
    for line in (x for x in lines if "pg_read_file" in x):
        for word in ("trim", "replace", "substr", "left(", "right(", "split_part", "E'", "chr("):
            assert word not in line.lower(), (word, line)


def test_the_loader_script_silences_echo_context_and_the_server_log_first() -> None:
    lines = _loader_script_lines()
    first_read = next(i for i, x in enumerate(lines) if "pg_read_file" in x)
    for setting in (
        "\\set VERBOSITY terse",
        "\\set SHOW_CONTEXT never",
        "SET log_statement = 'none';",
        "SET log_min_duration_statement = -1;",
        "SET log_min_error_statement = panic;",
    ):
        assert lines.index(setting) < first_read, setting
    text = "\n".join(lines)
    assert "ECHO" not in text and "\\echo" not in text and "\\qecho" not in text
    assert "\\o" not in text and "\\copy" not in text and "\\!" not in text


def test_the_loader_role_is_created_only_when_absent_and_never_repaired() -> None:
    lines = _loader_script_lines()
    creates = [i for i, x in enumerate(lines) if "CREATE ROLE" in x]
    assert len(creates) == 1
    guard = lines.index("\\if :loader_exists")
    other = [i for i, x in enumerate(lines) if x == "\\else"]
    assert other and guard < other[0] < creates[0], "CREATE ROLE only in the absent branch"
    password_ok = lines.index("\\if :loader_password_ok")
    assert other[0] < password_ok < creates[0]
    text = "\n".join(lines)
    assert "ALTER ROLE" not in text and "DROP" not in text and "REVOKE" not in text


def test_an_existing_loader_role_with_any_extra_right_is_refused() -> None:
    check = next(x for x in _loader_script_lines() if "AS loader_role_unsafe" in x)
    for part in (
        "r.rolsuper",
        "r.rolcreatedb",
        "r.rolcreaterole",
        "r.rolreplication",
        "r.rolbypassrls",
        "NOT r.rolcanlogin",
        "pg_catalog.pg_auth_members m WHERE m.member = r.oid OR m.roleid = r.oid",
        "pg_catalog.pg_db_role_setting s WHERE s.setrole = r.oid",
    ):
        assert part in check, part
    assert " AND " not in check, "every condition refuses on its own"


def test_each_refusal_raises_an_error_inside_its_branch() -> None:
    """`\\quit` exits zero, so a refusal must be a statement that fails under ON_ERROR_STOP."""
    lines = _loader_script_lines()
    for flag in (":loader_name_reserved", ":loader_role_unsafe"):
        start = lines.index(f"\\if {flag}")
        assert lines[start + 2].endswith("'::integer;"), flag
        assert lines[start + 3] == "\\endif", flag
    bad_file = lines.index("\\if :loader_password_ok") + 2
    assert lines[bad_file] == "\\else" and lines[bad_file + 2].endswith("'::integer;")
    assert "\\quit" not in "\n".join(lines) and "\\q" not in lines


def test_the_loader_role_is_created_without_privileges_and_only_granted_connect() -> None:
    lines = _loader_script_lines()
    create = next(x for x in lines if "CREATE ROLE" in x)
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L" in (
        create
    )
    for word in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"):
        assert re.findall(rf"\b{word}\b", create) == [], word
    assert "IN ROLE" not in create and "ADMIN" not in create and "INHERIT" not in create
    assert [x for x in lines if "GRANT" in x.upper()] == [
        "GRANT CONNECT ON DATABASE divesafe TO divesafe_loader;"
    ]


def test_the_loader_role_name_is_a_literal_reserved_names_are_refused_and_it_matches_hba() -> None:
    lines = _loader_script_lines()
    text = "\n".join(lines)
    assert ":'" not in text and ':"' not in text and "\\getenv" not in text
    assert "PASSWORD :" not in text
    assert lines[6].startswith(
        "SELECT 'divesafe_loader' IN ('divesafe_bootstrap', 'divesafe_owner', 'divesafe_app')"
    )
    names = set(re.findall(r"\bdivesafe_[a-z_]+\b", text)) - {
        "divesafe_bootstrap",
        "divesafe_owner",
        "divesafe_app",
    }
    assert names == {"divesafe_loader"}
    loader_rules = [r for r in _hba_rules() if r[0] == "hostssl" and "loader" in r[2]]
    assert [r[2] for r in loader_rules] == ["divesafe_loader"]


def test_the_loader_role_script_is_not_mounted_into_the_database_init_directory(
    text: str,
) -> None:
    """The script runs by hand on an existing volume (stdin of `exec -T`), never at init."""
    assert "20-create-loader-role" not in text


def _runbook_role_command() -> str:
    body = RUNBOOK.read_text()
    blocks = re.findall(r"```sh\n(.*?)```", body, re.S)
    found = [b for b in blocks if "20-create-loader-role.sql" in b]
    assert len(found) == 1, "exactly one runbook block runs the role script"
    return found[0]


def test_the_runbook_runs_the_role_script_with_the_password_read_inside_the_container() -> None:
    """ADR 0024 A1 (c): no `exec -e`, no psql echo option, no `$(cat` outside the single-quoted
    wrapper, and `-u postgres`. The exact text is pinned."""
    command = _runbook_role_command()
    assert command == (
        "docker compose -f docker/docker-compose.production.yml"
        " --env-file docker/production.env \\\n"
        "  exec -T -u postgres db sh -c "
        '\'PGPASSWORD="$(cat /run/secrets/db_bootstrap_password)" \\\n'
        "  exec psql -X -v ON_ERROR_STOP=1 -U divesafe_bootstrap -d divesafe -f -' \\\n"
        "  < docker/db/20-create-loader-role.sql\n"
    )
    _assert_role_command_is_quiet(command)


def _assert_role_command_is_quiet(command: str) -> None:
    flat = command.replace("\\\n", " ")
    assert "-u postgres" in flat and "exec -T" in flat
    options = flat[flat.index(" exec ") : flat.index(" db sh -c ")]
    assert options == " exec -T -u postgres", "no -e, --env or other option on exec"
    inner = flat[flat.index("sh -c '") + len("sh -c '") :]
    inner, _, outside = inner.partition("'")
    assert "$(cat" not in flat.replace(inner, ""), "the password is read inside the wrapper only"
    psql = inner[inner.index("psql") :]
    assert not re.search(r"\s-[a-zA-Z]*[eab]\b|--echo|ECHO", psql), "psql must not echo"
    assert "<" in outside


def test_the_runbook_quiet_check_rejects_each_echo_or_leak() -> None:
    good = _runbook_role_command()
    _assert_role_command_is_quiet(good)
    bad = {
        "exec -e": good.replace("exec -T", "exec -T -e PGPASSWORD=x"),
        "psql -e": good.replace("psql -X", "psql -e -X"),
        "psql -b": good.replace("psql -X", "psql -b -X"),
        "psql -a": good.replace("psql -X", "psql -a -X"),
        "ECHO": good.replace("-v ON_ERROR_STOP=1", "-v ON_ERROR_STOP=1 -v ECHO=errors"),
        "cat outside": good.replace("docker compose", 'PGPASSWORD="$(cat x)" docker compose'),
        "no -u postgres": good.replace("-u postgres ", ""),
    }
    for label, command in bad.items():
        with pytest.raises(AssertionError):
            _assert_role_command_is_quiet(command)
        assert label


def test_the_runbook_fallback_insert_names_hash_checked() -> None:
    """ADR 0024 A3: the owner `psql` fallback INSERT includes the NOT NULL column."""
    body = RUNBOOK.read_text()
    inserts = re.findall(r"INSERT INTO public\.kb_status_events \(([^)]*)\)", body)
    assert inserts == ["document_id, version, status, hash_checked"]
    assert "'WITHDRAWN', false)" in body


def test_the_secrets_script_writes_the_loader_files_with_the_approved_connect_timeout() -> None:
    script = (COMPOSE.parent / "make-secrets.sh").read_text()
    assert 'loader_password="$(random)"' in script
    assert re.search(r'printf .%s. "\$loader_password" \| write_new db_loader_password\b', script)
    assert (
        "postgresql://divesafe_loader:${loader_password}@db:5432/divesafe"
        '?sslmode=require&channel_binding=require&connect_timeout=5" |\n'
        "    write_new divesafe_loader_database_url || true"
    ) in script


def test_the_secrets_script_writes_files_only_through_write_new() -> None:
    """A bare redirect would overwrite a secret, or leave it world-readable with no message."""
    script = (COMPOSE.parent / "make-secrets.sh").read_text()
    body = script[script.index("write_new() {") :]
    function, _, rest = body.partition("\n}\n")
    assert 'cat >"$target"' in function
    assert not re.search(r">>?\s*\"?\$(dir|\{dir\})", rest), "only write_new writes a file"
    assert not re.search(r"\btee\b", rest)
