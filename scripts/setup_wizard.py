#!/usr/bin/env python3
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
VENV_DIR = ROOT / ".venv"
APP_URL = "http://127.0.0.1:8000"


def main() -> int:
    print_header()
    if not confirm("Set up a local Personal Task Assistant workspace now?", default=True):
        print("\nNo changes made.")
        return 0

    create_virtualenv()
    install_dependencies()
    write_env_file()

    print("\nSetup complete.")
    print(f"Local URL: {APP_URL}")
    print("The local setup uses SQLite and local development auth.")

    if confirm("Start the app now and open it in your browser?", default=True):
        return run_server()

    print("\nTo start later, run:")
    print("  .venv/bin/uvicorn app.main:app --reload")
    return 0


def print_header() -> None:
    print("Personal Task Assistant setup")
    print("=" * 30)
    print("This wizard creates a local development setup.")
    print("It does not ask for Jira, Asana, YouTrack, Slack, Telegram, or email credentials.")
    print("Those connectors should be added later through user-built adapters.")
    print()


def confirm(question: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{question} [{suffix}] ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def create_virtualenv() -> None:
    if venv_python().exists():
        print("\nVirtual environment already exists.", flush=True)
        return
    print("\nCreating virtual environment...", flush=True)
    run([sys.executable, "-m", "venv", str(VENV_DIR)])


def install_dependencies() -> None:
    print("\nInstalling Python dependencies...", flush=True)
    run([str(venv_python()), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])


def write_env_file() -> None:
    if ENV_FILE.exists():
        print("\n.env already exists; keeping it unchanged.")
        return

    api_key = secrets.token_urlsafe(32)
    session_secret = secrets.token_urlsafe(48)
    env_text = "\n".join(
        [
            "DATABASE_URL=sqlite:///./task_tracker.db",
            f"TASK_TRACKER_API_KEY={api_key}",
            "APP_NAME=Personal Task Assistant",
            f"PUBLIC_BASE_URL={APP_URL}",
            "TASK_STORE=sqlite",
            f"SESSION_SECRET_KEY={session_secret}",
            "SESSION_COOKIE_HTTPS=false",
            "GOOGLE_OAUTH_CLIENT_ID=",
            "GOOGLE_OAUTH_CLIENT_SECRET=",
            "ALLOWED_GOOGLE_EMAILS=local@example.com",
            "TASK_HISTORY_SHEET_ID=",
            "TASK_HISTORY_SHEET_TAB=Task History",
            "",
        ]
    )
    ENV_FILE.write_text(env_text, encoding="utf-8")
    print("\nCreated .env with local-only generated secrets.")


def run_server() -> int:
    command = [str(venv_python()), "-m", "uvicorn", "app.main:app", "--reload"]
    print("\nStarting local server. Press Ctrl+C in this window to stop it.")
    process = subprocess.Popen(command, cwd=ROOT)
    try:
        wait_for_readyz()
        webbrowser.open(APP_URL)
        process.wait()
    except KeyboardInterrupt:
        print("\nStopping server...")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.returncode or 0


def wait_for_readyz() -> None:
    ready_url = f"{APP_URL}/readyz"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(ready_url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    print("Server is starting slowly. Open the URL manually if the browser does not open.")


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
