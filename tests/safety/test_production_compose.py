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
    """A setting named like a secret may only point at a file or be a required variable that the
    operator's own settings file fills in (never a literal value)."""
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
    assert not re.search(r"echo[^\n]*(\$owner_password|\$app_password)", script)
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
    """The live evaluation runs that used 3000 tokens also used a 120 s call and a 300 s stage.
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


def test_the_bootstrap_superuser_is_not_reachable_over_the_network() -> None:
    hba = (COMPOSE.parent / "db" / "pg_hba.conf").read_text()
    rules = [line.split() for line in hba.splitlines() if line.strip() and line[0] != "#"]
    for rule in rules:
        if rule[0] == "hostssl":
            assert rule[1] == "divesafe", rule
            assert rule[2] in ("divesafe_owner", "divesafe_app"), rule
            assert rule[3] == "samenet", rule
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
        if re.search(r"\$\{?(bootstrap|owner|app)_password", line):
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
    assert [x for x in instructions if x.startswith("FROM ")] == ["FROM python:3.12-slim"]
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
