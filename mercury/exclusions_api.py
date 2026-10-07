"""Local dashboard API for exclusions and company holds."""

import base64
import binascii

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.exclusions import ExclusionError, ExclusionService
from mercury.csv_import import MAX_BYTES

router = APIRouter()
HTTP_STATUS = {"not_found": 404, "protected": 409, "not_blocked": 409, "still_excluded": 409}
ACTOR = "dashboard"


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: str = Field(max_length=10)
    value: str = Field(min_length=1, max_length=320)
    reason: str = Field(default="", max_length=300)
    include_subdomains: bool = False


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=300)
    confirm_opt_out: bool = False


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_b64: str = Field(min_length=1, max_length=(MAX_BYTES * 4) // 3 + 8)
    reason: str = Field(default="", max_length=300)


class HoldInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=300)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=300)


async def service() -> ExclusionService:
    from mercury.dashboard import _state
    try:
        from mercury.config import load_config
        config = load_config()
    except Exception:
        config = None
    return await ExclusionService(_state(), config).ready()


async def call(command):
    try:
        return await command
    except ExclusionError as error:
        raise HTTPException(HTTP_STATUS.get(error.code, 422),
                            {"code": error.code, "message": str(error), **error.details}) from error


@router.get("/api/exclusions")
async def list_exclusions(q: str = "", source: str = "", removed: bool = False):
    svc = await service()
    return {"rules": await svc.list(q[:120], source[:20], removed), "policy": svc.settings()}


@router.get("/api/exclusions/export.csv")
async def export_exclusions(removed: bool = False):
    text = await (await service()).export_csv(removed)
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="exclusions.csv"'})


@router.get("/api/exclusions/check")
async def check_exclusion(email: str):
    return await (await service()).check(email[:320])


@router.get("/api/exclusions/{rule_id}")
async def get_exclusion(rule_id: str):
    return await call((await service()).get(rule_id))


@router.post("/api/exclusions")
async def add_exclusion(body: AddInput):
    return await call((await service()).add(
        body.kind, body.value, reason=body.reason,
        include_subdomains=body.include_subdomains, actor=ACTOR))


@router.post("/api/exclusions/import")
async def import_exclusions(body: ImportInput):
    try:
        data = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(422, {"code": "bad_file",
                                  "message": "The upload could not be read."}) from error
    return await call((await service()).import_csv(data, actor=ACTOR, reason=body.reason))


@router.post("/api/exclusions/{rule_id}/remove")
async def remove_exclusion(rule_id: str, body: RemoveInput):
    return await call((await service()).remove(
        rule_id, note=body.note, actor=ACTOR, confirm_opt_out=body.confirm_opt_out))


@router.post("/api/outbox/{item_id}/requeue")
async def requeue_outbox(item_id: str):
    return await call((await service()).requeue(item_id))


@router.get("/api/company-holds")
async def list_holds(released: bool = False):
    return {"holds": await (await service()).holds(released)}


@router.post("/api/company-holds")
async def add_hold(body: HoldInput):
    return await call((await service()).hold(body.company_id, note=body.note, actor=ACTOR))


@router.post("/api/company-holds/{hold_id}/release")
async def release_hold(hold_id: str, body: NoteInput):
    return await call((await service()).release(hold_id, note=body.note, actor=ACTOR))
