from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path

from alembic import command
from alembic.config import Config

EXPLICIT_DATABASE_URL_ATTRIBUTE = "supportbot_explicit_database_url"


def synchronous_database_url(database_url: str) -> str:
    return database_url.replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"
    )


def build_alembic_config(database_url: str) -> Config:
    """Build a config whose explicit target cannot be replaced by ambient state."""

    root = Path.cwd()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes[EXPLICIT_DATABASE_URL_ATTRIBUTE] = database_url
    config.attributes["skip_logging_config"] = True
    return config


def resolve_migration_database_url(
    config: Config, environment: Mapping[str, str] | None = None
) -> str:
    """Resolve explicit, ambient and configured migration targets in safe order."""

    runtime_environment = os.environ if environment is None else environment
    explicit_database_url = config.attributes.get(EXPLICIT_DATABASE_URL_ATTRIBUTE)
    if explicit_database_url is not None:
        if not str(explicit_database_url).strip():
            raise ValueError("Explicit migration database URL must not be empty")
        database_url = explicit_database_url
    else:
        database_url = runtime_environment.get("DATABASE_URL") or config.get_main_option(
            "sqlalchemy.url"
        )
    if not database_url:
        data_dir = runtime_environment.get("DATA_DIR", "./data")
        database_url = "sqlite+aiosqlite:///" + os.path.join(data_dir, "support.db")
    return synchronous_database_url(str(database_url))


async def upgrade_database(database_url: str) -> None:
    """Run schema migrations before the bot starts consuming updates."""

    config = build_alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "head")


async def run() -> None:
    database_url = os.getenv("MIGRATION_DATABASE_URL")
    if not database_url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for the migration service")
    await upgrade_database(database_url)
    print("Database migrations completed")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
