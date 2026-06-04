#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
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
    print("Personal Task Assistant Local Mode")
    print("=" * 34)
    if not venv_python().exists() or not ENV_FILE.exists():
        print("Local Mode is not installed yet. Running setup first.")
        return run_setup_and_start()
    if local_app_available():
        print("Local server is already running.")
        open_browser()
        return 0
    if port_in_use():
        print("Port 8000 is already in use by another local app.")
        print("Stop that app first, or start Personal Task Assistant manually on another port:")
        print("  .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001")
        return 1
    run_doctor()
    return run_server()


def run_setup_and_start() -> int:
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "setup_wizard.py"), "--yes", "--start"], cwd=ROOT)


def run_doctor() -> None:
    subprocess.run([str(venv_python()), str(ROOT / "scripts" / "local_doctor.py")], cwd=ROOT, check=True)


def run_server() -> int:
    command = [str(venv_python()), "-m", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000"]
    print("\nStarting local server on http://127.0.0.1:8000.")
    print("Press Ctrl+C in this window to stop it.")
    process = subprocess.Popen(command, cwd=ROOT)
    try:
        wait_for_readyz()
        open_browser()
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
    deadline = time.time() + 30
    while time.time() < deadline:
        if local_app_available():
            return
        time.sleep(0.5)
    print("Server is starting slowly. Open http://127.0.0.1:8000 manually if the browser does not open.")


def readyz_available() -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/readyz", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def local_app_available() -> bool:
    if not readyz_available():
        return False
    api_key = read_env().get("TASK_TRACKER_API_KEY", "")
    if not api_key or api_key == "change-me":
        return False
    request = urllib.request.Request(
        f"{APP_URL}/api/tasks?include_done=false",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", 8000)) == 0


def open_browser() -> None:
    try:
        webbrowser.open(APP_URL)
    except Exception:
        print(f"Open {APP_URL} in your browser.")


def read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


if __name__ == "__main__":
    raise SystemExit(main())
