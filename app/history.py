from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

from app.config import get_settings
from app.schemas import TaskRead

logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

HISTORY_HEADERS = [
    "event_at",
    "action",
    "task_id",
    "title",
    "status",
    "previous_status",
    "assignee",
    "previous_assignee",
    "priority",
    "previous_priority",
    "origin",
    "source_name",
    "source_url",
    "due_at",
    "previous_due_at",
    "reminder_at",
    "completed_at",
    "created_at",
    "updated_at",
    "description",
    "source_context",
    "note",
]

_history_lock = Lock()
_history_sheet_ready = False


def record_task_event(
    action: str,
    task: TaskRead,
    *,
    previous: TaskRead | None = None,
    note: str = "",
) -> None:
    settings = get_settings()
    if not settings.task_history_enabled:
        return

    try:
        session = _authorized_session()
        _ensure_history_sheet(session, settings.task_history_sheet_id, settings.task_history_sheet_tab)
        _append_history_row(
            session,
            settings.task_history_sheet_id,
            settings.task_history_sheet_tab,
            _history_row(action, task, previous=previous, note=note),
        )
    except Exception:
        logger.warning("Failed to write task history event to Google Sheets", exc_info=True)


def choose_update_action(task: TaskRead, previous: TaskRead | None) -> str:
    if previous is not None and previous.status != task.status and task.status == "done":
        return "completed"
    return "updated"


def _authorized_session() -> AuthorizedSession:
    credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
    return AuthorizedSession(credentials)


def _ensure_history_sheet(session: AuthorizedSession, spreadsheet_id: str, sheet_name: str) -> None:
    global _history_sheet_ready
    if _history_sheet_ready:
        return

    with _history_lock:
        if _history_sheet_ready:
            return

        metadata_url = f"{SHEETS_API_BASE}/{spreadsheet_id}?fields=sheets(properties(sheetId,title))"
        metadata_response = session.get(metadata_url, timeout=10)
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        sheets = metadata.get("sheets", [])
        sheet = next(
            (
                item["properties"]
                for item in sheets
                if item.get("properties", {}).get("title") == sheet_name
            ),
            None,
        )

        if sheet is None:
            create_response = session.post(
                f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
                json={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": sheet_name,
                                    "gridProperties": {
                                        "rowCount": 1000,
                                        "columnCount": len(HISTORY_HEADERS),
                                    },
                                }
                            }
                        }
                    ]
                },
                timeout=10,
            )
            create_response.raise_for_status()

        header_range = _range_path(sheet_name, f"A1:{_column_letter(len(HISTORY_HEADERS))}1")
        header_response = session.put(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{header_range}?valueInputOption=RAW",
            json={"values": [HISTORY_HEADERS]},
            timeout=10,
        )
        header_response.raise_for_status()
        _history_sheet_ready = True


def _append_history_row(
    session: AuthorizedSession,
    spreadsheet_id: str,
    sheet_name: str,
    row: list[str | int],
) -> None:
    append_range = _range_path(sheet_name, f"A:{_column_letter(len(HISTORY_HEADERS))}")
    response = session.post(
        f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{append_range}:append"
        "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
        json={"values": [row]},
        timeout=10,
    )
    response.raise_for_status()


def _history_row(
    action: str,
    task: TaskRead,
    *,
    previous: TaskRead | None = None,
    note: str = "",
) -> list[str | int]:
    current_data = task.model_dump(mode="json")
    previous_data = previous.model_dump(mode="json") if previous is not None else {}
    return [
        datetime.now(timezone.utc).isoformat(),
        action,
        task.id,
        _text(current_data.get("title")),
        _text(current_data.get("status")),
        _text(previous_data.get("status")),
        _text(current_data.get("assignee")),
        _text(previous_data.get("assignee")),
        task.priority,
        _text(previous_data.get("priority")),
        _text(current_data.get("origin")),
        _text(current_data.get("source_name")),
        _text(current_data.get("source_url")),
        _text(current_data.get("due_at")),
        _text(previous_data.get("due_at")),
        _text(current_data.get("reminder_at")),
        _text(current_data.get("completed_at")),
        _text(current_data.get("created_at")),
        _text(current_data.get("updated_at")),
        _text(current_data.get("description")),
        _text(current_data.get("source_context")),
        note,
    ]


def _range_path(sheet_name: str, cell_range: str) -> str:
    escaped_sheet_name = sheet_name.replace("'", "''")
    return quote(f"'{escaped_sheet_name}'!{cell_range}", safe="")


def _column_letter(column_number: int) -> str:
    letters = ""
    value = column_number
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _text(value: object) -> str:
    return "" if value is None else str(value)
