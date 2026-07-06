# AGENTS.md — Operating Contract

This file is the entry point for any coding agent (Codex, Claude Code, or
other) working in this repository. Read it before changing anything.

## What this project is

Personal Task Assistant is not a classic task tracker: it is the coordination
layer between a human and autonomous AI agents. Humans decide, agents execute,
and both share one operational queue. The JSON API is the agent contract, the
MCP server (`adapters/task_assistant_mcp.py`) is the native way for agents to
operate the board, and the web UI is the human's operating surface.

## Skills — read the matching one BEFORE working

Playbooks distilled from real development sessions live in `.claude/skills/`.
Claude Code discovers them automatically; every other agent must open the
matching `SKILL.md` manually. Match by task:

| You are about to... | Read first |
| --- | --- |
| Work tasks from the board as the agent (claim/finish/ingest) | `.claude/skills/task-board-worker/SKILL.md` |
| Commit/ship ANY change (version, changelog, CI) | `.claude/skills/ship-release/SKILL.md` |
| Touch the DB schema, models, or migrations | `.claude/skills/db-schema-change/SKILL.md` |
| Touch the MCP tools or their descriptions | `.claude/skills/mcp-tool-change/SKILL.md` |

## Ground rules

- Python 3.12+, FastAPI + SQLAlchemy; SQLite in Local Mode, Firestore on
  Cloud Run. Setup: `python3 -m venv .venv && .venv/bin/pip install -r
  requirements.txt -r requirements-dev.txt`.
- Lint and tests must be green before any commit: `.venv/bin/ruff check .`
  and `.venv/bin/pytest -q` (CI runs exactly these).
- Schema changes go through Alembic revisions only — never
  `Base.metadata.create_all` (see the db-schema-change skill for why).
- Every shipped change bumps `app_version` in `app/config.py` and gets a
  detailed `CHANGELOG.txt` entry in the established style.
- Never commit credentials, tokens, `.env` values, or databases. The eval
  board (`evals/eval_board.db`) is disposable; never point evals at a real
  working database.
- Local dev auth is loopback-only by design; do not weaken it.

## Map

- `app/` — FastAPI app: `main.py` (routes), `store.py` (SQLite + Firestore
  stores), `models.py`/`schemas.py`, `migrate.py` (Alembic runner with
  legacy-DB stamping), `auth.py`, `history.py` (Sheets export).
- `migrations/` — Alembic revisions (numbered `000N_*.py`).
- `adapters/` — connectors: Telegram polling adapter, MCP server.
- `tests/` — pytest suite; MCP tools are tested against the in-process app.
- `evals/` — agent evaluation process for the MCP server (seeded board,
  questions, deterministic verifier, LLM harness). See `evals/README.md`.
- `docs/` — API, MCP, install, PM guide; `CHANGELOG.txt` is the release log.
