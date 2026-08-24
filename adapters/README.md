# Adapters

Adapters connect external sources to Personal Task Assistant through the JSON
API. They should stay small, auditable, and user-controlled.

The full source-health and ingestion-decision protocol is documented in
`docs/ADAPTER_CONTRACT.md`. It lets an adapter prove that it really checked its
source, remember ignored items without creating noise tasks, and reconsider an
item only when its content fingerprint changes or `revisit_after` expires.

The repository includes two connectors:

- `telegram_polling_adapter.py` reads Telegram Bot API updates and sends
  normalized tasks to `POST /api/agent/ingest/context`. It also handles the
  inline ✅ Done / 🔁 Rework / ✋ Block buttons on review requests that
  `automation/notify.py` sends, and **replies to bot messages**: replying to
  a reminder/review message about `#N` applies that reply to the task — the
  first word picks the action (закрыто/готово/done → close,
  доработай/rework → in_progress, блок/block → blocked, anything else →
  comment) and the full text is appended to the description as a dated note,
  with a localized confirmation back in the chat. Button presses and replies
  from an allowed chat are the human's decision applied with
  `PATCH /api/tasks/{id}` (deliberately the one path through the adapter
  that may set a task to `done` — a human did it).

  **Voice messages** are transcribed locally (`speech_to_text.py`: ffmpeg +
  a whisper CLI, no cloud, no API keys) and then behave like text: a voice
  reply to a task message acts on that task (first transcribed word picks
  the action); a **question** («какой статус задачи 158?», «что на доске?»)
  gets an answer — text plus a voice bubble synthesized locally (Piper
  neural voice via `PIPER_BIN`/`PIPER_MODEL`, falling back to macOS `say`);
  anything else creates a task titled from the transcript. The bot always
  echoes what it heard («🎙 …»), so mishearings are visible and correctable.

  Privacy: the audio itself never persists — downloads and synthesis
  intermediates live in temp dirs wiped immediately; the only trace kept is
  the transcript, appended to a weekly text log
  (`automation/logs/voice-transcripts-<monday>_<sunday>.txt`,
  `VOICE_TRANSCRIPT_DIR` to relocate). Configure `FFMPEG_BIN`/`WHISPER_BIN`
  with absolute paths (supervisors have a minimal PATH),
  `WHISPER_MODEL=small` or better for non-English speech; when STT is not
  available the bot says so instead of staying silent. Command traffic
  never touches the ingestion ledger or source-health counters.
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

For always-on operation (required for the review buttons to respond), install
the supervised job — it loads the repo's `.env` and restarts the adapter after
transient failures:

```bash
automation/install.sh --telegram
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

## Telegram Business ingestion («Автоматизация чатов»)

`telegram_business.py` (opt-in: `TELEGRAM_BUSINESS_TASKS=1`) turns the owner's
own Telegram chats into a reviewed task-candidate stream. Telegram Premium
lets a user connect one bot to their personal account; the connected bot then
receives `business_*` updates for new messages in the user's private 1:1
chats — both directions — through the same getUpdates stream this polling
adapter already consumes. Unlike a full user-session (MTProto) integration,
the bot holds only the permissions granted on the connect screen: with every
toggle off it cannot write, edit, or delete anything in those chats, so the
whole path is structurally read-only. No business send/edit/delete method
exists in the module, and the ledger plus a local suggested-list make every
candidate a one-shot suggestion.

Setup:

1. In `@BotFather`: `/mybots` → your bot → Bot Settings → **Business Mode →
   Turn on** (`getMe` should then report `can_connect_to_business: true`).
2. In Telegram (Premium): Settings → Telegram Business → Chatbots (newer
   clients: «Автоматизация чатов») → enter the bot's `@username`.
3. On the connect screen: pick which chats the bot sees (exclude anything
   sensitive) and **disable all permissions** — collection needs none of
   them. The bot confirms the connection in the notify chat and warns if
   extra permissions are still granted.
4. Set `TELEGRAM_BUSINESS_TASKS=1` next to the usual adapter variables and
   restart the supervised adapter job.

How it works: each business message lands in a small per-chat rolling buffer
in local JSON state (`TELEGRAM_BUSINESS_STATE`, default
`.adapter_state/telegram_business_state.json`); voice notes are transcribed
with the same local STT as the command path (`BUSINESS_VOICE_MAX_SECONDS`,
default 300). Once a chat stays quiet for `BUSINESS_ANALYZE_LULL` seconds
(default 180; a busy chat is force-analyzed after
`BUSINESS_ANALYZE_MAX_WAIT`), the unanalyzed window plus a little context is
sent to a **local headless Claude Code session** (the work runner's
no-API-billing pattern; `WORK_CLAUDE_BIN`, optional `BUSINESS_ANALYZE_MODEL`)
that extracts at most `BUSINESS_MAX_CANDIDATES` candidates of exactly two
kinds: a direct ask addressed to the owner, or the owner's own "ок, сделаю"
commitment. Anything below `BUSINESS_MIN_CONFIDENCE` is dropped; when in
doubt the model is told to return nothing.

Each surviving candidate is receipted as `needs_review` in the ingestion
ledger and becomes a card with inline buttons in `TELEGRAM_NOTIFY_CHAT_ID`:
✅ creates the task through `POST /api/agent/ingest/context` (stable
`external_id`, `assignee=me`, the quoted source message in the description)
and records a `created`/`duplicate` decision as the human's; 🙈 records a
durable `ignored` decision so the item is never suggested again, even after
the local state is wiped. Chats listed in `BUSINESS_IGNORE_CHAT_IDS` are
never buffered. Group chats are not part of Telegram's business updates —
this covers private 1:1 chats only, and only messages sent after the
connection was made.
