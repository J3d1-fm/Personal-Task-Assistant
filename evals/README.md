# Evaluation Process

This directory holds the repeatable evaluation process for the MCP server: it
measures whether an agent, given ONLY the MCP tools (no code access, no docs),
can answer realistic questions about the task board. Run it after any change
to the MCP tool surface, tool descriptions, or the API they wrap.

## Parts

- `seed_eval_board.py` — recreates the deterministic eval board: 16 tasks with
  fixed June-2026 timestamps, all deadlines in the past, so every answer is
  stable no matter when the evaluation runs.
- `evaluation.xml` — 10 read-only questions with single verifiable answers
  (per the MCP evaluation guidelines: independent, non-destructive, stable,
  not solvable by simple keyword search).
- `verify_answers.py` — ground-truth check without LLM costs: connects to the
  real MCP server over stdio, solves every question programmatically through
  read-only tools, and compares against `evaluation.xml`. Run this first —
  if it fails, the board, the questions, or the tools are broken, and an LLM
  run would measure noise.
- `evaluation.py` + `connections.py` — LLM evaluation harness (vendored from
  the Anthropic mcp-builder skill, Apache 2.0): gives Claude the questions and
  ONLY the MCP server, grades answers by string comparison, and reports
  accuracy, tool-call counts, and the agent's feedback on the tools.

## Running

1. Seed the board and serve it:

```bash
.venv/bin/python evals/seed_eval_board.py
DATABASE_URL=sqlite:///evals/eval_board.db TASK_TRACKER_API_KEY=eval-key \
  SESSION_SECRET_KEY=eval-secret-0123456789012345678901234567890 \
  .venv/bin/uvicorn app.main:app --port 8877
```

2. Ground-truth check (free, deterministic):

```bash
.venv/bin/python evals/verify_answers.py
# optionally exercise the write loop too (mutates the board; re-seed after):
.venv/bin/python evals/verify_answers.py --write-loop
.venv/bin/python evals/seed_eval_board.py
```

3. LLM evaluation (needs an Anthropic API key; costs tokens):

```bash
.venv/bin/pip install -r evals/requirements.txt
export ANTHROPIC_API_KEY=...
.venv/bin/python evals/evaluation.py \
  -t stdio \
  -c .venv/bin/python \
  -a adapters/task_assistant_mcp.py \
  -e TASK_TRACKER_URL=http://127.0.0.1:8877 \
  -e TASK_TRACKER_API_KEY=eval-key \
  -o evals/report.md \
  evals/evaluation.xml
```

4. Read `evals/report.md`: per-question pass/fail, tool-call traces, and the
   agent's feedback on the tools. Low accuracy usually means tool descriptions
   or output formats need work, not that the model is weak.

## Rules for changing the eval

- Keep questions read-only, independent, and stable; keep answers single
  verifiable strings (see the vendored harness docs).
- If you touch the seeded board or the questions, run `verify_answers.py`
  before committing — it is the contract that answers match the board.
- The eval board lives in `evals/eval_board.db` (gitignored); never point the
  eval at a real working database.
