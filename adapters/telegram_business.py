#!/usr/bin/env python3
"""Telegram Business ingestion: harvest task candidates from the owner's own chats.

Telegram Premium lets a user connect ONE bot to their personal account
(Settings -> Telegram Business -> Chatbots, surfaced in newer clients as
«Автоматизация чатов»). The connected bot then receives `business_*` updates
for new messages in the user's private 1:1 chats — both directions — through
the same getUpdates stream the polling adapter already consumes. Unlike an
MTProto user session, the bot holds only the permissions granted on the
connect screen; with every toggle off it cannot write, edit, or delete
anything in those chats, which makes this path structurally read-only.

This module turns that stream into REVIEWED task candidates, deliberately not
into tasks:

- every business message lands in a small per-chat rolling buffer (local JSON
  state next to the polling offset; voice notes are transcribed with the same
  local no-cloud STT the command path uses);
- once a chat goes quiet, the buffered window is analyzed by a local headless
  Claude Code session (the same no-API-billing pattern as the work runner)
  that extracts only two kinds of candidates: direct asks addressed to the
  owner, and the owner's own «ок, сделаю» commitments;
- each candidate becomes an inline-button card in the owner's notify chat:
  ✅ creates the task through the normal ingest path (external ids, ledger),
  🙈 records a durable `ignored` decision so it is never suggested again.
  Cards are also receipted as `needs_review` in the ingestion ledger, so a
  wiped local state cannot re-suggest what the owner has already seen.

The bot never writes into the business chats themselves: no business send,
edit, or delete method exists anywhere in this module. Everything outbound
goes to the owner's own notify chat.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BUSINESS_UPDATE_TYPES = (
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)
CANDIDATE_CALLBACK_RE = re.compile(r"^cand:(take|skip):(-?\d+):(\d+)$")
TOKEN_URL_RE = re.compile(r"(https://api\.telegram\.org/(?:file/)?bot)[^/\s]+")
DUE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CANDIDATE_KINDS = {"request", "self_commitment"}

SUGGESTED_KEEP = 200
CANDIDATE_KEEP = 100
CANDIDATE_MAX_AGE_DAYS = 14
CHAT_MAX_IDLE_DAYS = 30

MESSAGES: dict[str, dict[str, str]] = {
    "connected": {
        "ru": "🤝 Автоматизация чатов включена ({name}). Собираю кандидатов в задачи из личных чатов; "
        "в сами чаты бот не пишет.",
        "en": "🤝 Chat automation connected ({name}). Collecting task candidates from private chats; "
        "the bot never writes into them.",
    },
    "disconnected": {
        "ru": "🔌 Автоматизация чатов отключена — кандидаты из чатов больше не собираются.",
        "en": "🔌 Chat automation disconnected — no more candidates will be collected.",
    },
    "rights_warning": {
        "ru": "⚠️ У бота есть лишние права в подключении: {rights}. Для сбора задач они не нужны — "
        "лучше снять их в настройках Telegram.",
        "en": "⚠️ The connection grants extra permissions: {rights}. Collection does not need them — "
        "consider revoking them in Telegram settings.",
    },
    "card_header": {"ru": "📥 Кандидат в задачи", "en": "📥 Task candidate"},
    "card_kind_request": {
        "ru": "Просьба к тебе в чате с {chat}",
        "en": "A request addressed to you in the chat with {chat}",
    },
    "card_kind_commitment": {
        "ru": "Ты подтвердил в чате с {chat}, что берёшь это",
        "en": "You confirmed in the chat with {chat} that you will do this",
    },
    "card_task": {"ru": "Задача: {title}", "en": "Task: {title}"},
    "card_due": {"ru": "Срок: {due}", "en": "Due: {due}"},
    "btn_take": {"ru": "✅ В задачи", "en": "✅ Create task"},
    "btn_skip": {"ru": "🙈 Пропустить", "en": "🙈 Skip"},
    "cb_stale": {
        "ru": "Карточка устарела — этого кандидата больше нет в состоянии адаптера.",
        "en": "This card is stale — the candidate is no longer in the adapter state.",
    },
    "cb_created": {"ru": "✅ Создал задачу #{task_id}.", "en": "✅ Created task #{task_id}."},
    "cb_duplicate": {"ru": "Такая задача уже есть: #{task_id}.", "en": "Already tracked as #{task_id}."},
    "cb_failed": {
        "ru": "Не получилось создать задачу — смотри логи адаптера.",
        "en": "Could not create the task — check the adapter logs.",
    },
    "cb_skipped": {"ru": "Ок, пропустил — больше не предложу.", "en": "Skipped — it will not be suggested again."},
    "card_taken_suffix": {"ru": "✅ Задача #{task_id} создана.", "en": "✅ Task #{task_id} created."},
    "card_dup_suffix": {"ru": "♻️ Уже есть как задача #{task_id}.", "en": "♻️ Already tracked as task #{task_id}."},
    "card_skipped_suffix": {"ru": "🙈 Пропущено.", "en": "🙈 Skipped."},
}

MEDIA_MARKERS: dict[str, dict[str, str]] = {
    "voice": {"ru": "[голосовое]", "en": "[voice note]"},
    "voice_untranscribed": {"ru": "[голосовое без расшифровки]", "en": "[voice note, not transcribed]"},
    "photo": {"ru": "[фото]", "en": "[photo]"},
    "video": {"ru": "[видео]", "en": "[video]"},
    "document": {"ru": "[файл {name}]", "en": "[file {name}]"},
    "attachment": {"ru": "[вложение]", "en": "[attachment]"},
}


class BusinessTrackerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BusinessConfig:
    notify_chat_id: str
    state_path: Path
    analyze_lull: float
    analyze_max_wait: float
    context_messages: int
    buffer_limit: int
    text_limit: int
    max_candidates: int
    claude_bin: str | None
    model: str | None
    analyze_timeout: float
    voice_max_seconds: int
    min_confidence: float
    ignore_chat_ids: frozenset[int]


def _lang() -> str:
    return "ru" if os.getenv("ASSISTANT_LANG", "").strip().lower().startswith("ru") else "en"


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        raise SystemExit(f"{name} must be a number") from None


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None


def load_business_config() -> BusinessConfig | None:
    """Business ingestion is opt-in: TELEGRAM_BUSINESS_TASKS=1 enables it."""
    if not _bool_env("TELEGRAM_BUSINESS_TASKS"):
        return None
    notify_chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if not notify_chat_id:
        raise SystemExit("TELEGRAM_BUSINESS_TASKS=1 requires TELEGRAM_NOTIFY_CHAT_ID (candidate cards go there)")
    raw_ignores = os.getenv("BUSINESS_IGNORE_CHAT_IDS", "")
    try:
        ignore_chat_ids = frozenset(int(item) for item in raw_ignores.split(",") if item.strip())
    except ValueError:
        raise SystemExit("BUSINESS_IGNORE_CHAT_IDS must contain comma-separated numeric chat ids") from None
    return BusinessConfig(
        notify_chat_id=notify_chat_id,
        state_path=Path(os.getenv("TELEGRAM_BUSINESS_STATE", ".adapter_state/telegram_business_state.json")),
        analyze_lull=_float_env("BUSINESS_ANALYZE_LULL", 180.0, 30.0),
        analyze_max_wait=_float_env("BUSINESS_ANALYZE_MAX_WAIT", 900.0, 60.0),
        context_messages=_int_env("BUSINESS_CONTEXT_MESSAGES", 10, 0),
        buffer_limit=_int_env("BUSINESS_BUFFER_LIMIT", 40, 5),
        text_limit=_int_env("BUSINESS_TEXT_LIMIT", 1500, 200),
        max_candidates=_int_env("BUSINESS_MAX_CANDIDATES", 3, 1),
        claude_bin=os.getenv("WORK_CLAUDE_BIN", "").strip() or None,
        model=os.getenv("BUSINESS_ANALYZE_MODEL", "").strip() or None,
        analyze_timeout=_float_env("BUSINESS_ANALYZE_TIMEOUT", 240.0, 30.0),
        voice_max_seconds=_int_env("BUSINESS_VOICE_MAX_SECONDS", 300, 0),
        min_confidence=min(1.0, _float_env("BUSINESS_MIN_CONFIDENCE", 0.6, 0.0)),
        ignore_chat_ids=ignore_chat_ids,
    )


# --- local state -------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("connections", {})
                data.setdefault("chats", {})
                data.setdefault("candidates", {})
                return data
        except (OSError, ValueError) as exc:
            print(f"business state unreadable, starting fresh: {exc}", file=sys.stderr)
    return {"version": 1, "connections": {}, "chats": {}, "candidates": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in state.items() if not key.startswith("_")}
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def mark_dirty(state: dict[str, Any]) -> None:
    state["_dirty"] = True


def consume_dirty(state: dict[str, Any]) -> bool:
    return bool(state.pop("_dirty", False))


# --- HTTP seams (monkeypatched in tests) -------------------------------------


def _sanitize(value: object, config: Any) -> str:
    text = str(value).replace(config.telegram_bot_token, "<redacted>")
    return TOKEN_URL_RE.sub(r"\1<redacted>", text)


def _telegram_call(config: Any, method: str, payload: dict[str, Any], *, timeout: float = 20) -> dict[str, Any] | None:
    """Call the Bot API; None on failure (sanitized, never raises)."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/{method}",
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print(f"business telegram {method} failed: {_sanitize(exc, config)}", file=sys.stderr)
        return None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.ok or not data.get("ok"):
        detail = _sanitize(data or response.text[:200], config)
        print(f"business telegram {method} failed with HTTP {response.status_code}: {detail}", file=sys.stderr)
        return None
    result = data.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _tracker_call(config: Any, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{config.task_tracker_url}{path}",
            headers={
                "Authorization": f"Bearer {config.task_tracker_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise BusinessTrackerError(f"Task Assistant request to {path} failed: {exc}") from None
    if not response.ok:
        raise BusinessTrackerError(
            f"Task Assistant request to {path} failed with HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return dict(response.json())
    except ValueError as exc:
        raise BusinessTrackerError(f"Task Assistant request to {path} returned invalid JSON") from exc


# --- update handling ---------------------------------------------------------


def is_business_update(update: dict[str, Any]) -> bool:
    return any(key in update for key in BUSINESS_UPDATE_TYPES)


def handle_business_update(config: Any, state: dict[str, Any], update: dict[str, Any]) -> dict[str, int]:
    now = time.time()
    connection = update.get("business_connection")
    if connection is not None:
        _handle_connection(config, state, connection)
        return {}
    message = update.get("business_message")
    if message is not None:
        _buffer_message(config, state, message, edited=False, now=now)
        return {}
    message = update.get("edited_business_message")
    if message is not None:
        _buffer_message(config, state, message, edited=True, now=now)
        return {}
    deleted = update.get("deleted_business_messages")
    if deleted is not None:
        _drop_messages(state, deleted)
        return {}
    return {}


def _connection_rights(connection: dict[str, Any]) -> list[str]:
    """Granted permissions beyond passive reading — worth warning about."""
    rights = connection.get("rights")
    granted = {key for key, value in rights.items() if value} if isinstance(rights, dict) else set()
    if connection.get("can_reply"):
        granted.add("can_reply")
    granted.discard("can_read_messages")
    return sorted(granted)


def _handle_connection(config: Any, state: dict[str, Any], connection: dict[str, Any]) -> None:
    connection_id = str(connection.get("id") or "")
    user = connection.get("user") or {}
    name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part) or (
        f"@{user.get('username')}" if user.get("username") else str(user.get("id") or "?")
    )
    enabled = bool(connection.get("is_enabled"))
    extra_rights = _connection_rights(connection)
    state.setdefault("connections", {})[connection_id] = {
        "user_id": user.get("id"),
        "name": name,
        "is_enabled": enabled,
        "extra_rights": extra_rights,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    mark_dirty(state)
    lang = _lang()
    text = MESSAGES["connected" if enabled else "disconnected"][lang].format(name=name)
    if enabled and extra_rights:
        text = f"{text}\n\n{MESSAGES['rights_warning'][lang].format(rights=', '.join(extra_rights))}"
    _telegram_call(config, "sendMessage", {"chat_id": config.business.notify_chat_id, "text": text})


def _chat_entry(state: dict[str, Any], chat: dict[str, Any]) -> dict[str, Any]:
    chat_key = str(chat.get("id"))
    entry = state.setdefault("chats", {}).setdefault(
        chat_key,
        {"title": "", "messages": [], "analyzed_through": 0, "last_activity": 0.0, "suggested": [], "failures": 0},
    )
    title = " ".join(part for part in [chat.get("first_name"), chat.get("last_name")] if part) or (
        chat.get("title") or chat.get("username") or chat_key
    )
    entry["title"] = title
    return entry


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _render_message_text(config: Any, message: dict[str, Any]) -> str | None:
    """Flatten a business message into one analyzable line of text."""
    lang = _lang()
    text = str(message.get("text") or "").strip()
    if text:
        return _shorten(text, config.business.text_limit)
    caption = str(message.get("caption") or "").strip()
    marker = None
    if message.get("voice") or message.get("video_note"):
        media = message.get("voice") or message.get("video_note") or {}
        transcript = _transcribe_voice(config, media)
        if transcript:
            marker = f"{MEDIA_MARKERS['voice'][lang]} {transcript}"
        else:
            marker = MEDIA_MARKERS["voice_untranscribed"][lang]
    elif message.get("photo"):
        marker = MEDIA_MARKERS["photo"][lang]
    elif message.get("video"):
        marker = MEDIA_MARKERS["video"][lang]
    elif message.get("document"):
        name = str((message.get("document") or {}).get("file_name") or "").strip() or "?"
        marker = MEDIA_MARKERS["document"][lang].format(name=name)
    elif caption or message.get("audio") or message.get("sticker") or message.get("location"):
        marker = MEDIA_MARKERS["attachment"][lang]
    if marker is None and not caption:
        return None
    combined = f"{marker} {caption}".strip() if marker else caption
    return _shorten(combined, config.business.text_limit)


def _transcribe_voice(config: Any, media: dict[str, Any]) -> str | None:
    """Local STT for business voice notes; None quietly degrades to a marker."""
    import speech_to_text

    duration = int(media.get("duration") or 0)
    if duration > config.business.voice_max_seconds:
        return None
    ok, _reason = speech_to_text.available()
    if not ok:
        return None
    file_id = str(media.get("file_id") or "")
    if not file_id:
        return None
    result = _telegram_call(config, "getFile", {"file_id": file_id})
    file_path = str((result or {}).get("file_path") or "")
    if not file_path:
        return None
    with tempfile.TemporaryDirectory(prefix="pta-business-voice-") as workdir:
        audio = Path(workdir) / (Path(file_path).name or "voice.oga")
        try:
            response = requests.get(
                f"https://api.telegram.org/file/bot{config.telegram_bot_token}/{file_path}",
                timeout=60,
            )
            if not response.ok:
                return None
            audio.write_bytes(response.content)
        except (requests.RequestException, OSError) as exc:
            print(f"business voice download failed: {_sanitize(exc, config)}", file=sys.stderr)
            return None
        return speech_to_text.transcribe(audio, lang="ru" if _lang() == "ru" else None)


def _buffer_message(config: Any, state: dict[str, Any], message: dict[str, Any], *, edited: bool, now: float) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    try:
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return
    if chat_id in config.business.ignore_chat_ids:
        return
    message_id = int(message.get("message_id") or 0)
    if not message_id:
        return
    text = _render_message_text(config, message)
    if not text:
        return
    entry = _chat_entry(state, chat)
    if edited and message_id <= int(entry.get("analyzed_through") or 0):
        return  # conservative: never re-open an already analyzed message
    # In a private chat the counterpart's user id IS the chat id, so the
    # owner's outgoing messages are exactly those sent by someone else.
    sender = (message.get("from") or {}).get("id")
    record = {"id": message_id, "out": sender != chat_id, "text": text, "buffered_at": now}
    messages = entry["messages"]
    for index, existing in enumerate(messages):
        if int(existing.get("id") or 0) == message_id:
            messages[index] = {**record, "buffered_at": existing.get("buffered_at", now)}
            break
    else:
        messages.append(record)
    messages.sort(key=lambda item: int(item.get("id") or 0))
    del messages[: -config.business.buffer_limit]
    entry["last_activity"] = now
    mark_dirty(state)


def _drop_messages(state: dict[str, Any], deleted: dict[str, Any]) -> None:
    chat_key = str((deleted.get("chat") or {}).get("id"))
    entry = (state.get("chats") or {}).get(chat_key)
    if entry is None:
        return
    doomed = {int(item) for item in deleted.get("message_ids") or []}
    if not doomed:
        return
    entry["messages"] = [item for item in entry["messages"] if int(item.get("id") or 0) not in doomed]
    mark_dirty(state)


# --- analysis ----------------------------------------------------------------


def _unanalyzed(chat: dict[str, Any]) -> list[dict[str, Any]]:
    watermark = int(chat.get("analyzed_through") or 0)
    return [item for item in chat.get("messages") or [] if int(item.get("id") or 0) > watermark]


def _prune(state: dict[str, Any], now: float) -> None:
    candidates = state.get("candidates") or {}
    fresh: dict[str, Any] = {}
    for key, candidate in candidates.items():
        try:
            created = datetime.fromisoformat(str(candidate.get("created_at"))).timestamp()
        except (TypeError, ValueError):
            created = now
        if now - created <= CANDIDATE_MAX_AGE_DAYS * 86400:
            fresh[key] = candidate
    overflow = sorted(fresh, key=lambda item: str(fresh[item].get("created_at") or ""))[:-CANDIDATE_KEEP]
    for key in overflow:
        del fresh[key]
    if len(fresh) != len(candidates):
        state["candidates"] = fresh
        mark_dirty(state)
    chats = state.get("chats") or {}
    active_chat_keys = {key.split(":", 1)[0] for key in state.get("candidates", {})}
    for chat_key in list(chats):
        chat = chats[chat_key]
        idle = now - float(chat.get("last_activity") or 0)
        if idle > CHAT_MAX_IDLE_DAYS * 86400 and not _unanalyzed(chat) and chat_key not in active_chat_keys:
            del chats[chat_key]
            mark_dirty(state)


def tick(
    config: Any,
    state: dict[str, Any],
    *,
    now: float | None = None,
    run_analysis: Any = None,
    max_chats: int = 2,
) -> dict[str, int]:
    """Analyze chats whose buffer has settled; called once per polling cycle."""
    counts = {"items_checked": 0, "candidates": 0, "suppressed": 0}
    business = config.business
    if business is None:
        return counts
    current = time.time() if now is None else now
    _prune(state, current)
    due: list[str] = []
    for chat_key, chat in (state.get("chats") or {}).items():
        pending = _unanalyzed(chat)
        if not pending:
            continue
        quiet_for = current - float(chat.get("last_activity") or 0)
        oldest_wait = current - float(pending[0].get("buffered_at") or current)
        if quiet_for >= business.analyze_lull or oldest_wait >= business.analyze_max_wait:
            due.append(chat_key)
    for chat_key in due[:max_chats]:
        chat_counts = _analyze_chat(config, state, chat_key, run_analysis=run_analysis)
        for key in counts:
            counts[key] += chat_counts.get(key, 0)
    return counts


def _analyze_chat(config: Any, state: dict[str, Any], chat_key: str, *, run_analysis: Any = None) -> dict[str, int]:
    business = config.business
    counts = {"items_checked": 0, "candidates": 0, "suppressed": 0}
    chat = state["chats"][chat_key]
    watermark = int(chat.get("analyzed_through") or 0)
    pending = _unanalyzed(chat)
    if not pending:
        return counts
    counts["items_checked"] = len(pending)
    context = [item for item in chat["messages"] if int(item.get("id") or 0) <= watermark]
    context = context[-business.context_messages :] if business.context_messages else []
    prompt = build_analysis_prompt(
        chat.get("title") or chat_key,
        context,
        pending,
        suggested=list(chat.get("suggested") or []),
        lang=_lang(),
        min_confidence=business.min_confidence,
    )
    engine = run_analysis or _run_claude
    raw = engine(config, prompt)
    if raw is None:
        chat["failures"] = int(chat.get("failures") or 0) + 1
        if chat["failures"] >= 3:
            chat["analyzed_through"] = int(pending[-1]["id"])
            chat["failures"] = 0
            print(
                f"business analysis: giving up on {len(pending)} message(s) in chat {chat_key} after 3 failures",
                file=sys.stderr,
            )
        mark_dirty(state)
        return counts
    chat["failures"] = 0
    chat["analyzed_through"] = int(pending[-1]["id"])
    mark_dirty(state)
    allowed_ids = {int(item["id"]) for item in pending}
    suggested = chat.setdefault("suggested", [])
    for candidate in parse_candidates(
        raw, allowed_ids, limit=business.max_candidates, min_confidence=business.min_confidence
    ):
        message_id = candidate["source_message_id"]
        if message_id in suggested:
            continue
        source = next((item for item in pending if int(item["id"]) == message_id), None)
        quote = str((source or {}).get("text") or "")
        item_id = f"telegram:business:{chat_key}:{message_id}"
        fingerprint = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        try:
            if not _ledger_should_process(config, item_id, fingerprint):
                counts["suppressed"] += 1
                suggested.append(message_id)
                mark_dirty(state)
                continue
        except BusinessTrackerError as exc:
            print(f"business ledger check failed: {exc}", file=sys.stderr)
            continue
        enriched = {**candidate, "chat_id": chat_key, "chat_title": chat.get("title") or chat_key, "quote": quote}
        if _send_candidate_card(config, state, enriched) is None:
            continue
        suggested.append(message_id)
        del suggested[:-SUGGESTED_KEEP]
        counts["candidates"] += 1
        try:
            _ledger_record(
                config,
                item_id,
                fingerprint,
                decision="needs_review",
                reason=f"Suggested to the owner as a Telegram business candidate ({candidate['kind']})",
            )
        except BusinessTrackerError as exc:
            print(f"business ledger record failed: {exc}", file=sys.stderr)
    return counts


def _transcript_line(entry: dict[str, Any], lang: str) -> str:
    if lang == "ru":
        who = "Я" if entry.get("out") else "Собеседник"
    else:
        who = "Me" if entry.get("out") else "Contact"
    return f"#{entry['id']} {who}: {entry['text']}"


def build_analysis_prompt(
    chat_title: str,
    context: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    *,
    suggested: list[int],
    lang: str,
    min_confidence: float,
    today: str | None = None,
) -> str:
    current_date = today or datetime.now().astimezone().date().isoformat()
    schema = (
        '{"candidates": [{"source_message_id": 0, "kind": "request" | "self_commitment", '
        '"title": "...", "details": "...", "due": "YYYY-MM-DD" | null, "confidence": 0.0}]}'
    )
    suggested_text = ", ".join(f"#{item}" for item in suggested) if suggested else ("нет" if lang == "ru" else "none")
    if lang == "ru":
        parts = [
            "Ты — фильтр входящих задач личного ассистента. Ниже фрагмент ЛИЧНОГО Telegram-чата владельца "
            f"ассистента с собеседником «{chat_title}». Строки «Я:» — сообщения владельца, «Собеседник:» — "
            f"сообщения собеседника. Сегодня {current_date}.",
            "",
        ]
        if context:
            parts.append("— контекст (уже обработано, кандидатов отсюда не брать) —")
            parts.extend(_transcript_line(item, lang) for item in context)
        parts.append("— новые сообщения (кандидатов брать только отсюда) —")
        parts.extend(_transcript_line(item, lang) for item in pending)
        parts += [
            "",
            "Найди в новых сообщениях кандидатов в задачи владельца, строго двух видов:",
            '- "request": собеседник прямо просит владельца что-то сделать (просьба, поручение, вопрос, '
            "требующий действия владельца);",
            '- "self_commitment": владелец сам подтвердил, что что-то сделает («ок, сделаю», «отправлю вечером», '
            "«гляну завтра»).",
            "",
            "НЕ включай: болтовню, новости, мнения, вежливые вопросы, дела самого собеседника, уже сделанное, "
            f"и сообщения из списка уже предложенных: {suggested_text}.",
            "Сомневаешься — не включай: пустой список лучше мусора на доске задач.",
            "",
            "Ответь ОДНИМ валидным JSON без markdown и без пояснений, по схеме:",
            schema,
            "- title: короткая формулировка задачи с глаголом, по-русски, до 120 символов;",
            "- details: 1-2 предложения контекста;",
            "- due: только если срок явно назван в чате, иначе null;",
            f"- кандидатов с confidence ниже {min_confidence:.1f} не включай.",
            'Если кандидатов нет — {"candidates": []}.',
        ]
        return "\n".join(parts)
    parts = [
        "You are the intake filter of a personal task assistant. Below is a fragment of the owner's PRIVATE "
        f"Telegram chat with “{chat_title}”. Lines starting with “Me:” are the owner's messages, “Contact:” "
        f"the counterpart's. Today is {current_date}.",
        "",
    ]
    if context:
        parts.append("— context (already processed, never pick candidates here) —")
        parts.extend(_transcript_line(item, lang) for item in context)
    parts.append("— new messages (pick candidates ONLY from here) —")
    parts.extend(_transcript_line(item, lang) for item in pending)
    parts += [
        "",
        "Find task candidates for the owner, strictly of two kinds:",
        '- "request": the contact directly asks the owner to do something;',
        '- "self_commitment": the owner confirmed they will do something ("ok, will do", "I\'ll send it tonight").',
        "",
        "Do NOT include chatter, news, opinions, polite questions, the contact's own work, finished work, "
        f"or already-suggested messages: {suggested_text}.",
        "When in doubt, leave it out: an empty list beats noise on the board.",
        "",
        "Answer with ONE valid JSON object, no markdown, no commentary, following this schema:",
        schema,
        "- title: a short actionable phrasing, max 120 characters;",
        "- details: 1-2 sentences of context;",
        "- due: only when a deadline is explicitly named, otherwise null;",
        f"- omit candidates with confidence below {min_confidence:.1f}.",
        'No candidates -> {"candidates": []}.',
    ]
    return "\n".join(parts)


def _result_text(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    if isinstance(payload, dict):
        return str(payload.get("result") or "").strip()
    return stdout.strip()


def _run_claude(config: Any, prompt: str) -> str | None:
    """Headless Claude Code session — subscription-billed, no tools, no MCP."""
    business = config.business
    binary = business.claude_bin or shutil.which("claude")
    if not binary or not os.access(binary, os.X_OK):
        print("business analysis skipped: claude CLI not found (set WORK_CLAUDE_BIN).", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory(prefix="pta-business-") as workdir:
        mcp_config = Path(workdir) / "mcp-config.json"
        mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
        command = [
            binary,
            "-p",
            prompt,
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--output-format",
            "json",
            "--permission-mode",
            "default",
        ]
        if business.model:
            command += ["--model", business.model]
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=business.analyze_timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"business analysis: claude cut off after {business.analyze_timeout:.0f}s.", file=sys.stderr)
            return None
        except OSError as exc:
            print(f"business analysis failed to start {binary}: {exc}", file=sys.stderr)
            return None
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:300] or _result_text(completed.stdout)[:300]
        print(f"business analysis: claude exited {completed.returncode}: {detail}", file=sys.stderr)
        return None
    return _result_text(completed.stdout)


def parse_candidates(
    raw: str,
    allowed_ids: set[int],
    *,
    limit: int = 3,
    min_confidence: float = 0.6,
) -> list[dict[str, Any]]:
    """Extract validated candidates from the model's reply; tolerant of fences."""
    if not raw:
        return []
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    items = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            message_id = int(item.get("source_message_id"))
        except (TypeError, ValueError):
            continue
        kind = str(item.get("kind") or "")
        title = " ".join(str(item.get("title") or "").split())
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if message_id not in allowed_ids or message_id in seen or kind not in CANDIDATE_KINDS or not title:
            continue
        if confidence < min_confidence:
            continue
        due = str(item.get("due") or "").strip()
        seen.add(message_id)
        result.append(
            {
                "source_message_id": message_id,
                "kind": kind,
                "title": title[:160],
                "details": " ".join(str(item.get("details") or "").split())[:600],
                "due": due if DUE_DATE_RE.fullmatch(due) else None,
                "confidence": confidence,
            }
        )
        if len(result) >= limit:
            break
    return result


# --- candidate cards and callbacks -------------------------------------------


def _candidate_keyboard(chat_id: str, message_id: int, lang: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": MESSAGES["btn_take"][lang], "callback_data": f"cand:take:{chat_id}:{message_id}"},
                {"text": MESSAGES["btn_skip"][lang], "callback_data": f"cand:skip:{chat_id}:{message_id}"},
            ]
        ]
    }


def build_candidate_card(candidate: dict[str, Any], lang: str) -> str:
    kind_key = "card_kind_commitment" if candidate.get("kind") == "self_commitment" else "card_kind_request"
    lines = [
        MESSAGES["card_header"][lang],
        MESSAGES[kind_key][lang].format(chat=candidate.get("chat_title") or "?"),
        "",
        MESSAGES["card_task"][lang].format(title=candidate.get("title") or "?"),
    ]
    if candidate.get("details"):
        lines.append(str(candidate["details"]))
    if candidate.get("due"):
        lines.append(MESSAGES["card_due"][lang].format(due=candidate["due"]))
    quote = str(candidate.get("quote") or "").strip()
    if quote:
        lines += ["", f"«{_shorten(quote, 400)}»"]
    return "\n".join(lines)


def _send_candidate_card(config: Any, state: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    lang = _lang()
    result = _telegram_call(
        config,
        "sendMessage",
        {
            "chat_id": config.business.notify_chat_id,
            "text": build_candidate_card(candidate, lang),
            "reply_markup": _candidate_keyboard(str(candidate["chat_id"]), candidate["source_message_id"], lang),
        },
    )
    if result is None:
        return None
    key = f"{candidate['chat_id']}:{candidate['source_message_id']}"
    state.setdefault("candidates", {})[key] = {
        **candidate,
        "card_message_id": result.get("message_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    mark_dirty(state)
    return result


def _append_to_card(config: Any, callback: dict[str, Any], suffix: str) -> None:
    """Stamp the outcome onto the card; dropping reply_markup removes buttons."""
    message = callback.get("message") or {}
    text = str(message.get("text") or "")
    if not text or not message.get("message_id"):
        return
    _telegram_call(
        config,
        "editMessageText",
        {
            "chat_id": (message.get("chat") or {}).get("id"),
            "message_id": message.get("message_id"),
            "text": f"{text}\n\n{suffix}",
        },
    )


def _task_description(candidate: dict[str, Any], lang: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    quote = _shorten(str(candidate.get("quote") or ""), 400)
    lines = [str(candidate.get("details") or "").strip()]
    if lang == "ru":
        lines += ["", f"Из личного чата с {candidate.get('chat_title') or '?'} (Telegram)."]
        if quote:
            lines.append(f"«{quote}»")
        lines += ["", f"— {stamp} (Telegram, автоматизация чатов)"]
    else:
        lines += ["", f"From the private chat with {candidate.get('chat_title') or '?'} (Telegram)."]
        if quote:
            lines.append(f"“{quote}”")
        lines += ["", f"— {stamp} (Telegram, chat automation)"]
    return "\n".join(line for line in lines if line is not None).strip()


def _ingest_candidate(config: Any, candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    namespace = hashlib.sha256(config.source_id.encode("utf-8")).hexdigest()[:16]
    task: dict[str, Any] = {
        "title": str(candidate.get("title") or "")[:240],
        "description": _task_description(candidate, _lang()),
        "status": "backlog",
        "assignee": "me",
        "priority": 3,
    }
    if candidate.get("due"):
        task["due_at"] = f"{candidate['due']}T18:00:00Z"
    identity = hashlib.sha256(
        json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    task["external_id"] = (
        f"telegram:{namespace}:business:{candidate['chat_id']}:{candidate['source_message_id']}:{identity}:0"
    )
    payload = {
        "origin": "telegram",
        "source_name": f"Telegram · {candidate.get('chat_title') or '?'}",
        "source_url": None,
        "source_context": _shorten(str(candidate.get("quote") or ""), 1000) or None,
        "tasks": [task],
    }
    result = _tracker_call(config, "POST", "/api/agent/ingest/context", payload)
    created = list(result.get("created") or [])
    duplicates = list(result.get("duplicates") or [])
    if created:
        return "created", created[0]
    if duplicates:
        return "duplicate", duplicates[0]
    return "failed", None


def _ledger_should_process(config: Any, source_item_id: str, content_fingerprint: str) -> bool:
    result = _tracker_call(
        config,
        "POST",
        "/api/agent/ingestion/check",
        {
            "source_id": config.source_id,
            "items": [{"source_item_id": source_item_id, "content_fingerprint": content_fingerprint}],
        },
    )
    items = result.get("items") or []
    if len(items) != 1:
        raise BusinessTrackerError("ingestion check returned an unexpected item count")
    return bool(items[0].get("should_process"))


def _ledger_record(
    config: Any,
    source_item_id: str,
    content_fingerprint: str,
    *,
    decision: str,
    reason: str,
    task_id: int | None = None,
    decided_by: str = "adapter",
) -> None:
    _tracker_call(
        config,
        "POST",
        "/api/agent/ingestion/decisions",
        {
            "source_id": config.source_id,
            "source_item_id": source_item_id,
            "content_fingerprint": content_fingerprint,
            "decision": decision,
            "reason": reason,
            "task_id": task_id,
            "decided_by": decided_by,
        },
    )


def handle_business_callback(config: Any, state: dict[str, Any], callback: dict[str, Any]) -> dict[str, int]:
    """The owner pressed a button on a candidate card in the notify chat."""
    counts = {"created": 0, "duplicates": 0, "ignored": 0}
    lang = _lang()
    answer: dict[str, Any] = {"callback_query_id": callback.get("id")}
    match = CANDIDATE_CALLBACK_RE.match(str(callback.get("data") or ""))
    candidate = (state.get("candidates") or {}).get(f"{match.group(2)}:{match.group(3)}") if match else None
    if match is None or candidate is None:
        _telegram_call(config, "answerCallbackQuery", {**answer, "text": MESSAGES["cb_stale"][lang]})
        return counts
    action, chat_id, message_id = match.group(1), match.group(2), match.group(3)
    key = f"{chat_id}:{message_id}"
    item_id = f"telegram:business:{chat_id}:{message_id}"
    fingerprint = hashlib.sha256(str(candidate.get("quote") or "").encode("utf-8")).hexdigest()
    if action == "skip":
        try:
            _ledger_record(
                config,
                item_id,
                fingerprint,
                decision="ignored",
                reason="Owner skipped the business candidate card",
                decided_by="human",
            )
        except BusinessTrackerError as exc:
            print(f"business ledger record failed: {exc}", file=sys.stderr)
        state["candidates"].pop(key, None)
        mark_dirty(state)
        counts["ignored"] = 1
        _telegram_call(config, "answerCallbackQuery", {**answer, "text": MESSAGES["cb_skipped"][lang]})
        _append_to_card(config, callback, MESSAGES["card_skipped_suffix"][lang])
        return counts
    try:
        outcome, task = _ingest_candidate(config, candidate)
    except BusinessTrackerError as exc:
        print(f"business ingest failed: {exc}", file=sys.stderr)
        outcome, task = "failed", None
    if outcome == "failed":
        _telegram_call(config, "answerCallbackQuery", {**answer, "text": MESSAGES["cb_failed"][lang], "show_alert": True})
        return counts
    task_id = (task or {}).get("id")
    counts["created" if outcome == "created" else "duplicates"] = 1
    try:
        _ledger_record(
            config,
            item_id,
            fingerprint,
            decision=outcome,
            reason="Owner accepted the business candidate card",
            task_id=task_id,
            decided_by="human",
        )
    except BusinessTrackerError as exc:
        print(f"business ledger record failed: {exc}", file=sys.stderr)
    state["candidates"].pop(key, None)
    mark_dirty(state)
    text_key = "cb_created" if outcome == "created" else "cb_duplicate"
    suffix_key = "card_taken_suffix" if outcome == "created" else "card_dup_suffix"
    _telegram_call(config, "answerCallbackQuery", {**answer, "text": MESSAGES[text_key][lang].format(task_id=task_id)})
    _append_to_card(config, callback, MESSAGES[suffix_key][lang].format(task_id=task_id))
    return counts
