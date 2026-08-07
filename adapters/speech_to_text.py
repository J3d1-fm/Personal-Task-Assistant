#!/usr/bin/env python3
"""Local speech in both directions for the Telegram adapter — no cloud, no keys.

Voice notes are normalized with ffmpeg (16 kHz mono WAV) and transcribed by a
whisper CLI on this machine (a faster-whisper wrapper or any compatible
command that accepts `--model`, `--language`, `-o OUTPUT`, and an input file,
and writes optionally-timestamped text). Everything runs on-device.

The reverse direction — text to speech for voice answers — prefers a local
Piper neural voice (`PIPER_BIN` + `PIPER_MODEL`, .onnx) and falls back to
macOS `say`; output is OGG/Opus, the format Telegram voice bubbles expect.

Environment:
- FFMPEG_BIN       ffmpeg path (default: `ffmpeg` on PATH). Set an absolute
                   path for launchd, whose PATH is minimal.
- WHISPER_BIN      whisper CLI path (default: `whisper` on PATH).
- WHISPER_MODEL    model name or path (default: `small` — multilingual;
                   English-only models mangle Russian).
- WHISPER_HF_HOME  stable model cache for wrappers that default HF_HOME to
                   the current directory.
- PIPER_BIN        piper CLI path (neural TTS; optional).
- PIPER_MODEL      piper voice model path (.onnx; required with PIPER_BIN).
- TTS_SAY_VOICE    fallback macOS `say` voice (default Milena for Russian).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MODEL = "small"
DEFAULT_HF_HOME = str(Path.home() / ".local" / "share" / "codex-tools" / "hf-cache")
TIMESTAMP_LINE_RE = re.compile(r"^\[\d+(?:\.\d+)?-\d+(?:\.\d+)?\]\s*(.*)$")


def _binary(env_name: str, default: str) -> str | None:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which(default)


def available() -> tuple[bool, str]:
    if _binary("FFMPEG_BIN", "ffmpeg") is None:
        return False, "ffmpeg not found (set FFMPEG_BIN to an absolute path)"
    if _binary("WHISPER_BIN", "whisper") is None:
        return False, "whisper CLI not found (set WHISPER_BIN to an absolute path)"
    return True, ""


def parse_transcript_output(raw: str) -> str:
    """Extract plain text from the CLI output: strip `# ...` header lines and
    `[start-end]` timestamps; tolerate CLIs that print plain text already."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = TIMESTAMP_LINE_RE.match(line)
        lines.append(match.group(1).strip() if match else line)
    return " ".join(part for part in lines if part).strip()


def transcribe(audio: Path, *, lang: str | None = None, timeout: float = 300.0) -> str | None:
    """Audio file (any ffmpeg-readable format) -> plain transcript, or None."""
    ffmpeg = _binary("FFMPEG_BIN", "ffmpeg")
    whisper = _binary("WHISPER_BIN", "whisper")
    if not ffmpeg or not whisper:
        print(f"transcription skipped: {available()[1]}", file=sys.stderr)
        return None
    env = dict(os.environ)
    env.setdefault("HF_HOME", os.getenv("WHISPER_HF_HOME", DEFAULT_HF_HOME))
    model = os.getenv("WHISPER_MODEL", DEFAULT_MODEL)
    with tempfile.TemporaryDirectory(prefix="pta-stt-") as workdir:
        wav = Path(workdir) / "audio.wav"
        out = Path(workdir) / "transcript.txt"
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(audio), "-ar", "16000", "-ac", "1", str(wav)],
                check=True,
                capture_output=True,
                timeout=timeout,
            )
            command = [whisper, "--model", model]
            if lang:
                command += ["--language", lang]
            command += ["-o", str(out), str(wav)]
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=timeout, env=env, cwd=workdir
            )
        except subprocess.TimeoutExpired:
            print(f"transcription timed out after {timeout:.0f}s", file=sys.stderr)
            return None
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", "") or ""
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            print(f"transcription failed: {exc} {detail[:300]}", file=sys.stderr)
            return None
        raw = out.read_text(encoding="utf-8") if out.exists() else completed.stdout
    transcript = parse_transcript_output(raw)
    return transcript or None


# ---------- text to speech (voice answers) ----------


def tts_available() -> bool:
    if _binary("FFMPEG_BIN", "ffmpeg") is None:
        return False
    piper = os.getenv("PIPER_BIN", "").strip()
    if piper and os.access(piper, os.X_OK) and Path(os.getenv("PIPER_MODEL", "")).is_file():
        return True
    return shutil.which("say") is not None


def synthesize_voice(text: str, target_ogg: Path, *, lang: str = "ru", timeout: float = 120.0) -> Path | None:
    """Render text to OGG/Opus (Telegram voice-bubble format), or None.

    Prefers the Piper neural voice when configured; falls back to macOS
    `say`. All intermediates live in a temp dir and are removed."""
    ffmpeg = _binary("FFMPEG_BIN", "ffmpeg")
    if ffmpeg is None:
        print("voice answer skipped: ffmpeg not found", file=sys.stderr)
        return None
    piper = os.getenv("PIPER_BIN", "").strip()
    piper_model = os.getenv("PIPER_MODEL", "").strip()
    target_ogg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pta-tts-") as workdir:
        wav = Path(workdir) / "answer.wav"
        try:
            if piper and os.access(piper, os.X_OK) and Path(piper_model).is_file():
                subprocess.run(
                    [piper, "-m", piper_model, "-f", str(wav)],
                    input=text,
                    text=True,
                    check=True,
                    capture_output=True,
                    timeout=timeout,
                )
            else:
                say = shutil.which("say")
                if say is None:
                    print("voice answer skipped: neither piper nor say available", file=sys.stderr)
                    return None
                voice = os.getenv("TTS_SAY_VOICE", "").strip() or ("Milena" if lang == "ru" else "")
                command = [say, "-o", str(wav.with_suffix(".aiff"))]
                if voice:
                    command += ["-v", voice]
                command.append(text)
                subprocess.run(command, check=True, capture_output=True, timeout=timeout)
                wav = wav.with_suffix(".aiff")
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1", str(target_ogg)],
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", "") or ""
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            print(f"voice answer failed: {exc} {str(detail)[:200]}", file=sys.stderr)
            return None
    return target_ogg
