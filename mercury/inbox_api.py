"""Local dashboard API for the unified inbox (see mercury/control/inbox.py).

Reads return the service's JSON as is. Commands return ``{"success": true,
...}``; a refused command returns ``{"success": false, "message", "code",
...details}`` with an HTTP status from its error class (400 invalid, 403
compose_refused, 404 not_found, 409 stale_revision / draft_exists /
not_editable, 503 provider_failed). An ``Idempotency-Key`` header makes a
retried command return the first answer instead of running again.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mercury.control.errors import ControlError
from mercury.control.inbox import InboxService

router = APIRouter()
LIST_FILTERS = ("q", "mailbox", "intent", "stage", "prospect_status", "status", "read",
                "attention", "needs_you", "response", "snoozed", "reminder")


async def service(request: Request | None = None) -> InboxService:
    from mercury.dashboard import _ctx, _demo_config, _mail_context, _state

    try:
        _config, pool = _mail_context()
    except Exception:
        pool = None
    return await InboxService(_ctx(request), _state(), _demo_config(), pool).ready()


def _error(error: ControlError) -> JSONResponse:
    from mercury.dashboard import _control_error

    return _control_error(error, detail=True)


def _unexpected(error: Exception) -> JSONResponse:
    from mercury.control.audit import redact_text

    return JSONResponse({"success": False, "message": redact_text(error)}, status_code=500)


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _bad_body() -> JSONResponse:
    return JSONResponse({"success": False, "code": "invalid", "message": "Invalid request body."},
                        status_code=400)


async def _read(command):
    try:
        return await command
    except ControlError as e:
        return _error(e)


async def _command(command):
    try:
        result = await command
    except ControlError as e:
        return _error(e)
    except Exception as e:
        return _unexpected(e)
    return {"success": True, **result}


# ── Reads ──

@router.get("/api/inbox/conversations")
async def list_conversations(request: Request, limit: int = 50, offset: int = 0):
    """Filters repeat or take commas: ?mailbox=a@x.com&mailbox=b@y.com."""
    params = request.query_params
    filters = {name: params.getlist(name) if name in ("mailbox", "intent", "stage",
                                                      "prospect_status", "status")
               else params.get(name)
               for name in LIST_FILTERS if name in params}
    return await _read((await service(request)).list(filters, limit, offset))


@router.get("/api/inbox/conversations/{conversation_id}")
async def get_thread(conversation_id: str, request: Request):
    return await _read((await service(request)).thread(conversation_id))


@router.get("/api/inbox/reminders")
async def list_reminders(request: Request, due: bool = False, include_done: bool = False):
    return await _read((await service(request)).reminders(due=due, include_done=include_done))


@router.get("/api/inbox/contacts/{prospect_id}/notes")
async def list_notes(prospect_id: str, request: Request):
    return await _read((await service(request)).notes(prospect_id))


# ── Local state ──

@router.post("/api/inbox/conversations/{conversation_id}/read")
async def mark_read(conversation_id: str, request: Request):
    return await _command((await service(request)).mark(conversation_id, True))


@router.post("/api/inbox/conversations/{conversation_id}/unread")
async def mark_unread(conversation_id: str, request: Request):
    return await _command((await service(request)).mark(conversation_id, False))


@router.post("/api/inbox/conversations/{conversation_id}/snooze")
async def snooze(conversation_id: str, request: Request):
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).snooze(conversation_id, body.get("until")))


@router.delete("/api/inbox/conversations/{conversation_id}/snooze")
async def unsnooze(conversation_id: str, request: Request):
    return await _command((await service(request)).unsnooze(conversation_id))


@router.post("/api/inbox/conversations/{conversation_id}/stage")
async def set_stage(conversation_id: str, request: Request):
    """{"stage": one of the sales stages}. Audited; does not touch mail."""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).set_stage(conversation_id, body.get("stage")))


@router.post("/api/inbox/contacts/{prospect_id}/notes")
async def add_note(prospect_id: str, request: Request):
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).add_note(prospect_id, body.get("body")))


@router.patch("/api/inbox/notes/{note_id}")
async def edit_note(note_id: str, request: Request):
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).edit_note(note_id, body.get("body")))


@router.delete("/api/inbox/notes/{note_id}")
async def delete_note(note_id: str, request: Request):
    return await _command((await service(request)).delete_note(note_id))


@router.post("/api/inbox/conversations/{conversation_id}/reminders")
async def add_reminder(conversation_id: str, request: Request):
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).add_reminder(
        conversation_id, body.get("due_at"), body.get("note", "")))


@router.post("/api/inbox/reminders/{reminder_id}/done")
async def complete_reminder(reminder_id: str, request: Request):
    return await _command((await service(request)).complete_reminder(reminder_id))


@router.delete("/api/inbox/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str, request: Request):
    return await _command((await service(request)).delete_reminder(reminder_id))


@router.post("/api/inbox/bulk")
async def bulk(request: Request):
    """{"action": "read|unread|snooze|unsnooze|exclude", "conversation_ids": [...],
    "until"?: ISO (snooze), "kind"?: "email|domain", "reason"?: str,
    "confirm"?: true (required for exclude)}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).bulk(
        body.get("action"), body.get("conversation_ids"), until=body.get("until"),
        kind=body.get("kind") or "email", reason=str(body.get("reason") or "")[:300],
        confirm=body.get("confirm") is True))


# ── Compose (through the outbox) ──

@router.post("/api/inbox/conversations/{conversation_id}/drafts")
async def create_draft(conversation_id: str, request: Request):
    """{"body"?, "subject"?, "reply_to"?: inbound id, "generate"?: bool, "instruction"?}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).create_draft(
        conversation_id, body=str(body.get("body") or ""), subject=str(body.get("subject") or ""),
        reply_to=str(body.get("reply_to") or ""), generate=body.get("generate") is True,
        instruction=str(body.get("instruction") or "")))


@router.put("/api/inbox/conversations/{conversation_id}/drafts/{item_id}")
async def edit_draft(conversation_id: str, item_id: str, request: Request):
    """{"subject", "body", "revision"}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).edit_draft(
        conversation_id, item_id, str(body.get("subject") or ""), str(body.get("body") or ""),
        body.get("revision")))


@router.post("/api/inbox/conversations/{conversation_id}/drafts/{item_id}/regenerate")
async def regenerate_draft(conversation_id: str, item_id: str, request: Request):
    """{"instruction"?, "revision"}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).regenerate_draft(
        conversation_id, item_id, str(body.get("instruction") or ""), body.get("revision")))


@router.post("/api/inbox/conversations/{conversation_id}/drafts/{item_id}/approve")
async def approve_draft(conversation_id: str, item_id: str, request: Request):
    """{"revision"}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).approve_draft(
        conversation_id, item_id, body.get("revision")))


@router.post("/api/inbox/conversations/{conversation_id}/drafts/{item_id}/schedule")
async def schedule_draft(conversation_id: str, item_id: str, request: Request):
    """{"send_at": ISO, "revision"}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).schedule_draft(
        conversation_id, item_id, body.get("send_at"), body.get("revision")))


@router.post("/api/inbox/conversations/{conversation_id}/drafts/{item_id}/discard")
async def discard_draft(conversation_id: str, item_id: str, request: Request):
    """{"revision"}"""
    body = await _body(request)
    if body is None:
        return _bad_body()
    return await _command((await service(request)).discard_draft(
        conversation_id, item_id, body.get("revision")))
