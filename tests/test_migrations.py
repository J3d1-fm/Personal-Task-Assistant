from sqlalchemy import create_engine, inspect, text

from app.db import Base
from app.migrate import run_migrations

LEGACY_TASKS_TABLE = """
CREATE TABLE tasks (
    id INTEGER NOT NULL,
    title VARCHAR(240) NOT NULL,
    description TEXT,
    status VARCHAR(14) NOT NULL,
    assignee VARCHAR(10) NOT NULL,
    origin VARCHAR(8) NOT NULL,
    priority INTEGER NOT NULL,
    source_name VARCHAR(160),
    source_url TEXT,
    source_context TEXT,
    due_at DATETIME,
    reminder_at DATETIME,
    reminder_last_sent_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
)
"""


def _column_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("tasks")}
    finally:
        engine.dispose()


def _stamped_revision(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _index_names(url: str, table_name: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {index["name"] for index in inspect(engine).get_indexes(table_name)}
    finally:
        engine.dispose()


def test_fresh_database_is_created_at_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    run_migrations(url)
    assert "external_id" in _column_names(url)
    assert {"tasks", "source_states", "ingestion_decisions"} <= _table_names(url)
    assert _stamped_revision(url) == "0003"


def test_legacy_database_is_stamped_and_upgraded(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(LEGACY_TASKS_TABLE))
        connection.execute(
            text(
                "INSERT INTO tasks (id, title, status, assignee, origin, priority, created_at, updated_at) "
                "VALUES (1, 'old row', 'backlog', 'me', 'manual', 3, '2026-01-01', '2026-01-01')"
            )
        )
    engine.dispose()

    run_migrations(url)

    assert "external_id" in _column_names(url)
    assert {"source_states", "ingestion_decisions"} <= _table_names(url)
    assert _stamped_revision(url) == "0003"

    engine = create_engine(url)
    with engine.connect() as connection:
        title = connection.execute(text("SELECT title FROM tasks WHERE id = 1")).scalar_one()
    engine.dispose()
    assert title == "old row"


def test_create_all_database_is_stamped_at_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'createall.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    run_migrations(url)

    assert _stamped_revision(url) == "0003"
    assert "ix_ingestion_decisions_source_item" in _index_names(url, "ingestion_decisions")


def test_run_migrations_is_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path / 'twice.db'}"
    run_migrations(url)
    run_migrations(url)
    assert _stamped_revision(url) == "0003"
