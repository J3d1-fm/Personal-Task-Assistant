#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
VENV_DIR = ROOT / ".venv"
DEFAULT_APP_URL = "http://127.0.0.1:8000"


def main() -> int:
    args = parse_args()
    checks = [
        check_python_version(),
        check_virtualenv(),
        check_env_file(),
        check_local_env_values(),
        check_sqlite_path(),
        check_imports(),
    ]
    if args.require_server:
        checks.append(check_readyz(args.url))
    else:
        checks.append(check_readyz(args.url, optional=True))

    print("\nLocal Mode doctor")
    print("=" * 17)
    for check in checks:
        marker = "OK" if check.ok else ("WARN" if check.warning else "FAIL")
        print(f"[{marker}] {check.message}")

    failed = [check for check in checks if not check.ok and not check.warning]
    if failed:
        print("\nLocal Mode is not ready. Fix the FAIL items above and run this again.")
        return 1

    print("\nLocal Mode is ready.")
    print(f"App URL: {args.url}")
    print("Storage: local SQLite file")
    print("Cloud: not required")
    return 0


class Check:
    def __init__(self, ok: bool, message: str, *, warning: bool = False) -> None:
        self.ok = ok
        self.message = message
        self.warning = warning


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Personal Task Assistant Local Mode.")
    parser.add_argument("--require-server", action="store_true", help="fail if the local server is not running")
    parser.add_argument("--url", default=os.getenv("LOCAL_APP_URL", DEFAULT_APP_URL), help="local app URL to check")
    return parser.parse_args()


def check_python_version() -> Check:
    version = sys.version_info
    ok = version >= (3, 10)
    if ok:
        return Check(True, f"Python {version.major}.{version.minor}.{version.micro} detected and meets Python 3.10+")
    return Check(False, f"Python {version.major}.{version.minor}.{version.micro} detected; Python 3.10+ is required")


def check_virtualenv() -> Check:
    python_path = venv_python()
    return Check(python_path.exists(), f"Virtual environment Python: {python_path}")


def check_env_file() -> Check:
    return Check(ENV_FILE.exists(), f"Local .env file: {ENV_FILE}")


def check_local_env_values() -> Check:
    env = read_env()
    if not env:
        return Check(False, ".env is missing or empty")

    blockers = []
    if env.get("TASK_STORE", "").lower() != "sqlite":
        blockers.append("TASK_STORE must be sqlite for Local Mode")
    if not env.get("DATABASE_URL", "").startswith("sqlite:///"):
        blockers.append("DATABASE_URL must use sqlite:///")
    if env.get("SESSION_COOKIE_HTTPS", "").lower() not in {"false", "0", "no"}:
        blockers.append("SESSION_COOKIE_HTTPS must be false for local http")
    if env.get("GOOGLE_OAUTH_CLIENT_ID") or env.get("GOOGLE_OAUTH_CLIENT_SECRET"):
        blockers.append("Google OAuth values are set; Local Mode expects local auth")
    if env.get("TASK_TRACKER_API_KEY", "") in {"", "change-me"}:
        blockers.append("TASK_TRACKER_API_KEY must be generated")
    if env.get("SESSION_SECRET_KEY", "") in {"", "change-this-long-random-string"}:
        blockers.append("SESSION_SECRET_KEY must be generated")

    if blockers:
        return Check(False, "; ".join(blockers))
    return Check(True, "Local .env uses SQLite, local auth, and generated secrets")


def check_sqlite_path() -> Check:
    env = read_env()
    database_url = env.get("DATABASE_URL", "sqlite:///./task_tracker.db")
    db_path = sqlite_path_from_url(database_url)
    if db_path is None:
        return Check(False, f"Unsupported Local Mode database URL: {database_url}")
    if not db_path.exists():
        return Check(True, f"SQLite file will be created on first app start: {db_path}", warning=True)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=rw", uri=True) as connection:
            connection.execute("select 1")
    except sqlite3.Error as exc:
        return Check(False, f"SQLite check failed for {db_path}: {exc}")
    return Check(True, f"SQLite file is usable: {db_path}")


def check_imports() -> Check:
    python_path = venv_python()
    if not python_path.exists():
        return Check(False, "Python dependencies cannot be checked before .venv exists")
    command = [
        str(python_path),
        "-c",
        "import app.main",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return Check(False, f"App startup imports are not available in .venv: {detail}")
    return Check(True, "App startup imports are available in .venv")


def check_readyz(app_url: str, *, optional: bool = False) -> Check:
    try:
        with urllib.request.urlopen(f"{app_url.rstrip('/')}/readyz", timeout=2) as response:
            if response.status != 200:
                return Check(False, f"Local server /readyz returned HTTP {response.status}")
    except Exception:
        if optional:
            return Check(False, "Local server is not running yet", warning=True)
        return Check(False, "Local server is not responding")

    api_key = read_env().get("TASK_TRACKER_API_KEY", "")
    if not api_key or api_key == "change-me":
        return Check(False, "Local server /readyz responds, but TASK_TRACKER_API_KEY is missing", warning=optional)
    request = urllib.request.Request(
        f"{app_url.rstrip('/')}/api/tasks?include_done=false",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status == 200:
                return Check(True, "Local server /readyz and API auth are responding")
            return Check(False, f"Local server API returned HTTP {response.status}", warning=optional)
    except Exception:
        return Check(
            False,
            "A service responded on /readyz, but it did not accept this Local Mode API key",
            warning=optional,
        )


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


def sqlite_path_from_url(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url[len(prefix) :]
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    return path


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


if __name__ == "__main__":
    raise SystemExit(main())
