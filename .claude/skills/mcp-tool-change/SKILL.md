---
name: mcp-tool-change
description: How to add or change tools of the MCP server (adapters/task_assistant_mcp.py) — naming and docstring conventions, actionable errors, integration tests, and the mandatory evaluation process in evals/. Use this BEFORE touching any task_assistant_* tool, adding a new MCP capability, editing tool descriptions, or when an agent reports the MCP tools are confusing or failing.
---

# Changing the MCP tool surface

The MCP server is how agents operate the board with no code access and no
docs — so the quality bar is not "the code works" but "an agent given only
these tools succeeds". That is measured by the evaluation process, which is a
required step here, not an optional extra.

## Conventions (match the existing nine tools)

- Names: `task_assistant_<verb>_<noun>`, snake_case.
- One Pydantic input model per tool (`extra="forbid"`, constrained Fields with
  example-bearing descriptions). No manual validation in the body.
- Decorator annotations set honestly: `readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`.
- Docstring is the tool's contract: one-line summary, when to use it AND when
  to use a neighboring tool instead, Args, Returns with the exact shape.
- List-returning tools support `response_format` markdown (default) | json;
  mutations return compact JSON of the resulting task.
- Errors go through `_error_text` and must tell the agent what to do next
  (start the tracker, fix the key, use another tool) — never a stack trace.
- The server stays a pure HTTP client of the JSON API: no imports from `app/`,
  config only via `TASK_TRACKER_URL` / `TASK_TRACKER_API_KEY`.

## Required updates for any surface change

1. **Tests** — `tests/test_mcp_server.py`: add the tool to `EXPECTED_TOOLS`,
   and write an integration test that calls it against the real FastAPI app
   (the `wire_mcp_to_app` fixture routes the server's HTTP client through
   `ASGITransport` — no network, no mocks of your own).
2. **Verifier** — `evals/verify_answers.py`: bump `EXPECTED_TOOL_COUNT`.
3. **Docs** — the tool table in `docs/MCP.md`.
4. **Eval questions** — if the tool adds a read-only capability, consider a
   new question in `evals/evaluation.xml`. Rules: read-only, independent,
   stable answer (the seeded board is frozen June-2026 data — keep all
   deadlines in the past), single string-comparable answer, not solvable by
   keyword search. If you touch the board or the questions, update
   `compute_answers` in the verifier to derive the same answer.

## The evaluation gate (run before shipping)

```bash
.venv/bin/python evals/seed_eval_board.py
DATABASE_URL=sqlite:///evals/eval_board.db TASK_TRACKER_API_KEY=eval-key \
  SESSION_SECRET_KEY=eval-secret-0123456789012345678901234567890 \
  .venv/bin/uvicorn app.main:app --port 8877 &
.venv/bin/python evals/verify_answers.py --write-loop
.venv/bin/python evals/seed_eval_board.py   # re-seed after the write loop
```

The verifier must report all answers OK and the claim -> finish loop passing.
When tool descriptions or formats changed materially, also run the LLM
harness (`evals/evaluation.py`, needs `ANTHROPIC_API_KEY`; see
`evals/README.md`) and read the agent feedback in the report — low accuracy
means the descriptions need work, not that the model is weak.

Ship via the `ship-release` skill; describe new tools in the changelog by
name so agents can find them in history.
