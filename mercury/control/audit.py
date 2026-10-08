"""Idempotent commands and the audit trail.

Every command that changes something runs through ``run_command``:

1. The scope check. A refusal is audited like any other outcome.
2. With a request key (``OperatorContext.request_id``), a claim on that key
   for this client and operator. Replaying a finished key returns the
   recorded result, or raises the recorded error, and runs nothing. The
   same key with different arguments is refused (idempotency_key_reused);
   one still in flight is refused (request_in_progress). A failure that may
   pass on retry (Unavailable, an unexpected exception) releases the key.
3. The work. It notes each object it touched on an ``AuditTrail``.
4. One audit_log row per object: action, revisions before and after,
   outcome (ok, replayed, or the error code), a message and details.

Nothing secret is stored or echoed: ``redact`` drops values under
secret-looking keys and masks secret-shaped text (bearer tokens,
key=value credentials, passwords in URLs, the values of secret environment
variables) in messages and details.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from mercury.control import errors
from mercury.control.errors import ControlError, Forbidden, Unavailable

logger = logging.getLogger(__name__)

SECRET_HINTS = ("password", "secret", "token", "api_key", "apikey", "credential", "private_key", "_env")
REDACTED = "[redacted]"
_KEY_WORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|credential|private[_-]?key)"
_TEXT_PATTERNS = (
    # Authorization: Bearer abc...
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1" + REDACTED),
    # api_key=abc, SMTP_PASSWORD = abc
    (re.compile(rf"(?i)(\b\w*{_KEY_WORDS}\w*\s*=\s*)[\"']?[^\s\"',;&]+[\"']?"), r"\1" + REDACTED),
    # "password": "abc"
    (re.compile(rf"(?i)([\"']\w*{_KEY_WORDS}\w*[\"']\s*:\s*)[\"'][^\"']*[\"']"), r'\1"' + REDACTED + '"'),
    # smtp://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), r"\1" + REDACTED + "@"),
)
MESSAGE_MAX = 500


def looks_secret(path: str) -> bool:
    key = str(path).lower()
    return any(hint in key for hint in SECRET_HINTS)


def _secret_env_values() -> list[str]:
    values = {v for k, v in os.environ.items() if looks_secret(k) and v and len(v) >= 8}
    return sorted(values, key=len, reverse=True)


def redact_text(text) -> str:
    text = str(text or "")
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    for value in _secret_env_values():
        text = text.replace(value, REDACTED)
    return text


def redact(value):
    """``value`` with secrets removed, at any depth."""
    if isinstance(value, dict):
        return {k: REDACTED if looks_secret(k) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def fingerprint(action: str, params: dict) -> str:
    """What a request key promises: this action with these arguments."""
    payload = json.dumps({"action": action, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditTrail:
    """The objects one command touched. ``record`` once per object."""

    def __init__(self, object_type: str):
        self.object_type = object_type
        self.batch_id = ""
        self.entries: list[dict] = []

    def record(self, object_id: str, revision_before=None, revision_after=None,
               outcome: str = "ok", message: str = "", object_type: str = "", **detail):
        self.entries.append({
            "object_type": object_type or self.object_type, "object_id": object_id or "",
            "revision_before": "" if revision_before is None else str(revision_before),
            "revision_after": "" if revision_after is None else str(revision_after),
            "outcome": outcome, "message": message, "detail": detail,
        })


def _error_record(error: ControlError) -> dict:
    return {"class": type(error).__name__, "code": error.code,
            "message": redact_text(str(error)), "details": redact(error.details)}


def _raise_recorded(record: dict):
    cls = getattr(errors, record.get("class", ""), None)
    if not (isinstance(cls, type) and issubclass(cls, ControlError)):
        cls = ControlError
    details = dict(record.get("details") or {})
    if cls is errors.ProhibitedField:
        raise cls(record["message"], details.get("fields") or [], code=record["code"])
    raise cls(record["message"], code=record["code"], **details)


async def run_command(state, ctx, action: str, *, scope: str, params: dict, work,
                      object_type: str, object_id: str = "", revision_before=None):
    """Run ``work(trail)`` once for this request key and audit how it ended.
    ``params`` are the command's arguments as the caller gave them: they
    make the key's fingerprint and are never stored."""
    trail = AuditTrail(object_type)
    key = ctx.request_id or ""
    base = {"client": ctx.client, "operator": ctx.operator, "request_key": key, "action": action}

    async def audit(entries: list[dict]):
        rows = [{**base, "batch_id": trail.batch_id,
                 "object_type": e["object_type"], "object_id": e["object_id"],
                 "revision_before": e["revision_before"], "revision_after": e["revision_after"],
                 "outcome": e["outcome"], "message": redact_text(e["message"])[:MESSAGE_MAX],
                 "detail_json": json.dumps(redact(e["detail"]), default=str)}
                for e in entries]
        try:
            await state.add_audit(rows)
        except Exception as e:
            # The command already happened (or was refused); a lost audit
            # row must not turn that into a different answer.
            logger.error("audit write failed for %s: %s", action, redact_text(e))

    def failure(outcome: str, message: str, **detail) -> list[dict]:
        trail.record(object_id, revision_before, None, outcome=outcome, message=message, **detail)
        return trail.entries

    try:
        ctx.require(scope)
    except Forbidden as error:
        await audit(failure(error.code, str(error), **error.details))
        raise

    if key:
        mark = fingerprint(action, params)
        existing = await state.begin_command(ctx.client, ctx.operator, key, action, mark)
        if existing:
            if existing["action"] != action or existing["fingerprint"] != mark:
                error = errors.Conflict("this request key was already used for a different command",
                                        code="idempotency_key_reused")
            elif existing["state"] != "done":
                error = errors.Conflict("this request is still being processed",
                                        code="request_in_progress")
            else:
                recorded = json.loads(existing["result_json"] or "{}")
                await audit(failure("replayed", f"replay of a request that ended {existing['outcome']}"))
                if "error" in recorded:
                    _raise_recorded(recorded["error"])
                return recorded.get("result")
            await audit(failure(error.code, str(error)))
            raise error

    try:
        result = await work(trail)
    except ControlError as error:
        if key:
            if isinstance(error, Unavailable):
                await state.release_command(ctx.client, ctx.operator, key)
            else:
                await state.finish_command(ctx.client, ctx.operator, key, error.code,
                                           json.dumps({"error": _error_record(error)}))
        await audit(failure(error.code, str(error), **error.details))
        raise
    except BaseException as error:
        if key:
            await state.release_command(ctx.client, ctx.operator, key)
        await audit(failure("error", f"{type(error).__name__}: {error}"))
        raise
    if key:
        await state.finish_command(ctx.client, ctx.operator, key, "ok",
                                   json.dumps({"result": result}, default=str))
    if not trail.entries:
        trail.record(object_id, revision_before, None)
    await audit(trail.entries)
    return result
