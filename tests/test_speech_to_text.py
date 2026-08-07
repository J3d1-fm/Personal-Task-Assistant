"""Tests for the local STT helper (adapters/speech_to_text.py) — offline:
binaries are mocked, no ffmpeg or whisper runs."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

import speech_to_text  # noqa: E402


def test_parse_transcript_output_strips_headers_and_timestamps():
    raw = (
        "# language=ru duration=5.90s\n"
        "[0000.00-0003.32] Закрыто, пароль сменили.\n"
        "[0003.32-0005.88] Добавь задачу.\n"
    )
    assert speech_to_text.parse_transcript_output(raw) == "Закрыто, пароль сменили. Добавь задачу."


def test_parse_transcript_output_tolerates_plain_text():
    assert speech_to_text.parse_transcript_output("plain text\nsecond line\n") == "plain text second line"
    assert speech_to_text.parse_transcript_output("# only header\n") == ""


def test_available_reports_missing_binary(monkeypatch):
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.delenv("WHISPER_BIN", raising=False)
    monkeypatch.setattr(speech_to_text.shutil, "which", lambda name: None)
    ok, reason = speech_to_text.available()
    assert not ok and "ffmpeg" in reason


def test_transcribe_builds_commands_and_reads_output(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.delenv("WHISPER_BIN", raising=False)
    monkeypatch.setattr(speech_to_text.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0].endswith("whisper"):
            out_index = command.index("-o") + 1
            Path(command[out_index]).write_text(
                "# language=ru duration=2s\n[0000.00-0002.00] привет мир\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(speech_to_text.subprocess, "run", fake_run)
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"fake")
    assert speech_to_text.transcribe(audio, lang="ru") == "привет мир"
    ffmpeg_cmd, whisper_cmd = calls
    assert "-ar" in ffmpeg_cmd and "16000" in ffmpeg_cmd
    assert whisper_cmd[whisper_cmd.index("--model") + 1] == "small"
    assert whisper_cmd[whisper_cmd.index("--language") + 1] == "ru"


def test_transcribe_returns_none_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(speech_to_text.shutil, "which", lambda name: f"/usr/bin/{name}")

    def failing_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b"boom")

    monkeypatch.setattr(speech_to_text.subprocess, "run", failing_run)
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"fake")
    assert speech_to_text.transcribe(audio) is None


def test_synthesize_voice_prefers_piper_and_outputs_ogg(monkeypatch, tmp_path):
    piper = tmp_path / "piper"
    piper.write_text("#!/bin/sh\n")
    piper.chmod(0o755)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"model")
    monkeypatch.setenv("PIPER_BIN", str(piper))
    monkeypatch.setenv("PIPER_MODEL", str(model))
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(speech_to_text.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(speech_to_text.subprocess, "run", fake_run)
    target = tmp_path / "answer.ogg"
    assert speech_to_text.synthesize_voice("привет", target) == target
    assert calls[0][0] == str(piper)
    assert "-m" in calls[0] and str(model) in calls[0]
    assert "libopus" in calls[1]


def test_synthesize_voice_falls_back_to_say(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPER_BIN", raising=False)
    monkeypatch.delenv("PIPER_MODEL", raising=False)
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.delenv("TTS_SAY_VOICE", raising=False)
    monkeypatch.setattr(speech_to_text.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(speech_to_text.subprocess, "run", fake_run)
    assert speech_to_text.synthesize_voice("привет", tmp_path / "a.ogg", lang="ru") is not None
    assert calls[0][0].endswith("say")
    assert "Milena" in calls[0]
