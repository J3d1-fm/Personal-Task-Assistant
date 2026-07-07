# Daily task ritual

Runs once a day (15:00 local by default) to check the board and move work
forward without you starting it. Two layers:

- **Digest (always):** `daily_ritual.py` reads the board and writes
  `logs/digest-<date>.md` — overdue, waiting-your-review, blocked,
  unassigned-to-triage, due-soon, and the agent queue. It never changes the
  board, so you always get a report even if nothing else runs.
- **Agent work loop (opt-in):** if enabled, an agent works the codex queue
  (claim → do what it safely can → finish to `waiting_review`) through the MCP
  server, and the digest reports what it did.

## Prerequisites

- The venv is set up (`install.command` or `python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt`).
- A `.env` with at least `TASK_TRACKER_API_KEY` (Local Mode's key). The ritual
  reads the board with it.

## Install the schedule

```bash
automation/install.sh          # render + load the 15:00 launchd job
automation/install.sh --remove # unload + delete it
```

`install.sh` is run by you on purpose — it schedules an agent that acts on your
behalf. Confirm it works end to end once before trusting it unattended:

```bash
automation/daily_run.sh --no-work        # digest only, watch the output
launchctl kickstart -k gui/$(id -u)/com.taskassistant.daily   # fire the real job now
```

The digest is written to `automation/logs/digest-<date>.md`; run logs are next
to it. `daily_run.sh` reuses your running tracker if it finds one, otherwise it
starts a temporary instance for the run and stops it afterward.

## Enable the agent work loop

The digest alone is safe and needs no API key. To let the agent actually take
tasks into review, add to `.env`:

```bash
DAILY_RITUAL_WORK=1
ANTHROPIC_API_KEY=sk-ant-...
# optional:
# DAILY_RITUAL_MODEL=claude-sonnet-5   # cheaper than the opus default
```

Then run it once manually and read the digest before scheduling relies on it:

```bash
set -a; . .env; set +a
.venv/bin/python automation/daily_ritual.py --max-tasks 3
```

### What the agent may and may not do

- It only has the `task_assistant_*` MCP tools — no shell, repo, or web.
- It advances tasks to `waiting_review` only. It cannot mark anything `done` —
  that stays your decision (enforced in `work_loop._dispatch`, not just asked
  for in the prompt).
- `--max-tasks` caps how many tasks it may claim per run.
- For work it cannot truly finish from inside the tools (real code, outside
  actions), it leaves notes on the task and either sends it to review with its
  findings or marks it `blocked` with the reason.

## Files

| File | Role |
| --- | --- |
| `daily_ritual.py` | Deterministic driver: health check, situation, digest |
| `work_loop.py` | Opt-in agent loop over MCP with the safety rails |
| `daily_run.sh` | Scheduler entrypoint: tracker lifecycle + logging |
| `com.taskassistant.daily.plist` | launchd job (15:00 local) |
| `install.sh` | Render/load/remove the job |
| `logs/` | Dated digests and run logs (gitignored) |

## Change the time

Edit `StartCalendarInterval` in `com.taskassistant.daily.plist` and re-run
`install.sh`. For twice a day, make it an array of `<dict>` entries (e.g. 09:00
and 18:00).

Tests: `tests/test_daily_ritual.py` (digest logic) and
`tests/test_work_loop.py` (safety rails, no API key needed).
