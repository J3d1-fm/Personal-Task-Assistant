"""Tests for the safe .env loader (automation/env.sh) and wizard output.

The daily and watch entrypoints used to `source` .env directly, which
executes it as bash — an unquoted value with a space (the setup wizard wrote
`APP_NAME=Personal Task Assistant`) became a command invocation and killed
the runner under `set -euo pipefail`. The loader parses instead of executing;
these tests pin that behavior and keep the wizard's output loader-safe and
python-dotenv-equivalent.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_SH = ROOT / "automation" / "env.sh"

sys.path.insert(0, str(ROOT / "scripts"))


def bash_load(env_file: Path, *variables: str) -> list[str]:
    script = f'. "{ENV_SH}"; load_env "{env_file}"; ' + "; ".join(
        f'printf "%s\\n" "${{{name}:-}}"' for name in variables
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_loader_survives_spaces_comments_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "PLAIN=value\n"
        "UNQUOTED_SPACES=Personal Task Assistant\n"
        'DOUBLE_QUOTED="Task History"\n'
        "SINGLE_QUOTED='hello world'\n"
        "WITH_EQUALS=sqlite:///./x.db?mode=rwc\n"
        "CRLF_VALUE=trimmed\r\n"
        "not an assignment line\n"
        "1BAD=skipped\n",
        encoding="utf-8",
    )
    values = bash_load(
        env_file,
        "PLAIN",
        "UNQUOTED_SPACES",
        "DOUBLE_QUOTED",
        "SINGLE_QUOTED",
        "WITH_EQUALS",
        "CRLF_VALUE",
    )
    assert values == [
        "value",
        "Personal Task Assistant",
        "Task History",
        "hello world",
        "sqlite:///./x.db?mode=rwc",
        "trimmed",
    ]


def test_loader_tolerates_missing_file(tmp_path):
    assert bash_load(tmp_path / "absent.env", "APP_NAME") == [""]


def test_wizard_env_is_loader_safe_and_dotenv_equivalent(tmp_path, monkeypatch):
    import setup_wizard

    env_file = tmp_path / ".env"
    monkeypatch.setattr(setup_wizard, "ENV_FILE", env_file)
    setup_wizard.write_env_file()

    from dotenv import dotenv_values

    parsed = dotenv_values(env_file)
    assert parsed["APP_NAME"] == "Personal Task Assistant"
    assert parsed["TASK_HISTORY_SHEET_TAB"] == "Task History"

    keys = [key for key in parsed if parsed[key]]
    bash_values = bash_load(env_file, *keys)
    assert bash_values == [parsed[key] for key in keys]
