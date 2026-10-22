"""Apply schema migrations (ADR 0015) and grant the application role, as the OWNER role.

    DIVESAFE_ADMIN_DATABASE_URL=... DIVESAFE_APP_DB_ROLE=divesafe_app \\
        python -m divesafe.services.db_init

The running application connects as a different, least-privilege role (SELECT, INSERT and
UPDATE, plus DELETE on pending_runs only) and refuses to start if the guard is missing or the
role is too powerful.
"""

from __future__ import annotations

from divesafe.config.settings import load_settings
from divesafe.services.postgres import (
    LOADER_ROLE_INVALID,
    LoaderRoleError,
    check_loader_role_name,
    install_schema,
)

APP_ROLE_INVALID = "invalid application role name"


def main() -> None:
    settings = load_settings()
    if settings.admin_database_url is None:
        raise SystemExit("DIVESAFE_ADMIN_DATABASE_URL is not set")
    dsn = settings.admin_database_url.get_secret_value()
    if settings.loader_db_role is None:
        try:
            install_schema(dsn, settings.app_db_role)
        except ValueError:
            raise SystemExit(APP_ROLE_INVALID) from None
        except LoaderRoleError as exc:
            raise SystemExit(str(exc)) from None
        print("schema is up to date")
        return
    try:
        check_loader_role_name(settings.loader_db_role, settings.app_db_role)
    except ValueError:
        raise SystemExit(LOADER_ROLE_INVALID) from None
    try:
        install_schema(dsn, settings.app_db_role, loader_role=settings.loader_db_role)
    except ValueError:
        raise SystemExit(APP_ROLE_INVALID) from None
    except LoaderRoleError as exc:
        raise SystemExit(str(exc)) from None
    print("schema is up to date")


if __name__ == "__main__":
    main()
