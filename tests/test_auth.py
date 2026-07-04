from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth import is_loopback_request
from app.main import app


def _request_from(host: str | None):
    client = None if host is None else SimpleNamespace(host=host)
    return SimpleNamespace(client=client)


def test_is_loopback_request():
    assert is_loopback_request(_request_from("127.0.0.1"))
    assert is_loopback_request(_request_from("::1"))
    assert is_loopback_request(_request_from("testclient"))
    assert not is_loopback_request(_request_from("10.0.0.5"))
    assert not is_loopback_request(_request_from("203.0.113.7"))
    assert not is_loopback_request(_request_from(None))


def test_local_dev_auth_serves_ui_for_loopback():
    with TestClient(app) as fresh_client:
        response = fresh_client.get("/")
        assert response.status_code == 200
        assert "Personal Task Assistant" in response.text

        me = fresh_client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["name"] == "Local Dev"


def test_logout_clears_session():
    with TestClient(app, follow_redirects=False) as fresh_client:
        fresh_client.get("/")
        response = fresh_client.post("/logout")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
