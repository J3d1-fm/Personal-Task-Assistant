---
name: daily-task-ritual
description: The automation layer — the once-a-day ritual (launchd at 15:00 -> daily_ritual.py -> optional agent work loop), Telegram delivery of the digest (text + optional macOS voice note via notify.py), and the continuous watch loop (watch.py — reminders, review requests with inline buttons, agent trigger). How each is wired, run, extended, debugged, and the safety model. Use this whenever asked about the daily/scheduled run, the digest, notifications/напоминания, review buttons, "почему не сработала автоматизация", changing the schedule or what the agent may do unattended, or before editing anything under automation/.
---

# The daily task ritual

Once a day the assistant should check the board and move work forward without
the human kicking it off. That runs in two layers with a deliberate safety
split, both in `automation/`:

1. **Deterministic layer (`automation/daily_ritual.py`)** — always runs, never
   mutates the board. Health-checks the tracker, reads it over REST, classifies
   the board (`build_situation`), and writes a dated digest to
   `automation/logs/` ordered by what needs the human first: overdue,
   waiting-review, blocked, unassigned-to-triage, due-soon, then the agent
   side. This is the guaranteed daily deliverable — the human always gets "here
   is your board today" even if the agent layer is off or fails.
2. **Agent work loop (`automation/work_loop.py`, opt-in)** — only if
   `DAILY_RITUAL_WORK` is set and an `ANTHROPIC_API_KEY` is present. Gives a
   model ONLY the `task_assistant_*` MCP tools and the worker instructions,
   then it claims -> acts -> finishes tasks to `waiting_review`. What it did is
   folded back into the digest.

Since 0.9.0 two more pieces live in `automation/`:

3. **Outbound Telegram (`notify.py`)** — with `TELEGRAM_BOT_TOKEN` +
   `TELEGRAM_NOTIFY_CHAT_ID` the ritual delivers the digest as a message
   (plus a spoken voice note with `TELEGRAM_DIGEST_VOICE=1`, macOS
   `say`/`afconvert` only). Send failures log and never break callers;
   `--no-notify` skips delivery.
4. **Watch loop (`watch.py`, opt-in, `install.sh --watch`)** — polls every
   `WATCH_INTERVAL` (30s): pushes due reminders (stamps
   `reminder_last_sent_at` only after delivery), announces `waiting_review`
   entries with ✅/🔁/✋ inline buttons (presses are handled by the polling
   adapter — the bot's single `getUpdates` consumer; never add a second one),
   and with `WATCH_WORK=1` kicks the work loop when agent-ready backlog
   appears. State in `automation/.watch_state.json`.

## Wiring

Native: `automation/install.sh` installs the scheduler for the current OS —
launchd on macOS (`automation/com.taskassistant.daily.plist`, 15:00 local),
a systemd user timer or crontab entry on Linux. All of them run
`automation/daily_run.sh` -> `automation/daily_ritual.py`; the runner reuses a
running tracker or starts a temporary one for the duration and stops it after.
The user runs install.sh themselves — it schedules an agent that acts on their
behalf, so it is never auto-loaded.

Docker: the `ritual` service in `docker-compose.yml` runs
`automation/scheduler.py`, which sleeps until `RITUAL_TIME` (env, local to
`TZ`) daily and executes the same daily_ritual.py against the `tracker`
service. Self-hosting guide: `docs/SELF_HOSTING.md`.

## Safety model (enforced in code, not the prompt)

- The deterministic layer never writes to the board.
- The agent gets no shell, repo, or web access — only the MCP tools.
- `work_loop._dispatch` rejects `update_task(status="done")`: only the human
  closes tasks. The agent's terminal state is `waiting_review`.
- `--max-tasks` caps claims per run; over budget, the claim tool refuses, so an
  unattended run cannot run away.
- These rails have unit tests (`tests/test_work_loop.py`) that use no API key —
  keep them green when touching the loop.

## Common changes

- **Schedule/time**: the `StartCalendarInterval` in the plist; re-run
  `install.sh`. For twice-daily, add a second `<dict>` under a
  `StartCalendarInterval` array.
- **What the agent may do**: edit `SYSTEM_PROMPT` in `automation/work_loop.py`,
  but keep the never-close-tasks and claim-budget rails in `_dispatch`.
- **Digest content/order**: `build_situation` / `render_digest`; both are pure
  and covered by `tests/test_daily_ritual.py`.
- **Model/cost**: `DAILY_RITUAL_MODEL` (default `claude-sonnet-5`; board
  meta-work does not need a bigger tier).

## Debugging a missed or wrong run

- Check `automation/logs/run-<date>.log` and `launchd.err.log`.
- Confirm the job: `launchctl print gui/$(id -u)/com.taskassistant.daily`.
- Reproduce by hand: `automation/daily_run.sh --no-work` (digest only) or
  `.venv/bin/python automation/daily_ritual.py --no-work` against a running
  tracker.
- No digest usually means the tracker was unreachable (API key or URL) — the
  script logs which.

Full runbook: `automation/README.md`. Ship changes via `ship-release`.
