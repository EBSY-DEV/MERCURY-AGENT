"""Local dashboard API for writing personas, prompt inspection and previews."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mercury.control.personas import EDITABLE, PersonaInput, PersonaService
from mercury.personas import AVATAR_SEEDS, PersonaError, PersonaStore, avatar_url, generation_config

router = APIRouter()
HTTP_STATUS = {"not_found": 404, "provider_failed": 502}


class PromptInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    prospect_id: str = Field(min_length=1, max_length=100)
    version_id: str = Field(default="", max_length=100)
    instruction: str = Field(default="", max_length=500)


class ArchiveInput(BaseModel):
    archived: bool


async def context():
    from mercury.config import load_config
    from mercury.dashboard import _state
    service = await PersonaService(_state(), load_config()).ready()
    return service.state, service.config, service


async def call(command):
    """Run one service command, turning its error code into an HTTP status."""
    try:
        return await command
    except PersonaError as error:
        raise HTTPException(HTTP_STATUS.get(error.code, 409), str(error)) from error


@router.get("/api/personas")
async def overview():
    from mercury.brain import Brain, PROMPTS_DIR
    from mercury.config import _find_config_file
    state, config, service = await context()
    brain = Brain(state)
    template = PROMPTS_DIR / "writer.md"
    return {
        **await service.list(),
        "current": generation_config(config),
        "config_file": _find_config_file(),
        "markets": [market.model_dump() for market in config.icp.markets],
        "email": {"provider": config.channels.email.provider,
                  "require_approval": config.channels.email.require_approval,
                  "max_daily_sends": config.channels.email.max_daily_sends},
        "template": template.read_text() if template.exists() else "",
        "knowledge": brain.load_skills_for_agent("writer"),
        "avatars": [{"seed": seed, "url": avatar_url(seed)} for seed in AVATAR_SEEDS],
    }


@router.post("/api/personas")
async def create_persona(body: PersonaInput):
    *_, service = await context()
    persona = await call(service.create(body.model_dump()))
    return {"success": True, "id": persona["id"]}


@router.post("/api/personas/{persona_id}/save")
async def save_persona(persona_id: str, body: PersonaInput):
    *_, service = await context()
    fields = body.model_dump(include=set(EDITABLE))
    await call(service.update(persona_id, fields, body.expected_revision))
    return {"success": True, "id": persona_id}


@router.post("/api/personas/{persona_id}/default")
async def default_persona(persona_id: str):
    *_, service = await context()
    await call(service.set_default(persona_id))
    return {"success": True}


@router.post("/api/personas/{persona_id}/archive")
async def archive_persona(persona_id: str, body: ArchiveInput):
    *_, service = await context()
    await call(service.set_archived(persona_id, body.archived))
    return {"success": True}


@router.get("/api/personas/{persona_id}/versions")
async def persona_versions(persona_id: str):
    *_, service = await context()
    return {"versions": await call(service.versions(persona_id))}


@router.post("/api/personas/prompt")
async def inspect_prompt(body: PromptInput):
    *_, service = await context()
    return await call(service.prompt("", body.prospect_id, instruction=body.instruction, version_id=body.version_id))


@router.post("/api/personas/preview")
async def preview_email(body: PromptInput):
    *_, service = await context()
    # This makes one model call, but never creates an outbox item or campaign.
    result = await call(service.preview("", body.prospect_id, instruction=body.instruction, version_id=body.version_id))
    return {"success": True, **result}


@router.get("/api/outbox/{item_id}/generation-history")
async def email_history(item_id: str):
    from mercury.dashboard import _state
    state = _state()
    await state.init_db()
    item = await state.get_outbox_item(item_id)
    if not item:
        raise HTTPException(404, "Email not found")
    return {"email": item, "generations": await PersonaStore(state).history(item_id)}
