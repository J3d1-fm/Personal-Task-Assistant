"""Tests for the work-loop backend selector and the local Claude Code runner.

Everything runs offline: no CLI is spawned, no tracker is contacted. The
things that must hold: the backend is picked by WORK_RUNNER, pre-flight
reports the exact missing piece, the headless command carries the isolation
flags (--strict-mcp-config, tool allowlist scoped to the task-assistant MCP
server), the MCP config enables worker mode with the claim budget, and the
board diff attributes only tasks that entered waiting_review during the run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automation"))

import claude_work  # noqa: E402
import work_runner  # noqa: E402


def test_backend_selection(monkeypatch):
    monkeypatch.delenv("WORK_RUNNER", raising=False)
    assert work_runner.backend_name() == "api"
    for value in ("claude", "claude-code", "local", "CLAUDE"):
        monkeypatch.setenv("WORK_RUNNER", value)
        assert work_runner.backend_name() == "claude"
    monkeypatch.setenv("WORK_RUNNER", "api")
    assert work_runner.backend_name() == "api"


def test_backend_ready_reports_missing_pieces(monkeypatch):
    monkeypatch.setenv("WORK_RUNNER", "claude")
    monkeypatch.setattr(claude_work, "claude_binary", lambda: None)
    ready, reason = work_runner.backend_ready()
    assert not ready and "WORK_CLAUDE_BIN" in reason

    monkeypatch.setattr(claude_work, "claude_binary", lambda: "/usr/bin/true")
    ready, reason = work_runner.backend_ready()
    assert ready

    monkeypatch.setenv("WORK_RUNNER", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ready, reason = work_runner.backend_ready()
    assert not ready and "WORK_RUNNER=claude" in reason


def test_build_command_carries_the_isolation_flags():
    command = claude_work.build_command("/bin/claude", "/tmp/cfg.json")
    assert command[0] == "/bin/claude"
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == "/tmp/cfg.json"
    assert command[command.index("--allowed-tools") + 1] == "mcp__task-assistant"
    assert command[command.index("--output-format") + 1] == "json"
    assert "--model" not in command
    with_model = claude_work.build_command("/bin/claude", "/tmp/cfg.json", model="claude-sonnet-5")
    assert with_model[with_model.index("--model") + 1] == "claude-sonnet-5"


def test_mcp_config_enables_worker_mode_with_budget():
    config = claude_work.build_mcp_config("http://t", "key", 2)
    server = config["mcpServers"]["task-assistant"]
    assert server["env"]["TASK_MCP_WORKER_MODE"] == "1"
    assert server["env"]["TASK_MCP_CLAIM_BUDGET"] == "2"
    assert server["env"]["TASK_TRACKER_API_KEY"] == "key"
    assert server["args"][-1].endswith("task_assistant_mcp.py")


def test_worked_between_diffs_only_new_review_entries():
    before = [
        {"id": 1, "status": "in_progress"},
        {"id": 2, "status": "waiting_review"},
        {"id": 3, "status": "backlog"},
    ]
    after = [
        {"id": 1, "status": "waiting_review"},
        {"id": 2, "status": "waiting_review"},
        {"id": 3, "status": "done"},
    ]
    assert [t["id"] for t in claude_work.worked_between(before, after)] == [1]


def test_run_claude_work_skips_without_binary(monkeypatch, capsys):
    monkeypatch.setattr(claude_work, "claude_binary", lambda: None)
    assert claude_work.run_claude_work("http://t", "key") == []
    assert "claude CLI not found" in capsys.readouterr().err


def test_result_text_parses_json_and_falls_back():
    assert claude_work._result_text('{"result": "did things", "type": "result"}') == "did things"
    assert claude_work._result_text("plain text output") == "plain text output"
