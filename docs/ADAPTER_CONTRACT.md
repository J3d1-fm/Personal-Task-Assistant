# Source Adapter Contract

Personal Task Assistant keeps credentials and source-specific API calls outside
the core app. An adapter owns authentication, cursors, retries, and the minimum
read request needed to prove that its source is really accessible. The core app
stores only normalized health reports, ingestion decisions, and tasks.

This contract solves two different problems:

- **Source health:** distinguish a healthy empty scan from expired auth, partial
  coverage, rate limiting, bad configuration, and a connector crash.
- **Ingestion memory:** remember why a source item was created, ignored, or held
  for review without turning every candidate into a task.

## Reference flow

For every polling batch or webhook event:

1. Perform a real least-privilege read against the source. Checking an OAuth
   metadata endpoint alone is not enough because a valid token may have lost a
   required scope or resource permission.
2. Build a stable `source_item_id` and a content fingerprint. A SHA-256 hex
   digest of normalized actionable content is a good default.
3. Call `POST /api/agent/ingestion/check` before expensive parsing or task
   creation. The same item and fingerprint can be suppressed; changed content
   is processed again.
4. If actionable, create tasks through `POST /api/agent/ingest/context` with
   stable task-level `external_id` values.
5. Record the item outcome through `POST /api/agent/ingestion/decisions`.
6. Report the source check through `POST /api/agent/sources/report`, including
   zero-task healthy runs. Long-running adapters should report periodically;
   the Telegram reference adapter defaults to every five minutes.

The web UI reads `GET /api/sources` every minute. A nominally healthy source
whose latest report is older than 24 hours is shown as a stale heartbeat.
Repeated identical responses are not re-rendered, so screen readers are not
notified again until source state actually changes.

## Source health

`POST /api/agent/sources/report` accepts:

```json
{
  "source_id": "gmail:personal",
  "source_name": "Personal Gmail",
  "adapter_type": "gmail_polling",
  "status": "healthy",
  "items_checked": 42,
  "candidates": 3,
  "created": 1,
  "duplicates": 1,
  "ignored": 1,
  "suppressed": 0
}
```

Supported statuses:

| Status | Meaning |
| --- | --- |
| `healthy` | The real source read succeeded with full expected coverage. |
| `degraded` | Some useful coverage succeeded, but a scope, folder, or channel is missing. |
| `unavailable` | The source could not be reached; retry may succeed without reconfiguration. |
| `reauth_required` | Credentials are expired, revoked, or otherwise require user action. |
| `misconfigured` | Required account, scope, chat id, or adapter setting is missing. |
| `rate_limited` | The source rejected the check because of quota or throttling. |
| `failed` | The adapter reached an unexpected non-auth failure. |

Failure reports may add `error_code`, a sanitized `error_message`, and an
absolute HTTP(S) `action_url`. Never put tokens, authorization codes, signed
URLs, raw private messages, or credential-bearing reauth URLs in these fields.
Automatic token refresh and safe retries belong inside the adapter. An
interactive reauth flow remains an explicit user action exposed through
`action_url`.

Adapters may provide `checked_at`. Reports more than five minutes in the future
are rejected, and an older/equal report can never overwrite a newer state.
SQLite uses a conditional update and Firestore uses a transaction, so
out-of-order or concurrent heartbeat delivery cannot turn a newer auth failure
into a false green state. All API timestamps are normalized to UTC.

`GET /api/sources` and `GET /api/agent/sources` return the latest report for
each `source_id`, together with `last_checked_at`, `last_success_at`, and
`last_error_at`.

## Ingestion decisions

Check one or more items before processing:

```json
{
  "source_id": "slack:work",
  "items": [
    {
      "source_item_id": "thread:C123:1710000000.000100",
      "content_fingerprint": "8a86..."
    }
  ]
}
```

`POST /api/agent/ingestion/check` returns `should_process` plus the existing
decision when the exact item/fingerprint pair was seen before.

Record an outcome:

```json
{
  "source_id": "slack:work",
  "source_item_id": "thread:C123:1710000000.000100",
  "content_fingerprint": "8a86...",
  "decision": "ignored",
  "reason": "FYI only; no action requested",
  "decided_by": "adapter"
}
```

Decision values are `created`, `duplicate`, `ignored`, `needs_review`, and
`failed`. The first four suppress the same unchanged item. `failed` remains
retryable. Any decision may set `revisit_after`; once that time passes, the
same fingerprint becomes processable again. A changed fingerprint is always a
new evaluation opportunity.

`decided_by` is `adapter`, `agent`, `human`, or `system`. `task_id` may link a
decision to the first normalized task, but it is deliberately not a database
foreign key: deleting a rejected task must not delete the receipt that prevents
the source item from returning.

## Telegram reference implementation

`adapters/telegram_polling_adapter.py` implements the complete flow:

- Telegram `getUpdates` is the real read/heartbeat check.
- `telegram:<chat_id>:<message_id>` identifies the source item.
- A normalized text SHA-256 digest detects materially changed content.
- New and edited Telegram messages use the same stable item id, so edited
  content gets a new fingerprint and is evaluated again.
- Task `external_id` values combine an adapter-source namespace with a
  content-stable normalized-task hash. Reordering or inserting lines preserves
  the identity of unchanged tasks and separate bots cannot collide.
- Non-actionable messages receive an `ignored` receipt.
- Known unchanged items are suppressed before parsing and ingest.
- Created or duplicate task outcomes are recorded after idempotent ingest.
- Successful checks and sanitized failures update source health.

Telegram edit synchronization is intentionally append-only and safe: unchanged
lines dedupe, new or changed lines create tasks, and removed lines do not
automatically delete, close, rename, or reassign existing work. This avoids
corrupting a task that a human or agent has already started. Long-running
polling retries transient failures with bounded exponential backoff and honors
Telegram's full server-provided `retry_after`; credential failures stop until
the user fixes the token.

Run `--dry-run` first. Dry runs print task payloads but do not write tasks,
decisions, source health, or the Telegram offset.

## Out of scope: command traffic

Two message kinds are command traffic, not source content, and short-circuit
before the ingest/ledger path:

- **Inline-button callbacks** on review requests (sent by
  `automation/notify.py`): a press from an allowed chat applies the human's
  decision via `PATCH /api/tasks/{id}`.
- **Replies to the bot's own messages** that mention `#N` (reminders, review
  requests): the reply addresses that task directly. The first word picks the
  action — done-words close it, rework-words return it to in_progress,
  block-words block it, anything else is a comment — and the full reply text
  is appended to the task description as a dated note. The adapter answers
  with a localized confirmation.

Command traffic never creates ingestion decisions, never counts toward
source-health scan metrics, and needs no fingerprinting — the contract above
applies only to reading items *from* a source.
