"""Local dashboard API for CSV contact imports.

The browser keeps the file and sends it (base64) with each preview and
commit, so the server never stores an upload.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.imports import ImportService, decode_upload
from mercury.csv_import import MAX_BYTES, ImportFileError

router = APIRouter()
HTTP_STATUS = {"not_found": 404, "too_large": 413}


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(default="", max_length=200)
    content_b64: str = Field(min_length=1, max_length=(MAX_BYTES * 4) // 3 + 8)
    mapping: dict[str, str] | None = None
    delimiter: str = Field(default="", max_length=10)
    policy: str = Field(default="skip", max_length=10)
    exclude_rows: list[int] = Field(default_factory=list, max_length=10000)


class CommitInput(ImportInput):
    skip_invalid: bool = False


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=100)
    after_row: int = Field(default=0, ge=0)


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_risky: bool = False


async def service() -> ImportService:
    from mercury.config import load_env
    from mercury.dashboard import _state
    return await ImportService(_state(), load_env()).ready()


async def call(command):
    """Run one service command; an ImportFileError becomes an HTTP error whose
    detail carries the code and anything the UI needs to offer a fix."""
    try:
        return await command
    except ImportFileError as error:
        raise HTTPException(HTTP_STATUS.get(error.code, 422),
                            {"code": error.code, "message": str(error), **error.details}) from error


def _options(body: ImportInput) -> dict:
    return dict(filename=body.filename, mapping=body.mapping, delimiter=body.delimiter,
                policy=body.policy, exclude_rows=body.exclude_rows)


@router.post("/api/imports/preview")
async def preview(body: ImportInput):
    svc = await service()
    return await call(svc.preview(decode_upload(body.content_b64), **_options(body)))


@router.post("/api/imports/commit")
async def commit(body: CommitInput):
    svc = await service()
    return await call(svc.commit(decode_upload(body.content_b64), skip_invalid=body.skip_invalid,
                                 origin="dashboard", **_options(body)))


@router.get("/api/imports")
async def batches():
    svc = await service()
    return {"batches": await svc.batches(), "providers": svc._providers()}


@router.get("/api/imports/{batch_id}")
async def batch(batch_id: str):
    return await call((await service()).batch(batch_id))


@router.get("/api/imports/{batch_id}/verify")
async def verify_estimate(batch_id: str):
    return await call((await service()).verify_estimate(batch_id))


@router.post("/api/imports/{batch_id}/verify")
async def verify(batch_id: str, body: VerifyInput):
    return await call((await service()).verify(batch_id, limit=body.limit, after_row=body.after_row))


@router.post("/api/imports/{batch_id}/release")
async def release(batch_id: str, body: ReleaseInput):
    return await call((await service()).release(batch_id, include_risky=body.include_risky))
