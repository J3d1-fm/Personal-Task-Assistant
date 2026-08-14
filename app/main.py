from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import current_user, is_allowed_email, is_loopback_request, require_api_key_or_user
from app.config import get_settings
from app.db import get_db
from app.history import choose_update_action, record_task_event
from app.ingestion_store import decision_should_process, get_ingestion_store
from app.migrate import run_migrations
from app.models import TaskAssignee, TaskStatus
from app.schemas import (
    AgentQueueRead,
    AgentQueueSummary,
    ContextIngest,
    ContextIngestResult,
    IngestionCheckRequest,
    IngestionCheckResult,
    IngestionCheckResultItem,
    IngestionDecisionRead,
    IngestionDecisionWrite,
    SourceReport,
    SourceStateRead,
    TaskCreate,
    TaskRead,
    TaskUpdate,
    TriageAction,
    TriageApplyRequest,
    TriageApplyResult,
    TriageApplyResultItem,
    TriageBatchRead,
    TriageCard,
    TriageResolution,
)
from app.store import DuplicateExternalIdError, get_task_store

settings = get_settings()
settings.validate_runtime_config()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not settings.use_firestore:
        run_migrations()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ok"}


def _auth_setup_response(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "auth_setup.html",
        {
            "app_name": settings.app_name,
            "redirect_uri": oauth_redirect_uri(request),
        },
        status_code=503,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    if settings.local_dev_auth_enabled and is_loopback_request(request):
        request.session["user"] = {
            "email": settings.local_dev_user_email,
            "name": "Local Dev",
            "picture": "",
        }
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "user": current_user(request),
            },
        )
    if not settings.google_oauth_enabled:
        return _auth_setup_response(request)
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "user": user,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    if settings.local_dev_auth_enabled and is_loopback_request(request):
        request.session["user"] = {
            "email": settings.local_dev_user_email,
            "name": "Local Dev",
            "picture": "",
        }
        return RedirectResponse(url="/")
    if not settings.google_oauth_enabled:
        return _auth_setup_response(request)
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
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[TaskRead]:
    tasks = get_task_store(db).list_tasks(status=status, assignee=assignee, include_done=include_done)
    if offset:
        tasks = tasks[offset:]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


