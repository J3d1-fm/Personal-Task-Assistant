from datetime import datetime, timedelta, timezone

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from app.auth import current_user, is_allowed_email, require_api_key_or_user
from app.config import get_settings
from app.db import Base, engine, get_db
from app.history import choose_update_action, record_task_event
from app.models import TaskAssignee, TaskStatus
from app.schemas import (
    AgentQueueRead,
    AgentQueueSummary,
    ContextIngest,
    ContextIngestResult,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.store import get_task_store


settings = get_settings()
settings.validate_runtime_config()
app = FastAPI(title=settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=settings.session_cookie_https,
    max_age=60 * 60 * 24 * 30,
)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

oauth = OAuth()
if settings.google_oauth_enabled:
    oauth.register(
        name="google",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@app.on_event("startup")
def startup() -> None:
    if not settings.use_firestore:
        Base.metadata.create_all(bind=engine)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    if settings.local_dev_auth_enabled:
        request.session["user"] = {
            "email": settings.local_dev_user_email,
            "name": "Local Dev",
            "picture": "",
        }
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "user": current_user(request),
            },
        )
    if not settings.google_oauth_enabled:
        return templates.TemplateResponse(
            "auth_setup.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "redirect_uri": oauth_redirect_uri(request),
            },
            status_code=503,
        )
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "user": user,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    if settings.local_dev_auth_enabled:
        request.session["user"] = {
            "email": settings.local_dev_user_email,
            "name": "Local Dev",
            "picture": "",
        }
        return RedirectResponse(url="/")
    if not settings.google_oauth_enabled:
        return templates.TemplateResponse(
            "auth_setup.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "redirect_uri": oauth_redirect_uri(request),
            },
            status_code=503,
        )
    return await oauth.google.authorize_redirect(request, oauth_redirect_uri(request))


@app.get("/auth/google/callback")
async def google_auth_callback(request: Request) -> RedirectResponse:
    if not settings.google_oauth_enabled:
        return RedirectResponse(url="/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(url="/login")
    userinfo = token.get("userinfo")
    if userinfo is None:
        userinfo = await oauth.google.userinfo(token=token)
    email = str(userinfo.get("email", "")).lower()
    if not email or not is_allowed_email(email):
        request.session.clear()
        return RedirectResponse(url="/login?denied=1")
    request.session["user"] = {
        "email": email,
        "name": str(userinfo.get("name") or email),
        "picture": str(userinfo.get("picture") or ""),
    }
    return RedirectResponse(url="/")


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


def oauth_redirect_uri(request: Request) -> str:
    if settings.normalized_public_base_url:
        return f"{settings.normalized_public_base_url}/auth/google/callback"
    return str(request.url_for("google_auth_callback"))


@app.get("/api/me", dependencies=[Depends(require_api_key_or_user)])
def me(request: Request) -> dict[str, str] | None:
    return current_user(request)


@app.get("/api/tasks", response_model=list[TaskRead], dependencies=[Depends(require_api_key_or_user)])
def list_tasks(
    db: Session = Depends(get_db),
    status: TaskStatus | None = None,
    assignee: str | None = None,
    include_done: bool = Query(default=False),
) -> list[TaskRead]:
    return get_task_store(db).list_tasks(status=status, assignee=assignee, include_done=include_done)


@app.post("/api/tasks", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> TaskRead:
    task = get_task_store(db).create_task(payload)
    record_task_event("created", task)
    return task


@app.patch("/api/tasks/{task_id}", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def update_task(task_id: int, payload: TaskUpdate, db: Session = Depends(get_db)) -> TaskRead:
    store = get_task_store(db)
    previous = store.get_task(task_id)
    task = store.update_task(task_id, payload)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    record_task_event(choose_update_action(task, previous), task, previous=previous)
    return task


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_api_key_or_user)])
def delete_task(task_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    store = get_task_store(db)
    previous = store.get_task(task_id)
    deleted = store.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    if previous is not None:
        record_task_event("dismissed", previous)
    return {"deleted": True}


@app.get("/api/agent/queue", response_model=AgentQueueRead, dependencies=[Depends(require_api_key_or_user)])
def agent_queue(
    db: Session = Depends(get_db),
    status: TaskStatus | None = None,
    assignee: TaskAssignee | None = None,
    include_done: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=250),
    sort: str = Query(default="smart", pattern="^(smart|due|created|updated|priority|owner)$"),
) -> AgentQueueRead:
    all_tasks = get_task_store(db).list_tasks(include_done=True)
    selected_tasks = [
        task
        for task in all_tasks
        if (include_done or _is_active(task))
        and (status is None or task.status == status)
        and (assignee is None or task.assignee == assignee)
    ]
    return AgentQueueRead(
        summary=_agent_queue_summary(all_tasks),
        tasks=_sort_tasks_for_agent(selected_tasks, sort=sort)[:limit],
    )


