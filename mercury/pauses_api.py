"""Local dashboard API for out-of-office pauses."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.pauses import PauseError, PauseService

router = APIRouter()
HTTP_STATUS = {"not_found": 404, "over": 409}
ACTOR = "dashboard"


class DateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date: str = Field(min_length=10, max_length=10)
    note: str = Field(default="", max_length=300)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=300)


async def service() -> PauseService:
    from mercury.dashboard import _state
    try:
        from mercury.config import load_config
        config = load_config()
    except Exception:
        config = None
    return await PauseService(_state(), config).ready()


async def call(command):
    try:
        return await command
    except PauseError as error:
        raise HTTPException(HTTP_STATUS.get(error.code, 422),
                            {"code": error.code, "message": str(error), **error.details}) from error


@router.get("/api/pauses")
async def list_pauses(ended: bool = False):
    svc = await service()
    return {"pauses": await svc.list(ended), "timezone": svc.timezone(),
            "capability": svc.capability()}


@router.get("/api/pauses/{pause_id}")
async def get_pause(pause_id: str):
    return await call((await service()).get(pause_id))


@router.post("/api/pauses/{pause_id}/return-date")
async def set_return_date(pause_id: str, body: DateInput):
    return await call((await service()).set_return_date(
        pause_id, body.date, note=body.note, actor=ACTOR))


@router.post("/api/pauses/{pause_id}/resume")
async def resume_pause(pause_id: str, body: NoteInput):
    return await call((await service()).resume(pause_id, note=body.note, actor=ACTOR))
