"""Tests for the packaging helpers: the container scheduler and release notes."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))
sys.path.insert(0, str(ROOT / "scripts"))

from release_notes import extract_section  # noqa: E402
from scheduler import parse_ritual_time, seconds_until_next_run  # noqa: E402


def test_parse_ritual_time():
    assert parse_ritual_time("15:00") == (15, 0)
    assert parse_ritual_time("9:5") == (9, 5)
    assert parse_ritual_time(" 09:30 ") == (9, 30)
    with pytest.raises(ValueError):
        parse_ritual_time("25:00")
    with pytest.raises(ValueError):
        parse_ritual_time("12:61")


def test_seconds_until_next_run_same_day():
    now = datetime(2026, 7, 7, 12, 0, 0)
    assert seconds_until_next_run(now, 15, 0) == 3 * 3600


def test_seconds_until_next_run_rolls_to_tomorrow():
    now = datetime(2026, 7, 7, 15, 0, 0)  # exactly at target -> tomorrow
    assert seconds_until_next_run(now, 15, 0) == 24 * 3600
    just_after = datetime(2026, 7, 7, 15, 0, 1)
    assert seconds_until_next_run(just_after, 15, 0) == 24 * 3600 - 1


CHANGELOG_SAMPLE = """Personal Task Assistant Changelog

Version 0.7.0 - 2026-07-08

Packaging:
- Added docker-compose deployment.
- Bumped the app version from 0.6.0 to 0.7.0.

Version 0.6.0 - 2026-07-07

- Older entry.
"""


def test_extract_section_returns_only_that_version():
    section = extract_section(CHANGELOG_SAMPLE, "0.7.0")
    assert "docker-compose deployment" in section
    assert "Older entry" not in section
    assert "Version 0.6.0" not in section


def test_extract_section_missing_version():
    assert extract_section(CHANGELOG_SAMPLE, "9.9.9") is None


def test_real_changelog_has_current_version_section():
    from app.config import get_settings

    changelog = (ROOT / "CHANGELOG.txt").read_text(encoding="utf-8")
    assert extract_section(changelog, get_settings().app_version) is not None, (
        "CHANGELOG.txt must contain a section for the current app_version "
        "(the release workflow depends on it)"
    )
