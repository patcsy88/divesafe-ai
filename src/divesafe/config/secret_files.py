"""Secrets read from files in a directory such as /run/secrets (ADR 0024).

A container that gets its secrets through the environment shows them in `docker inspect` and in
/proc/<pid>/environ. A file mounted only into the service that needs it does not. Only the names
in SECRET_FILES are read, so another file in the directory can never set an ordinary setting. A
value given both here and in the environment is refused, not resolved by precedence. Messages name
a field and a reason, never a value.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Collection, Mapping
from pathlib import Path

from pydantic import SecretStr

# field name -> whether the value may span several lines (a pretty-printed JSON object)
SECRET_FILES: dict[str, bool] = {
    "database_url": False,
    "admin_database_url": False,
    "llm_api_key": False,
    "met_api_token": False,
    "api_key_hashes": True,
    # Read only by the knowledge loader's narrow settings model (ADR 0024 A4); the API refuses it.
    "loader_database_url": False,
}
LOADER_SECRET_FIELD = "loader_database_url"
FILE_PREFIX = "divesafe_"
ENV_PREFIX = "DIVESAFE_"
MAX_SECRET_BYTES = 65_536


class SecretFileError(ValueError):
    """A secret file is unusable. The message never contains the file's content."""


def secret_file_name(field: str) -> str:
    return f"{FILE_PREFIX}{field}"


def read_secret_files(
    directory: Path, environ: Mapping[str, str], fields: Collection[str] | None = None
) -> dict[str, SecretStr]:
    """The secrets found in `directory`, by Settings field name. An empty or blank file means
    "not set", as an empty environment variable does. `fields` limits which files are opened (a
    name not in SECRET_FILES is ignored); None means every field except the loader's."""
    if not directory.is_dir():
        raise SecretFileError("DIVESAFE_SECRETS_DIR is not a directory")
    wanted = (
        {f for f in SECRET_FILES if f != LOADER_SECRET_FIELD} if fields is None else set(fields)
    )
    set_in_environment = {name.upper() for name, value in environ.items() if value}
    found: dict[str, SecretStr] = {}
    for field, multiline in SECRET_FILES.items():
        if field not in wanted:
            continue
        path = directory / secret_file_name(field)
        if not os.path.lexists(path):
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError:
            raise SecretFileError(f"{path.name} cannot be read") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SecretFileError(f"{path.name} is not a file")
            chunks: list[bytes] = []
            remaining = MAX_SECRET_BYTES + 1
            while remaining > 0 and (chunk := os.read(descriptor, remaining)):
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            raise SecretFileError(f"{path.name} cannot be read") from None
        finally:
            os.close(descriptor)
        if len(raw) > MAX_SECRET_BYTES:
            raise SecretFileError(f"{path.name} is larger than {MAX_SECRET_BYTES} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretFileError(f"{path.name} is not UTF-8 text") from None
        if "\x00" in text:
            raise SecretFileError(f"{path.name} contains a NUL character")
        text = text.strip()
        if not text:
            continue
        if not multiline and not text.isprintable():
            raise SecretFileError(f"{path.name} must hold a single line of printable text")
        if f"{ENV_PREFIX}{field}".upper() in set_in_environment:
            raise SecretFileError(
                f"{ENV_PREFIX}{field.upper()} is set in the environment and in {path.name}; "
                "set only one"
            )
        found[field] = SecretStr(text)
    return found
