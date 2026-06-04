# Adapters

Adapters connect external sources to Personal Task Assistant through the JSON
API. They should stay small, auditable, and user-controlled.

The repository includes one reference adapter:

- `telegram_polling_adapter.py` reads Telegram Bot API updates and sends
  normalized tasks to `POST /api/agent/ingest/context`.

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

The adapter fails closed when `TELEGRAM_ALLOWED_CHAT_IDS` is empty. To accept
updates from every chat intentionally, set `TELEGRAM_ALLOW_ALL_CHATS=true`; this
is not recommended for public bots.
