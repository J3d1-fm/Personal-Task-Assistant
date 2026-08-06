"""Tests for the outbound Telegram notification helpers (automation/notify.py).

Everything runs offline: requests.post is monkeypatched and captured. The
invariants that matter — chunking at Telegram's limit, buttons only on the
final chunk, token redaction, and disabled-by-default configuration — are all
enforced here.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automation"))

import notify  # noqa: E402
from notify import (  # noqa: E402
    NotifyConfig,
    chunk_message,
    load_notify_config,
    redact,
    review_reply_markup,
    send_message,
)


def _config(**overrides) -> NotifyConfig:
    values = {"bot_token": "123456:SECRET-TOKEN", "chat_id": "42"}
    values.update(overrides)
    return NotifyConfig(**values)


class _Response:
    ok = True
    status_code = 200
    text = ""


def test_load_notify_config_disabled_without_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET")
    monkeypatch.delenv("TELEGRAM_NOTIFY_CHAT_ID", raising=False)
    assert load_notify_config() is None


def test_load_notify_config_reads_voice_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_DIGEST_VOICE", "1")
    monkeypatch.setenv("TELEGRAM_DIGEST_VOICE_NAME", "Samantha")
    config = load_notify_config()
    assert config == NotifyConfig(
        bot_token="123456:SECRET", chat_id="42", voice_enabled=True, voice_name="Samantha"
    )


def test_chunk_message_keeps_short_text_whole():
    assert chunk_message("hello\nworld") == ["hello\nworld"]
    assert chunk_message("") == []


def test_chunk_message_splits_at_line_boundaries():
    lines = [f"line {i} " + "x" * 90 for i in range(60)]
    chunks = chunk_message("\n".join(lines), limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "\n".join(chunks).splitlines() == lines


def test_chunk_message_hard_splits_an_overlong_line():
    chunks = chunk_message("a" * 2500, limit=1000)
    assert [len(c) for c in chunks] == [1000, 1000, 500]


def test_send_message_attaches_buttons_to_final_chunk_only(monkeypatch):
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(data)
        return _Response()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    markup = review_reply_markup(7)
    long_text = "\n".join("x" * 100 for _ in range(60))
    assert send_message(_config(), long_text, reply_markup=markup)
    assert len(calls) > 1
    assert all("reply_markup" not in call for call in calls[:-1])
    assert calls[-1]["reply_markup"] == markup


def test_send_message_reports_failure(monkeypatch):
    class _Failed:
        ok = False
        status_code = 500
        text = "boom https://api.telegram.org/bot123456:SECRET-TOKEN/sendMessage"

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: _Failed())
    assert not send_message(_config(), "hello")


def test_redact_hides_the_bot_token():
    message = "error at https://api.telegram.org/bot123456:SECRET-TOKEN/sendMessage token 123456:SECRET-TOKEN"
    cleaned = redact(message, "123456:SECRET-TOKEN")
    assert "SECRET-TOKEN" not in cleaned
    assert "<redacted>" in cleaned


def test_review_reply_markup_encodes_all_three_actions():
    markup = json.loads(review_reply_markup(15))
    data = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert data == ["task:done:15", "task:rework:15", "task:block:15"]
