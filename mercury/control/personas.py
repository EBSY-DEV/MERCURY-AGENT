"""Writing persona commands for the dashboard, the CLI and MCP.

Personas are referenced by id, exact name (any case) or a unique id prefix,
so a person at a terminal can type "Alex, founder" and a tool can pass the id.
Every failure is a PersonaError carrying a stable code.
"""

import secrets

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mercury.personas import AVATAR_SEEDS, JSON_INSTRUCTION, PersonaError, PersonaStore

EDITABLE = ("name", "description", "tone", "instructions", "examples", "avatar_seed")


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


def avatar_seed(value: str) -> str:
    """Accept a seed or its number, so "7" and "mercury-persona-07" both work."""
    value = value.strip()
    if value.isdigit() and 1 <= int(value) <= len(AVATAR_SEEDS):
        return AVATAR_SEEDS[int(value) - 1]
    if value not in AVATAR_SEEDS:
        raise PersonaError("invalid", f"Avatar must be 1-{len(AVATAR_SEEDS)} or a seed like {AVATAR_SEEDS[0]}")
    return value


def validated(data: dict) -> dict:
    try:
        return PersonaInput(**data).model_dump()
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "input"
        raise PersonaError("invalid", f"{field}: {first['msg']}") from error


class PersonaService:
    def __init__(self, state, config):
        self.state, self.config = state, config
        self.store = PersonaStore(state)

    async def ready(self):
        await self.state.init_db()
        await self.store.ensure_default(self.config)
        return self

    async def list(self, include_archived=True) -> dict:
        personas = await self.store.list()
        default_id = await self.state.get_setting("default_persona_id")
        for persona in personas:
            persona["is_default"] = persona["id"] == default_id
        if not include_archived:
            personas = [p for p in personas if not p["archived"]]
        return {"personas": personas, "default_id": default_id}

    async def find(self, ref: str) -> dict:
        ref = (ref or "").strip()
        if not ref:
            raise PersonaError("invalid", "Name a persona")
        personas = (await self.list())["personas"]
        for match in (
            lambda p: p["id"] == ref,
            lambda p: p["name"].casefold() == ref.casefold(),
            lambda p: len(ref) >= 4 and p["id"].startswith(ref),
        ):
            found = [p for p in personas if match(p)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise PersonaError("ambiguous", f"'{ref}' matches {len(found)} personas. Use the id instead")
        raise PersonaError("not_found", f"No persona called '{ref}'")

    async def get(self, ref: str) -> dict:
        persona = await self.find(ref)
        return {**persona, "versions": await self.store.versions(persona["id"])}

    async def _unique_name(self, name: str, persona_id: str = ""):
        """Names are how people and tools refer to personas, so keep them distinct."""
        for persona in (await self.list())["personas"]:
            if persona["id"] != persona_id and persona["name"].casefold() == name.strip().casefold():
                raise PersonaError("invalid", f"A persona called '{persona['name']}' already exists")

    async def create(self, data: dict) -> dict:
        data = validated({k: v for k, v in data.items() if k in EDITABLE})
        await self._unique_name(data["name"])
        persona_id = await self.store.save(data)
        return await self.find(persona_id)

    async def update(self, ref: str, changes: dict, expected_revision: int | None = None) -> dict:
        """Apply the given fields on top of the latest version.

        Only tone, instructions and examples create a new version. Without an
        expected revision the latest one is assumed, which still fails if
        someone saves in between.
        """
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise PersonaError("invalid", f"Cannot edit {', '.join(sorted(unknown))}")
        current = await self.find(ref)
        data = {key: current[key] or "" for key in EDITABLE} | changes
        data["expected_revision"] = expected_revision or current["revision"]
        data = validated(data)
        await self._unique_name(data["name"], current["id"])
        await self.store.save(data, current["id"])
        return await self.find(current["id"])

    async def set_default(self, ref: str) -> dict:
        persona = await self.find(ref)
        await self.store.set_default(persona["id"])
        return await self.find(persona["id"])

    async def set_archived(self, ref: str, archived: bool) -> dict:
        persona = await self.find(ref)
        await self.store.archive(persona["id"], archived)
        return await self.find(persona["id"])

    async def versions(self, ref: str) -> list[dict]:
        """Every version, newest first, with what it wrote and how it did."""
        persona_id = (await self.find(ref))["id"]
        stats = await self.store.stats(persona_id)
        empty = {"drafted": 0, "sent": 0, "replies": 0}
        return [v | {key: stats.get(v["id"], empty)[key] for key in empty} for v in await self.store.versions(persona_id)]

    async def totals(self) -> dict:
        """Drafted, sent and replies per persona across all its versions."""
        result = {}
        for row in (await self.store.stats()).values():
            total = result.setdefault(row["persona_id"], {"drafted": 0, "sent": 0, "replies": 0})
            for key in total:
                total[key] += row[key]
        return result

    async def _generation_inputs(self, ref, prospect_id, revision=None, version_id="", draft=None):
        """Contact by id or email; persona by reference and revision, or the default."""
        from mercury.agents.writer import Writer
        from mercury.brain import Brain
        prospect = await self.state.get_prospect(prospect_id)
        if not prospect and "@" in prospect_id:
            prospect = await self.state.get_prospect_by_email(prospect_id.strip())
        if not prospect:
            raise PersonaError("not_found", "Contact not found")
        if not version_id and (ref or revision):
            persona = await self.find(ref or await self.state.get_setting("default_persona_id"))
            versions = await self.store.versions(persona["id"])
            version = next((v for v in versions if v["revision"] == revision), None) if revision else versions[0]
            if version is None:
                raise PersonaError("not_found", f"{persona['name']} has no version {revision}")
            version_id = version["id"]
        profile = await self.store.resolve(self.config, version_id)
        if profile["archived"]:
            raise PersonaError("invalid", "Restore this persona before generating a preview")
        if draft:
            # Unsaved edits ride on the version they started from; the snapshot
            # says so, and nothing is saved as a new version.
            edits = validated({key: profile[key] or "" for key in EDITABLE} | {
                key: draft[key] for key in ("tone", "instructions", "examples") if key in draft})
            profile = profile | {key: edits[key] for key in ("tone", "instructions", "examples")} | {"unsaved": True}
        return Writer(Brain(self.state), self.state, self.config), prospect, profile

    async def prompt(self, ref, prospect_id, revision=None, instruction="", version_id="", draft=None) -> dict:
        """The exact writer prompt, assembled without a model call, plus its labelled sections."""
        writer, prospect, profile = await self._generation_inputs(ref, prospect_id, revision, version_id, draft)
        sections, _profile = await writer.personal_prompt_sections(prospect, instruction, profile)
        sections.append(("format", "Output format", JSON_INSTRUCTION))
        return {
            "prompt": "".join(text for _key, _label, text in sections),
            "sections": [{"key": key, "label": label, "text": text} for key, label, text in sections],
            "persona": profile,
        }

    async def preview(self, ref, prospect_id, revision=None, instruction="", version_id="", draft=None) -> dict:
        """Write one sample email. One model call; nothing is queued or sent."""
        writer, prospect, profile = await self._generation_inputs(ref, prospect_id, revision, version_id, draft)
        draft = await writer._write_personal_email(prospect, instruction, profile)
        if not draft:
            raise PersonaError("provider_failed", "The writer returned no draft. Check the agent log and try again")
        async with self.state._connect() as db:
            cursor = await db.execute("SELECT prompt FROM email_generations WHERE id = ?", (draft["generation_id"],))
            prompt = (await cursor.fetchone())[0]
        return {**draft, "prompt": prompt, "persona": profile}
