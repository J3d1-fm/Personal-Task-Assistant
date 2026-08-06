# Daily task ritual

Runs once a day (15:00 local by default) to check the board and move work
forward without you starting it. Deploy shapes: launchd on macOS, systemd/cron
on Linux (both via `install.sh`), or the self-scheduling `ritual` service in
`docker-compose.yml` (`scheduler.py`, time set by `RITUAL_TIME`) — see
`docs/SELF_HOSTING.md` for the standalone deployment. Two layers:

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
# DAILY_RITUAL_MODEL=claude-sonnet-5   # the default; set to pick another tier
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

## Telegram delivery: the digest comes to you

Everything above also works with no Telegram at all — the digest then only
lands in `logs/`. To have the assistant deliver it, add to `.env`:

```bash
TELEGRAM_BOT_TOKEN=...            # the same bot the polling adapter uses
TELEGRAM_NOTIFY_CHAT_ID=123456789 # your chat with the bot
# optional spoken digest (macOS only — uses `say` + `afconvert`):
# TELEGRAM_DIGEST_VOICE=1
# TELEGRAM_DIGEST_VOICE_NAME=Samantha
```

The ritual then sends the digest text (and, with voice on, a short spoken
summary as an audio note) after writing the file. Delivery failures are logged
and never break the ritual; `--no-notify` skips delivery for a manual run.
Message text is chunked under Telegram's 4096-char limit; the bot token is
redacted from all logged errors.

## Watch loop: reflexes between rituals

`watch.py` polls the board (default every 30s) and reacts to what a
once-a-day pulse is too slow for:

- **Due reminders** → pushed to Telegram, then `reminder_last_sent_at` is
  stamped so the tracker's debounce holds. No Telegram configured → nothing
  is stamped, nothing is silently swallowed.
- **Tasks entering `waiting_review`** → announced with inline ✅ Done /
  🔁 Rework / ✋ Block buttons. Presses come back through the polling adapter
  (the bot's single `getUpdates` consumer) and update the task over the API.
  The ✅ button is the human deciding — agents themselves still cannot close
  tasks.
- **New agent-ready backlog** (opt-in `WATCH_WORK=1` + `ANTHROPIC_API_KEY`) →
  kicks the same work loop as the ritual, so a task assigned to the agent is
  picked up within one interval instead of at 15:00. All work-loop rails
  apply unchanged; `WATCH_WORK_MAX_TASKS` caps each kick.

```bash
automation/install.sh --watch            # launchd KeepAlive (macOS) / systemd
automation/install.sh --watch --remove
# or by hand, against a running tracker:
.venv/bin/python automation/watch.py --once
```

The loop presumes a running tracker (`run-local.command` or Docker) and
tolerates outages of both the tracker and Telegram: it logs and retries the
next tick. Announced-review state lives in `automation/.watch_state.json`.
Env knobs: `WATCH_INTERVAL` (seconds, min 5), `WATCH_STATE` (state path).

## Files

| File | Role |
| --- | --- |
| `daily_ritual.py` | Deterministic driver: health check, situation, digest, spoken script |
| `work_loop.py` | Opt-in agent loop over MCP with the safety rails |
| `notify.py` | Outbound Telegram: chunked messages, voice notes, review buttons |
| `watch.py` | Reflex loop: reminders, review requests, agent trigger |
| `daily_run.sh` | Scheduler entrypoint: tracker lifecycle + logging |
| `watch_run.sh` | KeepAlive entrypoint for the watch loop (env + venv) |
| `scheduler.py` | In-container daily scheduler (docker-compose `ritual` service) |
| `com.taskassistant.daily.plist` | launchd job (15:00 local, macOS) |
| `com.taskassistant.watch.plist` | launchd KeepAlive job for the watch loop (macOS) |
| `install.sh` | Install/remove the schedules: launchd, systemd user units, or cron |
| `logs/` | Dated digests, voice notes, and run logs (gitignored) |

## Change the time

Edit `StartCalendarInterval` in `com.taskassistant.daily.plist` and re-run
`install.sh`. For twice a day, make it an array of `<dict>` entries (e.g. 09:00
and 18:00).

Tests: `tests/test_daily_ritual.py` (digest + spoken script + delivery),
`tests/test_work_loop.py` (safety rails), `tests/test_notify.py` (chunking,
buttons, redaction), `tests/test_watch.py` (reminder stamping, review
announcements) — all offline, no API keys needed.