@app.post("/api/agent/tasks", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def create_agent_task(payload: TaskCreate, db: Session = Depends(get_db)) -> TaskRead:
    task = get_task_store(db).create_task(payload)
    record_task_event("created", task, note="agent_api")
    return task


@app.post("/api/ingest/context", response_model=ContextIngestResult, dependencies=[Depends(require_api_key_or_user)])
@app.post("/api/agent/ingest/context", response_model=ContextIngestResult, dependencies=[Depends(require_api_key_or_user)])
def ingest_context(payload: ContextIngest, db: Session = Depends(get_db)) -> ContextIngestResult:
    created: list[TaskRead] = []
    store = get_task_store(db)
    for item in payload.tasks:
        data = item.model_dump()
        data["origin"] = payload.origin
        data["source_name"] = data.get("source_name") or payload.source_name
        data["source_url"] = data.get("source_url") or payload.source_url
        data["source_context"] = data.get("source_context") or payload.source_context
        task = store.create_task(TaskCreate(**data))
        record_task_event("created", task, note="context_ingest")
        created.append(task)
    return ContextIngestResult(created=created)


@app.get("/api/reminders/due", response_model=list[TaskRead], dependencies=[Depends(require_api_key_or_user)])
def due_reminders(db: Session = Depends(get_db), now: datetime | None = None) -> list[TaskRead]:
    return get_task_store(db).due_reminders(now=now)


def _is_active(task: TaskRead) -> bool:
    return task.status not in {TaskStatus.done, TaskStatus.cancelled}


def _normalized_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _time_targets(task: TaskRead) -> list[datetime]:
    return [
        normalized
        for normalized in (_normalized_datetime(task.due_at), _normalized_datetime(task.reminder_at))
        if normalized is not None
    ]


def _is_overdue(task: TaskRead, now: datetime | None = None) -> bool:
    if not _is_active(task):
        return False
    current = now or datetime.now(timezone.utc)
    return any(target < current for target in _time_targets(task))


def _is_due_soon(task: TaskRead, now: datetime | None = None) -> bool:
    if not _is_active(task):
        return False
    current = now or datetime.now(timezone.utc)
    soon = current + timedelta(days=1)
    return any(current <= target <= soon for target in _time_targets(task))


def _is_codex_ready(task: TaskRead) -> bool:
    return (
        _is_active(task)
        and task.assignee == TaskAssignee.codex
        and task.status in {TaskStatus.backlog, TaskStatus.in_progress}
    )


def _needs_human_input(task: TaskRead) -> bool:
    return _is_active(task) and (
        task.assignee == TaskAssignee.me
        or task.status in {TaskStatus.waiting_review, TaskStatus.blocked}
    )


def _agent_queue_summary(tasks: list[TaskRead]) -> AgentQueueSummary:
    now = datetime.now(timezone.utc)
    active = [task for task in tasks if _is_active(task)]
    return AgentQueueSummary(
        active=len(active),
        overdue=sum(1 for task in active if _is_overdue(task, now)),
        due_soon=sum(1 for task in active if _is_due_soon(task, now)),
        codex_ready=sum(1 for task in active if _is_codex_ready(task)),
        human_input=sum(1 for task in active if _needs_human_input(task)),
        review=sum(1 for task in active if task.status == TaskStatus.waiting_review),
        blocked=sum(1 for task in active if task.status == TaskStatus.blocked),
        unassigned=sum(1 for task in active if task.assignee == TaskAssignee.unassigned),
    )


def _task_rank(task: TaskRead) -> int:
    rank = task.priority * 100
    if _is_overdue(task):
        rank -= 70
    if _is_due_soon(task):
        rank -= 30
    if _is_codex_ready(task):
        rank -= 18
    if task.status == TaskStatus.waiting_review:
        rank -= 12
    if task.status == TaskStatus.blocked:
        rank += 15
    if not _is_active(task):
        rank += 200
    return rank


def _due_sort_value(task: TaskRead) -> datetime:
    targets = _time_targets(task)
    if targets:
        return min(targets)
    return datetime.max.replace(tzinfo=timezone.utc)


def _timestamp_desc(value: datetime | None) -> float:
    normalized = _normalized_datetime(value)
    if normalized is None:
        return 0
    return -normalized.timestamp()


def _sort_tasks_for_agent(tasks: list[TaskRead], *, sort: str) -> list[TaskRead]:
    if sort == "due":
        key = lambda task: (_due_sort_value(task), task.priority, _timestamp_desc(task.updated_at))
    elif sort == "created":
        key = lambda task: (_timestamp_desc(task.created_at), task.priority)
    elif sort == "updated":
        key = lambda task: (_timestamp_desc(task.updated_at), task.priority)
    elif sort == "priority":
        key = lambda task: (task.priority, _due_sort_value(task), _timestamp_desc(task.updated_at))
    elif sort == "owner":
        key = lambda task: (str(task.assignee), _due_sort_value(task), task.priority)
    else:
        key = lambda task: (_task_rank(task), _due_sort_value(task), _timestamp_desc(task.updated_at))
    return sorted(tasks, key=key)
