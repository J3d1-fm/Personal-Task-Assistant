---
name: db-schema-change
description: How to change the task database schema in this repository — Alembic revisions, the legacy-database stamping heuristic, dual-store (SQLite + Firestore) updates, and the required migration tests. Use this BEFORE adding, renaming, or removing any column or table, changing an enum, or touching app/models.py, app/migrate.py, or migrations/ — even for a "trivial" new field.
---

# Changing the schema

Schema changes here must work for three kinds of databases at once: fresh
installs, databases created by pre-0.4.0 releases via `create_all` (no
`alembic_version` table), and Firestore documents that never migrate. A change
that only works for your local fresh DB will corrupt or crash real user
installs on their next `git pull`, which is why `create_all` is never the
answer.

## Steps

1. **New Alembic revision** in `migrations/versions/`, numbered sequentially
   (`0003_...py`), using `migrations/versions/0002_add_task_external_id.py`
   as the template.
   Hardcode enum value lists in the migration — do not import app models into
   migration files; migrations must stay frozen while models evolve.

2. **Update the model and schemas together**: `app/models.py` (column),
   `app/schemas.py` (TaskBase/TaskUpdate/TaskRead fields with validation
   constraints). New fields must be optional or have defaults — old rows and
   old Firestore documents will not have them.

3. **Update the stamping heuristic** in `app/migrate.py`. It detects databases
   without `alembic_version` and decides which revision they match by
   inspecting the `tasks` columns (currently: `external_id` present → head,
   absent → `0001`). When your revision adds a column, make that column the
   new marker and bump `HEAD_REVISION`; otherwise a fresh `create_all`-shaped
   database (the test suite creates these) gets stamped too low and the
   upgrade fails on an already-existing column.

4. **Both stores**: `SqliteTaskStore` usually needs nothing beyond the model;
   `FirestoreTaskStore` is schemaless but needs the field handled in
   `encode_task_data` (enums → values) and must tolerate documents missing the
   field (defaults in `TaskRead` cover this).

5. **Tests** — extend `tests/test_migrations.py`:
   - add the new column to the expected-columns assertions;
   - keep the legacy-upgrade test honest: its `LEGACY_TASKS_TABLE` SQL
     reproduces the 0.3.x schema and must NOT gain your new column — the test
     proves old databases upgrade cleanly with data intact;
   - run the full suite (`.venv/bin/pytest -q`) — conftest exercises the
     stamping path on every run.

## Gotchas learned the hard way

- SQLite ignores timezone info: `DateTime(timezone=True)` columns come back
  naive. Application code normalizes naive datetimes as UTC
  (`_normalized_datetime` in app/main.py); keep that assumption.
- SQLite ALTER is limited — plain ADD COLUMN is fine; anything structural
  (drop/rename/constraint change) needs batch mode, which `migrations/env.py`
  already enables via `render_as_batch` for SQLite.
- Unique + nullable works on SQLite (multiple NULLs allowed) — that is why
  `external_id` can be both optional and unique.

Ship the change via the `ship-release` skill; schema releases are minor
version bumps.
