"""Install the assessments schema and its guard as the OWNER role.

    DIVESAFE_ADMIN_DATABASE_URL=... DIVESAFE_APP_DB_ROLE=divesafe_app \\
        python -m divesafe.services.db_init

The running application connects as a different, least-privilege role (SELECT, INSERT, UPDATE
only) and refuses to start if the guard is missing or the role is too powerful.
"""

from __future__ import annotations

from divesafe.config import Settings
from divesafe.services.postgres import install_schema


def main() -> None:
    settings = Settings()
    if settings.admin_database_url is None:
        raise SystemExit("DIVESAFE_ADMIN_DATABASE_URL is not set")
    install_schema(settings.admin_database_url.get_secret_value(), settings.app_db_role)
    print("assessments schema installed")


if __name__ == "__main__":
    main()
