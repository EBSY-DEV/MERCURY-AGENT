"""Local dashboard API for the pain library (proposed / confirmed / rejected).

Same contract as the signals gate: Mercury proposes, a person decides, and
only confirmed pains are written from. Mutations return
``{"success": true, ...}``; a refusal is ``{"success": false, "message",
"code"}`` with a 4xx status.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.errors import ControlError

router = APIRouter()
CODE_STATUS = {"not_found": 404, "duplicate": 409, "matches_rejected": 409,
               "stale_revision": 409}


class PainBody(BaseModel):
    """Fields of a pain; on a save only the fields sent change."""
    model_config = ConfigDict(extra="forbid")
    code: str = Field(default="", max_length=48)
    label: str | None = Field(default=None, max_length=200)
    market: str | None = Field(default=None, max_length=80)
    sector: str | None = Field(default=None, max_length=80)
    owner_words: str | None = Field(default=None, max_length=600)
    scene: str | None = Field(default=None, max_length=400)
    cost: str | None = Field(default=None, max_length=300)
    signal_codes: list[str] | None = None
    offer_key: str | None = Field(default=None, max_length=80)
    evidence: list[str] | None = None
    avoid_terms: list[str] | None = None
    # Create only: decide it in the same step (a person adding their own pain).
    confirm: bool = False
    # Save only: the revision on screen. A stale one is refused.
    expected_revision: int | None = Field(default=None, ge=1)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(max_length=20)
    code: str = Field(default="", max_length=48)
    codes: list[str] = Field(default_factory=list, max_length=200)
    note: str = Field(default="", max_length=300)
    # {code: revision on screen}; a stale one is reported in "stale".
    revisions: dict[str, int] = Field(default_factory=dict)


def _service(request: Request | None = None):
    from mercury.control.pains import PainService
    from mercury.dashboard import _ctx, _state
    return PainService(_state(), _ctx(request))


def _fail(error: ControlError) -> JSONResponse:
    from mercury.dashboard import _control_error
    return _control_error(error, detail=True, overrides=CODE_STATUS)


def _fields(body: PainBody) -> dict:
    return body.model_dump(exclude_unset=True, exclude={"confirm", "expected_revision"})


@router.get("/api/pains")
async def list_pains(status: str = "", market: str | None = None, offer_key: str | None = None):
    """The library: ``pains`` (each with ``stats``), ``summary`` counts, and
    the choices an editor offers: ``markets``, ``offers``, ``signals``."""
    try:
        service = await _service().ready()
        data = await service.list(status.strip(), market, offer_key)
    except ControlError as error:
        return _fail(error)
    markets, offers = [], []
    try:
        from mercury.config import load_config
        from mercury.demos import offers_by_key
        config = load_config()
        markets = [m.name for m in config.icp.markets]
        offers = sorted(offers_by_key(config))
    except Exception:
        pass  # the library still lists without a readable mercury.yaml
    return {**data, "markets": markets, "offers": offers, "signals": await service.vocabulary()}


@router.get("/api/pains/{code}")
async def get_pain(code: str):
    try:
        return await (await _service().ready()).get(code.upper())
    except ControlError as error:
        return _fail(error)


@router.post("/api/pains")
async def add_pain(body: PainBody, request: Request):
    """Add a pain by hand. It starts proposed unless ``confirm`` is true."""
    try:
        pain = await (await _service(request).ready()).add(_fields(body), confirm=body.confirm)
        return {"success": True, "pain": pain}
    except ControlError as error:
        return _fail(error)


@router.post("/api/pains/status")
async def set_pains_status(body: StatusBody, request: Request):
    """Confirm, reject or reopen pains: {"codes" | "code", "status", "note"}.
    Unknown codes come back in ``unknown``, stale revisions in ``stale``."""
    codes = body.codes or ([body.code] if body.code else [])
    try:
        result = await (await _service(request).ready()).set_status(
            codes, body.status, body.note, body.revisions or None)
        return {"success": True, **result}
    except ControlError as error:
        return _fail(error)


@router.post("/api/pains/{code}/save")
async def save_pain(code: str, body: PainBody, request: Request):
    """Edit the fields sent; the status is not editable here."""
    try:
        pain = await (await _service(request).ready()).edit(
            code, _fields(body), body.expected_revision)
        return {"success": True, "pain": pain}
    except ControlError as error:
        return _fail(error)
