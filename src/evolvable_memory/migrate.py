from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from evolvable_memory.config import Settings
from evolvable_memory.domain.common import DomainError


def alembic_config(database_url: str) -> Config:
    config = Config()
    script_location = Path(__file__).with_name("migrations")
    sqlalchemy_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def run() -> None:
    settings = Settings.from_environment()
    if settings.database_url is None:
        raise DomainError("EMF_DATABASE_URL is required to run database migrations")
    upgrade_database(settings.database_url)


if __name__ == "__main__":
    run()
