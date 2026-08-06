#!/usr/bin/env python3
"""Outbound Telegram notifications for Personal Task Assistant automation.

The core app never talks to third-party services on its own behalf — that
boundary stays. Everything outbound lives here on the automation side, used by
the daily ritual (digest delivery) and the watch loop (reminders and review
requests). Configuration is environment-only and optional: with no
TELEGRAM_NOTIFY_CHAT_ID the callers behave exactly as before this module
existed.

- TELEGRAM_BOT_TOKEN            same bot the polling adapter uses
- TELEGRAM_NOTIFY_CHAT_ID       chat that receives digests/reminders/review
                                requests; unset => notifications disabled
- TELEGRAM_DIGEST_VOICE         truthy => also send a spoken digest
- TELEGRAM_DIGEST_VOICE_NAME    optional macOS `say` voice name

Send functions return booleans instead of raising, so a Telegram outage can
never break the ritual or the watcher; errors are printed with the bot token
redacted. Telegram caps message text at 4096 characters, so texts are chunked
at line boundaries before sending.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_TOKEN_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+")


@dataclass(frozen=True)
class NotifyConfig:
    bot_token: str
    chat_id: str
    voice_enabled: bool = False
    voice_name: str | None = None
    voice_rate: int | None = None


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def assistant_lang() -> str:
    """Language of everything the assistant says (ASSISTANT_LANG=ru|en)."""
    return "ru" if os.getenv("ASSISTANT_LANG", "").strip().lower().startswith("ru") else "en"


def load_notify_config() -> NotifyConfig | None:
    """Read the outbound configuration; None means notifications are off."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if not token or token == "change-me" or not chat_id:
        return None
    rate_raw = os.getenv("TELEGRAM_DIGEST_VOICE_RATE", "").strip()
    try:
        rate = int(rate_raw) if rate_raw else None
    except ValueError:
        rate = None
    return NotifyConfig(
        bot_token=token,
        chat_id=chat_id,
        voice_enabled=_bool_env("TELEGRAM_DIGEST_VOICE"),
        voice_name=os.getenv("TELEGRAM_DIGEST_VOICE_NAME", "").strip() or None,
        voice_rate=rate,
    )


def voice_candidates(explicit: str | None, lang: str) -> list[str | None]:
    """Voices to try in order. Russian prefers the enhanced Milena when the
    user has downloaded it (Settings -> Accessibility -> Spoken Content) and
    quietly falls back to the built-in compact one."""
    if explicit:
        return [explicit]
    if lang == "ru":
        return ["Milena (Enhanced)", "Milena"]
    return [None]


def redact(text: object, token: str) -> str:
    value = str(text)
    if token:
        value = value.replace(token, "<redacted>")
    return TELEGRAM_TOKEN_URL_RE.sub(r"\1<redacted>", value)


def chunk_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring line boundaries."""
    text = text.rstrip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def _api_post(config: NotifyConfig, method: str, *, data: dict, files: dict | None = None) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.bot_token}/{method}",
            data=data,
            files=files,
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"telegram {method} failed: {redact(exc, config.bot_token)}", file=sys.stderr)
        return False
    if not response.ok:
        detail = redact(response.text[:300], config.bot_token)
        print(f"telegram {method} failed with HTTP {response.status_code}: {detail}", file=sys.stderr)
        return False
    return True


def send_message(config: NotifyConfig, text: str, *, reply_markup: str | None = None) -> bool:
    """Send text (chunked when long); buttons attach to the final chunk."""
    chunks = chunk_message(text)
    if not chunks:
        return True
    for index, chunk in enumerate(chunks):
        data: dict = {"chat_id": config.chat_id, "text": chunk}
        if reply_markup is not None and index == len(chunks) - 1:
            data["reply_markup"] = reply_markup
        if not _api_post(config, "sendMessage", data=data):
            return False
    return True


def send_audio(config: NotifyConfig, path: Path, *, title: str) -> bool:
    try:
        with path.open("rb") as handle:
            return _api_post(
                config,
                "sendAudio",
                data={"chat_id": config.chat_id, "title": title},
                files={"audio": (path.name, handle, "audio/mp4")},
            )
    except OSError as exc:
        print(f"telegram sendAudio failed: cannot read {path}: {exc}", file=sys.stderr)
        return False


def synthesize_speech(
    text: str,
    target: Path,
    *,
    voices: tuple[str | None, ...] | list[str | None] = (None,),
    rate: int | None = None,
) -> Path | None:
    """Render text to an .m4a via macOS `say` + `afconvert`; None if unavailable.

    Voices are tried in order (a not-installed voice fails fast and the next
    candidate is tried). Both binaries ship with macOS; on other platforms
    this quietly returns None so the text digest still goes out alone.
    """
    say = shutil.which("say")
    afconvert = shutil.which("afconvert")
    if not say or not afconvert:
        print("spoken digest skipped: `say`/`afconvert` not available on this system.", file=sys.stderr)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    aiff = target.with_suffix(".aiff")
    last_error = ""
    try:
        for voice in voices or (None,):
            command = [say, "-o", str(aiff)]
            if voice:
                command += ["-v", voice]
            if rate:
                command += ["-r", str(rate)]
            command.append(text)
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=120)
                subprocess.run(
                    [afconvert, "-f", "m4af", "-d", "aac", str(aiff), str(target)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                return target
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = (getattr(exc, "stderr", b"") or b"").decode(errors="replace")[:200]
                last_error = f"{exc} {detail}"
    finally:
        aiff.unlink(missing_ok=True)
    print(f"spoken digest skipped: {last_error}", file=sys.stderr)
    return None


BUTTON_LABELS = {
    "en": ("✅ Done", "🔁 Rework", "✋ Block"),
    "ru": ("✅ Готово", "🔁 Доработать", "✋ Блок"),
}


def review_reply_markup(task_id: int, lang: str = "en") -> str:
    """Inline keyboard for a review request; handled by the polling adapter.
    Labels follow the assistant language; callback_data is the protocol and
    never changes."""
    import json

    done, rework, block = BUTTON_LABELS.get(lang, BUTTON_LABELS["en"])
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": done, "callback_data": f"task:done:{task_id}"},
                    {"text": rework, "callback_data": f"task:rework:{task_id}"},
                    {"text": block, "callback_data": f"task:block:{task_id}"},
                ]
            ]
        }
    )