@app.post("/api/tasks", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def create_task(payload: TaskCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> TaskRead:
    task = _create_task_or_conflict(get_task_store(db), payload)
    background_tasks.add_task(record_task_event, "created", task)
    return task


@app.get("/api/tasks/{task_id}", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def get_task(task_id: int, db: Session = Depends(get_db)) -> TaskRead:
    task = get_task_store(db).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.patch("/api/tasks/{task_id}", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def update_task(
    task_id: int,
    payload: TaskUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TaskRead:
    store = get_task_store(db)
    previous = store.get_task(task_id)
    try:
        task = store.update_task(task_id, payload)
    except DuplicateExternalIdError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    background_tasks.add_task(record_task_event, choose_update_action(task, previous), task, previous=previous)
    return task


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_api_key_or_user)])
def delete_task(task_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, bool]:
    store = get_task_store(db)
    previous = store.get_task(task_id)
    deleted = store.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    if previous is not None:
        background_tasks.add_task(record_task_event, "dismissed", previous)
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
    now = datetime.now(timezone.utc)
    tasks = get_task_store(db).list_tasks(include_done=include_done)
    selected_tasks = [
        task
        for task in tasks
        if (include_done or _is_active(task))
        and (status is None or task.status == status)
        and (assignee is None or task.assignee == assignee)
    ]
    return AgentQueueRead(
        summary=_agent_queue_summary(tasks, now=now),
        tasks=_sort_tasks_for_agent(selected_tasks, sort=sort, now=now)[:limit],
    )


@app.get(
    "/api/agent/queue/summary",
    response_model=AgentQueueSummary,
    dependencies=[Depends(require_api_key_or_user)],
)
def agent_queue_summary(db: Session = Depends(get_db)) -> AgentQueueSummary:
    return _agent_queue_summary(get_task_store(db).list_tasks(include_done=False))


@app.post("/api/agent/claim", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def claim_agent_task(background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> TaskRead:
    store = get_task_store(db)
    now = datetime.now(timezone.utc)
    candidates = [
        task
        for task in store.list_tasks(include_done=False)
        if task.assignee == TaskAssignee.codex and task.status == TaskStatus.backlog
    ]
    for candidate in _sort_tasks_for_agent(candidates, sort="smart", now=now):
        claimed = store.try_claim(candidate.id)
        if claimed is not None:
            background_tasks.add_task(record_task_event, "claimed", claimed, previous=candidate, note="agent_api")
            return claimed
    raise HTTPException(status_code=404, detail="No claimable task")


@app.post("/api/agent/tasks", response_model=TaskRead, dependencies=[Depends(require_api_key_or_user)])
def create_agent_task(
    payload: TaskCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TaskRead:
    task = _create_task_or_conflict(get_task_store(db), payload)
    background_tasks.add_task(record_task_event, "created", task, note="agent_api")
    return task


@app.post("/api/ingest/context", response_model=ContextIngestResult, dependencies=[Depends(require_api_key_or_user)])
@app.post(
    "/api/agent/ingest/context",
    response_model=ContextIngestResult,
    dependencies=[Depends(require_api_key_or_user)],
)
def ingest_context(
    payload: ContextIngest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ContextIngestResult:
    created: list[TaskRead] = []
    duplicates: list[TaskRead] = []
    store = get_task_store(db)
    for item in payload.tasks:
        data = item.model_dump()
        data["origin"] = payload.origin
        data["source_name"] = data.get("source_name") or payload.source_name
        data["source_url"] = data.get("source_url") or payload.source_url
        data["source_context"] = data.get("source_context") or payload.source_context
        create_payload = TaskCreate(**data)
        if create_payload.external_id:
            existing = store.get_task_by_external_id(create_payload.external_id)
            if existing is not None:
                duplicates.append(existing)
                continue
        try:
            task = store.create_task(create_payload)
        except DuplicateExternalIdError:
            existing = store.get_task_by_external_id(create_payload.external_id or "")
            if existing is not None:
                duplicates.append(existing)
            continue
        background_tasks.add_task(record_task_event, "created", task, note="context_ingest")
        created.append(task)
    return ContextIngestResult(created=created, duplicates=duplicates)


@app.get(
    "/api/sources",
    response_model=list[SourceStateRead],
    dependencies=[Depends(require_api_key_or_user)],
)
@app.get(
    "/api/agent/sources",
    response_model=list[SourceStateRead],
    dependencies=[Depends(require_api_key_or_user)],
)
def list_source_states(db: Session = Depends(get_db)) -> list[SourceStateRead]:
    return get_ingestion_store(db).list_source_states()


@app.post(
    "/api/agent/sources/report",
    response_model=SourceStateRead,
    dependencies=[Depends(require_api_key_or_user)],
)
def report_source_state(payload: SourceReport, db: Session = Depends(get_db)) -> SourceStateRead:
    return get_ingestion_store(db).report_source(payload)


@app.post(
    "/api/agent/ingestion/check",
    response_model=IngestionCheckResult,
    dependencies=[Depends(require_api_key_or_user)],
)
def check_ingestion_items(payload: IngestionCheckRequest, db: Session = Depends(get_db)) -> IngestionCheckResult:
    store = get_ingestion_store(db)
    decisions = store.get_decisions(payload.source_id, payload.items)
    results = []
    for item in payload.items:
        decision = decisions.get((item.source_item_id, item.content_fingerprint))
        results.append(
            IngestionCheckResultItem(
                source_item_id=item.source_item_id,
                content_fingerprint=item.content_fingerprint,
                should_process=decision_should_process(decision),
                existing_decision=decision,
            )
        )
    return IngestionCheckResult(source_id=payload.source_id, items=results)


@app.post(
    "/api/agent/ingestion/decisions",
    response_model=IngestionDecisionRead,
    dependencies=[Depends(require_api_key_or_user)],
)
def record_ingestion_decision(
    payload: IngestionDecisionWrite,
    db: Session = Depends(get_db),
) -> IngestionDecisionRead:
    return get_ingestion_store(db).record_decision(payload)


@app.get("/api/reminders/due", response_model=list[TaskRead], dependencies=[Depends(require_api_key_or_user)])
def due_reminders(db: Session = Depends(get_db), now: datetime | None = None) -> list[TaskRead]:
    return get_task_store(db).due_reminders(now=now)


@app.post(
    "/api/agent/triage/next",
    response_model=TriageBatchRead,
    dependencies=[Depends(require_api_key_or_user)],
)
def triage_next(
    db: Session = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=50),
    cooldown_hours: float = Query(default=24.0, ge=0, le=24 * 14),
    mark: bool = Query(default=True),
) -> TriageBatchRead:
    now = datetime.now(timezone.utc)
    store = get_task_store(db)
    tasks = store.list_tasks(include_done=False)
    active = [task for task in tasks if _is_active(task)]
    cutoff = now - timedelta(hours=cooldown_hours)
    eligible: list[TaskRead] = []
    skipped = 0
    for task in _sort_tasks_for_agent(active, sort="smart", now=now):
        shown = _normalized_datetime(task.last_shown_at)
        if cooldown_hours > 0 and shown is not None and shown > cutoff:
            skipped += 1
            continue
        eligible.append(task)
    batch = eligible[:limit]
    if mark and batch:
        store.mark_shown([task.id for task in batch], now=now)
    return TriageBatchRead(
        summary=_agent_queue_summary(tasks, now=now),
        cards=[_triage_card(task, now=now, shown_at=now if mark else None) for task in batch],
        skipped_recently_shown=skipped,
        cooldown_hours=cooldown_hours,
    )


@app.post(
    "/api/agent/triage/apply",
    response_model=TriageApplyResult,
    dependencies=[Depends(require_api_key_or_user)],
)
def triage_apply(
    payload: TriageApplyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TriageApplyResult:
    store = get_task_store(db)
    now = datetime.now(timezone.utc)
    results: list[TriageApplyResultItem] = []
    for resolution in payload.resolutions:
        previous = store.get_task(resolution.id)
        if previous is None:
            results.append(_triage_failure(resolution, "Task not found"))
            continue
        try:
            update_payload = _resolution_to_update(resolution, previous, now=now)
        except ValueError as exc:
            results.append(_triage_failure(resolution, str(exc)))
            continue
        try:
            task = store.update_task(resolution.id, update_payload)
        except DuplicateExternalIdError as exc:
            results.append(_triage_failure(resolution, str(exc)))
            continue
        if task is None:
            results.append(_triage_failure(resolution, "Task not found"))
            continue
        background_tasks.add_task(
            record_task_event,
            choose_update_action(task, previous),
            task,
            previous=previous,
            note=f"triage:{resolution.action}",
        )
        results.append(
            TriageApplyResultItem(id=resolution.id, action=resolution.action, ok=True, task=task)
        )
    applied = sum(1 for item in results if item.ok)
    return TriageApplyResult(
        applied=applied,
        failed=len(results) - applied,
        results=results,
        summary=_agent_queue_summary(store.list_tasks(include_done=False), now=now),
    )


def _create_task_or_conflict(store, payload: TaskCreate) -> TaskRead:
    try:
        return store.create_task(payload)
    except DuplicateExternalIdError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


_TRIAGE_STATUS_BY_ACTION = {
    TriageAction.done: TaskStatus.done,
    TriageAction.cancel: TaskStatus.cancelled,
    TriageAction.block: TaskStatus.blocked,
}

_TRIAGE_SNIPPET_LIMIT = 280


def _triage_card(task: TaskRead, *, now: datetime, shown_at: datetime | None = None) -> TriageCard:
    created = _normalized_datetime(task.created_at) or now
    snippet = None
    if task.description:
        flattened = " ".join(task.description.split())
        snippet = flattened[:_TRIAGE_SNIPPET_LIMIT] + ("…" if len(flattened) > _TRIAGE_SNIPPET_LIMIT else "")
    return TriageCard(
        id=task.id,
        title=task.title,
        status=task.status,
        assignee=task.assignee,
        origin=task.origin,
        priority=task.priority,
        source_name=task.source_name,
        source_url=task.source_url,
        due_at=task.due_at,
        reminder_at=task.reminder_at,
        age_days=max(0, (now - created).days),
        overdue=_is_overdue(task, now),
        last_shown_at=shown_at or task.last_shown_at,
        description_snippet=snippet,
    )


def _triage_failure(resolution: TriageResolution, error: str) -> TriageApplyResultItem:
    return TriageApplyResultItem(id=resolution.id, action=resolution.action, ok=False, error=error)


def _resolution_to_update(resolution: TriageResolution, previous: TaskRead, *, now: datetime) -> TaskUpdate:
    updates: dict = {}
    forced_status = _TRIAGE_STATUS_BY_ACTION.get(resolution.action)
    if forced_status is not None:
        updates["status"] = forced_status
    if resolution.action == TriageAction.defer and resolution.due_at is None and resolution.reminder_at is None:
        raise ValueError("defer requires due_at or reminder_at")
    if resolution.action == TriageAction.assign and resolution.assignee is None:
        raise ValueError("assign requires assignee")
    for field in ("status", "assignee", "priority", "due_at", "reminder_at"):
        value = getattr(resolution, field)
        if value is not None:
            updates.setdefault(field, value)
    if resolution.note:
        stamp = now.strftime("%Y-%m-%d %H:%M")
        note_line = f"— {stamp} (triage): {resolution.note}"
        description = (previous.description or "").rstrip()
        updates["description"] = f"{description}\n\n{note_line}" if description else note_line
    if not updates:
        raise ValueError("update requires at least one field or a note")
    return TaskUpdate(**updates)


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


def _agent_queue_summary(tasks: list[TaskRead], now: datetime | None = None) -> AgentQueueSummary:
    current = now or datetime.now(timezone.utc)
    active = [task for task in tasks if _is_active(task)]
    return AgentQueueSummary(
        active=len(active),
        overdue=sum(1 for task in active if _is_overdue(task, current)),
        due_soon=sum(1 for task in active if _is_due_soon(task, current)),
        codex_ready=sum(1 for task in active if _is_codex_ready(task)),
        human_input=sum(1 for task in active if _needs_human_input(task)),
        review=sum(1 for task in active if task.status == TaskStatus.waiting_review),
        blocked=sum(1 for task in active if task.status == TaskStatus.blocked),
        unassigned=sum(1 for task in active if task.assignee == TaskAssignee.unassigned),
    )


def _task_rank(task: TaskRead, now: datetime) -> int:
    rank = task.priority * 100
    if _is_overdue(task, now):
        rank -= 70
    if _is_due_soon(task, now):
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


def _sort_tasks_for_agent(tasks: list[TaskRead], *, sort: str, now: datetime | None = None) -> list[TaskRead]:
    current = now or datetime.now(timezone.utc)

    def due_key(task: TaskRead) -> tuple:
        return (_due_sort_value(task), task.priority, _timestamp_desc(task.updated_at))

    def created_key(task: TaskRead) -> tuple:
        return (_timestamp_desc(task.created_at), task.priority)

    def updated_key(task: TaskRead) -> tuple:
        return (_timestamp_desc(task.updated_at), task.priority)

    def priority_key(task: TaskRead) -> tuple:
        return (task.priority, _due_sort_value(task), _timestamp_desc(task.updated_at))

    def owner_key(task: TaskRead) -> tuple:
        return (str(task.assignee), _due_sort_value(task), task.priority)

    def smart_key(task: TaskRead) -> tuple:
        return (_task_rank(task, current), _due_sort_value(task), _timestamp_desc(task.updated_at))

    keys = {
        "due": due_key,
        "created": created_key,
        "updated": updated_key,
        "priority": priority_key,
        "owner": owner_key,
    }
    return sorted(tasks, key=keys.get(sort, smart_key))
