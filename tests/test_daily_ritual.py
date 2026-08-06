import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automation"))

from daily_ritual import build_situation, render_digest  # noqa: E402

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)


def _task(**overrides) -> dict:
    base = {
        "id": 1,
        "title": "t",
        "status": "backlog",
        "assignee": "unassigned",
        "origin": "manual",
        "priority": 3,
        "due_at": None,
        "reminder_at": None,
        "source_name": None,
    }
    base.update(overrides)
    return base


def test_classifies_each_bucket():
    tasks = [
        _task(id=1, status="backlog", assignee="codex", due_at="2020-01-01T00:00:00Z"),  # overdue + agent
        _task(id=2, status="waiting_review", assignee="me"),
        _task(id=3, status="blocked", assignee="me"),
        _task(id=4, status="backlog", assignee="unassigned"),  # needs triage
        _task(id=5, status="backlog", assignee="codex", due_at="2026-07-07T20:00:00Z"),  # due soon + agent
        _task(id=6, status="done", assignee="codex"),  # inactive
        _task(id=7, status="cancelled", assignee="codex"),  # inactive
    ]
    s = build_situation(tasks, NOW)

    assert s.total_active == 5
    assert [t["id"] for t in s.overdue] == [1]
    assert [t["id"] for t in s.due_soon] == [5]
    assert [t["id"] for t in s.blocked] == [3]
    assert [t["id"] for t in s.waiting_review] == [2]
    assert [t["id"] for t in s.needs_triage] == [4]
    assert sorted(t["id"] for t in s.agent_ready) == [1, 5]


def test_overdue_not_double_counted_as_due_soon():
    tasks = [_task(id=1, status="backlog", assignee="codex", due_at="2020-01-01T00:00:00Z")]
    s = build_situation(tasks, NOW)
    assert s.overdue and not s.due_soon


def test_reminder_alone_can_make_overdue():
    tasks = [_task(id=1, assignee="me", reminder_at="2026-07-01T00:00:00Z")]
    s = build_situation(tasks, NOW)
    assert [t["id"] for t in s.overdue] == [1]


def test_digest_has_sections_and_counts():
    tasks = [
        _task(id=1, status="waiting_review", assignee="codex"),
        _task(id=2, status="blocked", assignee="me"),
    ]
    text = render_digest(build_situation(tasks, NOW))
    assert "# Daily task digest" in text
    assert "2 active" in text
    assert "## Needs you" in text
    assert "Waiting for your review (1)" in text
    assert "Blocked" in text
    assert "_Agent work loop was not run this cycle._" in text


def test_digest_reports_worked_tasks():
    worked = [_task(id=9, title="did a thing", assignee="codex")]
    s = build_situation([], NOW)
    text = render_digest(s, worked=worked)
    assert "Worked this cycle (1)" in text
    assert "did a thing" in text
    assert "waiting_review" in text


def test_empty_board_is_all_clear():
    text = render_digest(build_situation([], NOW))
    assert "0 active" in text
    assert "Nothing overdue" in text


# ---------- spoken digest + Telegram delivery ----------


def test_render_spoken_covers_the_hot_items():
    import daily_ritual

    tasks = [
        _task(id=1, title="pay the invoice", status="backlog", assignee="me", due_at="2020-01-01T00:00:00Z"),
        _task(id=2, title="check the deck", status="waiting_review", assignee="me"),
        _task(id=3, status="backlog", assignee="codex"),
    ]
    spoken = daily_ritual.render_spoken(build_situation(tasks, NOW))
    assert "3 active tasks" in spoken
    assert "pay the invoice" in spoken
    assert "check the deck" in spoken
    assert "1 task ready for the agent." in spoken


def test_render_spoken_empty_board_and_worked():
    import daily_ritual

    spoken = daily_ritual.render_spoken(build_situation([], NOW))
    assert "Nothing is overdue." in spoken
    worked = [_task(id=9, title="sorted inbox")]
    spoken = daily_ritual.render_spoken(build_situation([], NOW), worked=worked)
    assert "The agent finished 1 task to review: sorted inbox." in spoken


def test_notify_digest_noop_when_unconfigured(monkeypatch):
    import daily_ritual
    import notify

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_NOTIFY_CHAT_ID", raising=False)
    monkeypatch.setattr(
        notify, "send_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send"))
    )
    daily_ritual.notify_digest("digest", "spoken", NOW)


def test_notify_digest_sends_text_and_voice(monkeypatch, tmp_path):
    import daily_ritual
    import notify

    config = notify.NotifyConfig(bot_token="t", chat_id="1", voice_enabled=True)
    sent, audio_sent, synthesized = [], [], []
    monkeypatch.setattr(notify, "load_notify_config", lambda: config)
    monkeypatch.setattr(notify, "send_message", lambda _c, text, **k: sent.append(text) or True)
    monkeypatch.setattr(notify, "send_audio", lambda _c, path, title: audio_sent.append(path) or True)

    def fake_synthesize(text, target, voice=None):
        synthesized.append((text, voice))
        return tmp_path / "digest.m4a"

    monkeypatch.setattr(notify, "synthesize_speech", fake_synthesize)
    daily_ritual.notify_digest("the digest", "the spoken script", NOW)
    assert sent == ["the digest"]
    assert synthesized == [("the spoken script", None)]
    assert audio_sent == [tmp_path / "digest.m4a"]


def test_notify_digest_text_only_without_voice_flag(monkeypatch):
    import daily_ritual
    import notify

    config = notify.NotifyConfig(bot_token="t", chat_id="1", voice_enabled=False)
    sent = []
    monkeypatch.setattr(notify, "load_notify_config", lambda: config)
    monkeypatch.setattr(notify, "send_message", lambda _c, text, **k: sent.append(text) or True)
    monkeypatch.setattr(
        notify,
        "synthesize_speech",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("voice must not run")),
    )
    daily_ritual.notify_digest("the digest", "spoken", NOW)
    assert sent == ["the digest"]
