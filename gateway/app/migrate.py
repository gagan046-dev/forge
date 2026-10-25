"""Apply database migrations: `python -m app.migrate` (used by the Helm migration Job).

Only FORGE_DATABASE_URL is needed, so the Job doesn't have to carry the gateway's other secrets.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

log = logging.getLogger("app.migrate")

GATEWAY_DIR = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(GATEWAY_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_DIR / "alembic"))
    if database_url:
        # ConfigParser interpolation treats % specially; escape it for URL-encoded passwords.
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    database_url = os.environ.get("FORGE_DATABASE_URL")
    if not database_url:
        log.error("FORGE_DATABASE_URL is not set")
        return 2
    command.upgrade(alembic_config(database_url), "head")
    log.info("database schema is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
