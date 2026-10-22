"""Database role names the knowledge loader may never use. No imports, so config can use it."""

from __future__ import annotations

DEFAULT_APP_ROLE = "divesafe_app"
RESERVED_ROLE_NAMES = ("divesafe_bootstrap", "divesafe_owner", DEFAULT_APP_ROLE)
