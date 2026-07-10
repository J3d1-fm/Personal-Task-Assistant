# Adapters

Adapters connect external sources to Personal Task Assistant through the JSON
API. They should stay small, auditable, and user-controlled.

The full source-health and ingestion-decision protocol is documented in
`docs/ADAPTER_CONTRACT.md`. It lets an adapter prove that it really checked its
source, remember ignored items without creating noise tasks, and reconsider an
item only when its content fingerprint changes or `revisit_after` expires.

The repository includes two connectors:

- `telegram_polling_adapter.py` reads Telegram Bot API updates and sends
  normalized tasks to `POST /api/agent/ingest/context`.
- `task_assistant_mcp.py` exposes the JSON API as an MCP server so Claude Code,
  Claude Desktop, and other MCP clients can read the queue, claim work, and
  ingest context directly. See `docs/MCP.md`.

## Safety Rules

- Keep source credentials in env vars or a secret manager.
- Do not commit bot tokens, OAuth secrets, exports, databases, or service-account files.
- Start with `--dry-run` before writing tasks into a live assistant.
- Restrict chat ids with `TELEGRAM_ALLOWED_CHAT_IDS` when using Telegram.
- Let the adapter create tasks; let the human approve sensitive outbound actions.

## Telegram Polling Adapter

Copy the env example:

```bash
cp adapters/telegram.env.example .env.telegram
```

Fill in your own values:

```bash
TELEGRAM_BOT_TOKEN=...
TASK_TRACKER_URL=http://127.0.0.1:8000
TASK_TRACKER_API_KEY=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
TELEGRAM_ALLOW_ALL_CHATS=false
TELEGRAM_SOURCE_ID=telegram:primary
TELEGRAM_SOURCE_NAME=Telegram Bot
TELEGRAM_HEALTH_REPORT_INTERVAL=300
TELEGRAM_MAX_RETRY_INTERVAL=300
```

Run a dry test:

```bash
set -a
. ./.env.telegram
set +a
python3 adapters/telegram_polling_adapter.py --once --dry-run
```

Run one real polling pass:

```bash
python3 adapters/telegram_polling_adapter.py --once
```

Run continuously:

```bash
python3 adapters/telegram_polling_adapter.py
```

The adapter extracts task lines with these prefixes:

- `codex:` or `agent:` creates Codex-owned work.
- `me:` or `human:` creates human-owned work.
- `review:` creates a human review task.
- `blocked:` creates a blocked task.
- `todo:` creates an unassigned task.

Optional priority prefix:

```text
P1 codex: fix production auth regression
P2 me: approve the agent plan
P3 todo: update launch checklist
```

Optional DD suffix:

```text
codex: prepare release notes dd:2026-06-07
```

The adapter stores its Telegram update offset in
`.adapter_state/telegram_polling_state.json` by default. That directory is
ignored by git.

Every successful `getUpdates` call is also a real source heartbeat. The adapter
reports a normalized health snapshot at most once per
`TELEGRAM_HEALTH_REPORT_INTERVAL` seconds (five minutes by default), records a
sanitized failure immediately, and classifies expired/invalid bot access as
`reauth_required`. A `--once` run always reports once unless it is a dry run.
Long-running adapters retry transient network/API failures with exponential
backoff capped by `TELEGRAM_MAX_RETRY_INTERVAL`. Telegram's explicit
`retry_after` value is always honored even when it is longer than that local
backoff cap. Auth failures remain terminal until the user fixes the token.

If `TELEGRAM_SOURCE_ID` is omitted, the adapter derives a stable id from the
public bot id portion of the token, so secret rotation does not create a new
source card or decision namespace. Set it explicitly when moving an existing
installation that previously used another id.

For every allowed message, the adapter checks
`telegram:<chat_id>:<message_id>` plus a SHA-256 fingerprint before parsing.
Known unchanged items are suppressed. Messages without a supported task prefix
are durably recorded as `ignored`; edited content gets a new fingerprint and is
eligible for evaluation again. Dry runs do not write these receipts.

Task-level `external_id` values include a hash of the adapter source id and a
content-stable hash of each normalized task line. Reordering or inserting lines
does not reassign the identity of existing tasks, and two bots reading the same
group cannot collide. The edit policy is deliberately conservative: unchanged
lines dedupe, new or changed lines create new tasks, and removed lines never
auto-delete or rewrite work that may already be in progress.

The adapter fails closed when `TELEGRAM_ALLOWED_CHAT_IDS` is empty. To accept
updates from every chat intentionally, set `TELEGRAM_ALLOW_ALL_CHATS=true`; this
is not recommended for public bots.
