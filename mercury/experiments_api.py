"""Local dashboard API for A/B experiments (see mercury/control/experiments.py).

Reads return the service's JSON as is: every count, rate, interval and
decision the Experiments screen shows is computed here, never in the page.
Commands return ``{"success": true, ...}``; a refused command returns
``{"success": false, "message", "code", ...details}`` with an HTTP status
from its error class (400 invalid, 404 not_found, 409 not_editable /
stale_version / confirmation_required). An ``Idempotency-Key`` header makes
a retried command return the first answer instead of running again.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mercury.control.errors import ControlError
from mercury.control.experiments import ExperimentService, parse_revision

router = APIRouter()


async def service(request: Request | None = None) -> ExperimentService:
    from mercury.dashboard import _ctx, _demo_config, _state

    return await ExperimentService(_ctx(request), _state(), _demo_config()).ready()


def _error(error: ControlError) -> JSONResponse:
    from mercury.dashboard import _control_error

    return _control_error(error, detail=True)


def _unexpected(error: Exception) -> JSONResponse:
    from mercury.control.audit import redact_text

    return JSONResponse({"success": False, "message": redact_text(error)}, status_code=500)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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

@router.get("/api/experiments")
async def list_experiments(request: Request):
    """The table: one row per experiment, plus the options the form needs."""
    return await _read((await service(request)).list())


@router.get("/api/experiments/{ref}")
async def get_experiment(ref: str, request: Request, revision: str = ""):
    """Definition, controls and results of one revision (current by default)."""
    svc = await service(request)
    try:
        number = parse_revision(revision)
    except ControlError as e:
        return _error(e)
    return await _read(svc.get(ref, number))


@router.get("/api/experiments/{ref}/results")
async def experiment_results(ref: str, request: Request, revision: str = ""):
    svc = await service(request)
    try:
        number = parse_revision(revision)
    except ControlError as e:
        return _error(e)
    return await _read(svc.results(ref, number))


@router.get("/api/experiments/{ref}/assignments")
async def experiment_assignments(ref: str, request: Request, revision: str = "", arm: str = "",
                                 limit: int = 100, offset: int = 0):
    """Each assignment with the emails sent for it and their generation."""
    svc = await service(request)
    try:
        number = parse_revision(revision)
    except ControlError as e:
        return _error(e)
    return await _read(svc.exposures(ref, number, arm, limit, offset))


@router.get("/api/experiments/{ref}/preview")
async def preview_saved(ref: str, request: Request, sample: int = 10, prospect: str = ""):
    return await _read((await service(request)).preview(ref, sample=sample,
                                                        prompt_prospect=prospect))


@router.post("/api/experiments/preview")
async def preview_definition(request: Request):
    """Preview a definition before saving it (the New experiment form)."""
    data = await _body(request)
    return await _read((await service(request)).preview(definition=data.get("definition") or data))


# ── Commands ──

@router.post("/api/experiments")
async def create_experiment(request: Request):
    return await _command((await service(request)).create(await _body(request)))


@router.patch("/api/experiments/{ref}")
async def update_experiment(ref: str, request: Request):
    data = await _body(request)
    expected = data.pop("expected_version", None)
    return await _command((await service(request)).update(ref, data, expected))


@router.post("/api/experiments/{ref}/start")
async def start_experiment(ref: str, request: Request):
    return await _command((await service(request)).start(ref))


@router.post("/api/experiments/{ref}/pause")
async def pause_experiment(ref: str, request: Request):
    """Pause enrollment. Enrolled prospects continue."""
    return await _command((await service(request)).pause(ref))


@router.post("/api/experiments/{ref}/resume")
async def resume_experiment(ref: str, request: Request):
    return await _command((await service(request)).resume(ref))


@router.post("/api/experiments/{ref}/hold")
async def hold_experiment(ref: str, request: Request):
    """Hold every unsent email of the experiment."""
    data = await _body(request)
    return await _command((await service(request)).hold(ref, str(data.get("reason") or "")))


@router.post("/api/experiments/{ref}/release")
async def release_experiment(ref: str, request: Request):
    return await _command((await service(request)).release(ref))


@router.post("/api/experiments/{ref}/complete")
async def complete_experiment(ref: str, request: Request):
    data = await _body(request)
    return await _command((await service(request)).complete(ref, bool(data.get("confirm"))))


@router.post("/api/experiments/outcomes/{inbound_id}")
async def label_outcome(inbound_id: str, request: Request):
    """Set the outcome label of one reply by hand."""
    data = await _body(request)
    return await _command((await service(request)).label(inbound_id, str(data.get("label") or "")))
