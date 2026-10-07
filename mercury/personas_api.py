"""Local dashboard API for writing personas, prompt inspection and previews."""

import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mercury.personas import AVATAR_SEEDS, JSON_INSTRUCTION, PersonaStore, avatar_url, generation_config

router = APIRouter()


class PersonaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    tone: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(default="", max_length=8000)
    examples: str = Field(default="", max_length=8000)
    avatar_seed: str = Field(default_factory=lambda: secrets.choice(AVATAR_SEEDS))
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("avatar_seed")
    @classmethod
    def valid_avatar(cls, value):
        if value not in AVATAR_SEEDS:
            raise ValueError("Choose an avatar from the Critters collection")
        return value


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
    state = _state()
    await state.init_db()
    config = load_config()
    store = PersonaStore(state)
    await store.ensure_default(config)
    return state, config, store


@router.get("/api/personas")
async def overview():
    from mercury.brain import Brain, PROMPTS_DIR
    from mercury.config import _find_config_file
    state, config, store = await context()
    brain = Brain(state)
    template = PROMPTS_DIR / "writer.md"
    return {
        "personas": await store.list(),
        "default_id": await state.get_setting("default_persona_id"),
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
    _state, _config, store = await context()
    persona_id = await store.save(body.model_dump())
    return {"success": True, "id": persona_id}


@router.post("/api/personas/{persona_id}/save")
async def save_persona(persona_id: str, body: PersonaInput):
    _state, _config, store = await context()
    try:
        await store.save(body.model_dump(), persona_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"success": True, "id": persona_id}


@router.post("/api/personas/{persona_id}/default")
async def default_persona(persona_id: str):
    _state, _config, store = await context()
    try:
        await store.set_default(persona_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"success": True}


@router.post("/api/personas/{persona_id}/archive")
async def archive_persona(persona_id: str, body: ArchiveInput):
    _state, _config, store = await context()
    try:
        await store.archive(persona_id, body.archived)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"success": True}


@router.get("/api/personas/{persona_id}/versions")
async def persona_versions(persona_id: str):
    _state, _config, store = await context()
    return {"versions": await store.versions(persona_id)}


async def preview_context(body):
    from mercury.agents.writer import Writer
    from mercury.brain import Brain
    state, config, store = await context()
    prospect = await state.get_prospect(body.prospect_id)
    if not prospect:
        raise HTTPException(404, "Contact not found")
    try:
        profile = await store.resolve(config, body.version_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    if profile["archived"]:
        raise HTTPException(409, "Restore this persona before generating a preview")
    return Writer(Brain(state), state, config), prospect, profile


@router.post("/api/personas/prompt")
async def inspect_prompt(body: PromptInput):
    writer, prospect, profile = await preview_context(body)
    prompt, _profile = await writer.build_personal_prompt(prospect, body.instruction, profile)
    return {"prompt": prompt + JSON_INSTRUCTION, "persona": profile}


@router.post("/api/personas/preview")
async def preview_email(body: PromptInput):
    writer, prospect, profile = await preview_context(body)
    # This makes one model call, but never creates an outbox item or campaign.
    draft = await writer._write_personal_email(prospect, body.instruction, profile)
    if not draft:
        raise HTTPException(502, "The writer returned no draft. Check the agent log and try again")
    async with writer.state._connect() as db:
        cursor = await db.execute("SELECT prompt FROM email_generations WHERE id = ?", (draft["generation_id"],))
        prompt = (await cursor.fetchone())[0]
    return {"success": True, **draft, "prompt": prompt, "persona": profile}


@router.get("/api/outbox/{item_id}/generation-history")
async def email_history(item_id: str):
    from mercury.dashboard import _state
    state = _state()
    await state.init_db()
    item = await state.get_outbox_item(item_id)
    if not item:
        raise HTTPException(404, "Email not found")
    return {"email": item, "generations": await PersonaStore(state).history(item_id)}
