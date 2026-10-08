"""Local dashboard API for per-prospect demos (the demo gate's bookkeeping)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.demos import DemoError, DemoService

logger = logging.getLogger("mercury.dashboard")
router = APIRouter()
HTTP_STATUS = {"not_found": 404, "no_config": 503, "retired": 409}


class DemoRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A demo id, a contact id or email, or an outbox email id.
    target: str = Field(min_length=1, max_length=200)
    offer_key: str = Field(default="", max_length=80)


class ReadyInput(DemoRef):
    demo_url: str | None = Field(default=None, max_length=500)
    recording_path: str | None = Field(default=None, max_length=500)
    agent_id: str | None = Field(default=None, max_length=200)
    built_by: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=2000)


class RetireInput(DemoRef):
    reason: str = Field(default="", max_length=200)


async def service() -> DemoService:
    from mercury.config import load_config
    from mercury.dashboard import _state

    try:
        config = load_config()
    except Exception as error:  # listing still works; changes are refused
        logger.warning(f"/api/demos: could not load the config: {error}")
        config = None
    return await DemoService(_state(), config).ready()


async def call(command):
    try:
        return await command
    except DemoError as error:
        raise HTTPException(HTTP_STATUS.get(error.code, 422),
                            {"code": error.code, "message": str(error)}) from error


@router.get("/api/demos")
async def overview(include_retired: bool = False):
    svc = await service()
    return await call(svc.overview(include_retired))


@router.post("/api/demos/request")
async def request_demo(body: DemoRef):
    svc = await service()
    return {"success": True, "demo": await call(svc.request(body.target, body.offer_key))}


@router.post("/api/demos/ready")
async def mark_ready(body: ReadyInput):
    svc = await service()
    fields = body.model_dump(exclude={"target", "offer_key"}, exclude_none=True)
    return {"success": True, "demo": await call(svc.mark_ready(body.target, body.offer_key, **fields))}


@router.post("/api/demos/retire")
async def retire(body: RetireInput):
    svc = await service()
    return {"success": True, "demo": await call(svc.retire(body.target, body.offer_key, body.reason))}
