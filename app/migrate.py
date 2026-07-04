"""Schema migrations for the SQL-backed task store.

Databases created by pre-0.4.0 releases used ``Base.metadata.create_all`` and
have no ``alembic_version`` table. On startup we detect that case, stamp the
matching revision based on the columns that actually exist, and then upgrade
to head. Firestore deployments never run migrations.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]

INITIAL_REVISION = "0001"
HEAD_REVISION = "0002"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # ConfigParser treats % as interpolation syntax.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def run_migrations(database_url: str | None = None) -> None:
    url = database_url or get_settings().database_url
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        has_tasks_table = inspector.has_table("tasks")
        has_version_table = inspector.has_table("alembic_version")
        if has_tasks_table and not has_version_table:
            columns = {column["name"] for column in inspector.get_columns("tasks")}
            revision = HEAD_REVISION if "external_id" in columns else INITIAL_REVISION
            command.stamp(config, revision)
    finally:
        engine.dispose()
    command.upgrade(config, "head")
