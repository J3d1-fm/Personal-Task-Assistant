import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)

# "testclient" is the fixed client host Starlette's in-process TestClient reports;
# it can never appear on a real network socket.
LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def is_loopback_request(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in LOOPBACK_CLIENT_HOSTS


def has_valid_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> bool:
    settings = get_settings()
    supplied = None
    if credentials and credentials.scheme.lower() == "bearer":
        supplied = credentials.credentials
    if supplied is None:
        supplied = request.headers.get("x-api-key")
    if not supplied or not settings.task_tracker_api_key:
        return False
    return secrets.compare_digest(supplied, settings.task_tracker_api_key)


def current_user(request: Request) -> dict[str, str] | None:
    user = request.session.get("user")
    if not isinstance(user, dict):
        return None
    email = user.get("email")
    if not isinstance(email, str):
        return None
    return user


def is_allowed_email(email: str) -> bool:
    allowed = get_settings().allowed_email_set
    return not allowed or email.lower() in allowed


def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    if not has_valid_api_key(request, credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
        )


def require_api_key_or_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    if has_valid_api_key(request, credentials):
        return
    user = current_user(request)
    if user and is_allowed_email(user["email"]):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Login with Google or provide a valid API key",
    )
