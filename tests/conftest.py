import os
from pathlib import Path

TEST_DB_PATH = Path(__file__).resolve().parent / "test_task_tracker.db"

os.environ["TASK_STORE"] = "sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["TASK_TRACKER_API_KEY"] = "test-api-key"
os.environ["SESSION_SECRET_KEY"] = "unit-test-session-secret-key-0123456789"
os.environ["SESSION_COOKIE_HTTPS"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = ""
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = ""
os.environ["ALLOWED_GOOGLE_EMAILS"] = ""
os.environ["TASK_HISTORY_SHEET_ID"] = ""

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

API_HEADERS = {"Authorization": "Bearer test-api-key"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_tasks(client):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ingestion_decisions"))
        connection.execute(text("DELETE FROM source_states"))
        connection.execute(text("DELETE FROM tasks"))
    yield


@pytest.fixture
def api_headers():
    return dict(API_HEADERS)


@pytest.fixture
def make_task(client, api_headers):
    def _make_task(**overrides):
        payload = {"title": "Test task", **overrides}
        response = client.post("/api/tasks", json=payload, headers=api_headers)
        assert response.status_code == 200, response.text
        return response.json()

    return _make_task
