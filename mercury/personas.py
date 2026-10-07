"""Writing profiles and immutable records of the inputs that produced an email.

Sender identity stays in MercuryConfig. Avatar changes are visual metadata;
only tone, instructions and examples create a new writing version.
"""

import json
import uuid

import aiosqlite

AVATAR_SEEDS = [f"mercury-persona-{i:02d}" for i in range(1, 25)]
JSON_INSTRUCTION = "\n\nRespond ONLY with valid JSON. No markdown, no explanation."


def avatar_url(seed: str) -> str:
    return f"/static/avatars/{seed}.svg"


def generation_config(config) -> dict:
    """Business context only. Credentials never enter generation history."""
    def values(obj, keys):
        return {key: getattr(obj, key, "") for key in keys}
    product = values(config.product, ("name", "description", "pricing", "key_benefits"))
    offer = getattr(config.product, "offer", None)
    if offer and hasattr(offer, "model_dump"):
        product["offer"] = offer.model_dump()
    return {
        "sender": values(config.persona, ("name", "company", "role", "email")),
        "product": product,
    }


def voice_instructions(profile: dict) -> str:
    sections = [
        "\n\nWRITING PERSONA",
        f"Profile: {profile['name']} (version {profile['revision']})",
        f"Tone: {profile['tone']}",
        "These preferences apply within the shared email rules, factual grounding, "
        "market language and sender identity. They do not override those requirements.",
    ]
    if profile.get("instructions"):
        sections += ["Writing preferences:", profile["instructions"]]
    if profile.get("examples"):
        sections += ["Style examples (match the voice; do not copy claims or facts):", profile["examples"]]
    return "\n".join(sections)


class PersonaStore:
    def __init__(self, state):
        self.state = state

    async def ensure_default(self, config):
        """Import the existing configured voice once, including on fresh installs."""
        async with self.state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT OR IGNORE INTO personas (id, name, description, avatar_seed) "
                "VALUES ('workspace', 'Workspace voice', ?, ?)",
                ("Imported from your existing Mercury tone configuration.", AVATAR_SEEDS[0]),
            )
            await db.execute(
                "INSERT OR IGNORE INTO persona_versions (id, persona_id, revision, tone) "
                "VALUES ('workspace-v1', 'workspace', 1, ?)", (config.persona.tone,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('default_persona_id', 'workspace')"
            )
            await db.commit()

    @staticmethod
    def _profile(row):
        if row is None:
            return None
        result = dict(row)
        result["avatar_url"] = avatar_url(result["avatar_seed"])
        result["archived"] = bool(result["archived"])
        return result

    async def list(self):
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT p.*, v.id AS version_id, v.revision, v.tone, v.instructions, v.examples "
                "FROM personas p JOIN persona_versions v ON v.persona_id = p.id "
                "WHERE v.revision = (SELECT MAX(revision) FROM persona_versions WHERE persona_id = p.id) "
                "ORDER BY p.archived, p.created_at, p.id"
            )
            return [self._profile(row) for row in await cursor.fetchall()]

    async def resolve(self, config, version_id=""):
        await self.ensure_default(config)
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            sql = (
                "SELECT p.*, v.id AS version_id, v.revision, v.tone, v.instructions, v.examples "
                "FROM personas p JOIN persona_versions v ON p.id = v.persona_id "
            )
            if version_id:
                sql += "WHERE v.id = ?"
                args = (version_id,)
            else:
                sql += "WHERE p.id = (SELECT value FROM settings WHERE key = 'default_persona_id') "
                sql += "ORDER BY v.revision DESC LIMIT 1"
                args = ()
            cursor = await db.execute(sql, args)
            profile = self._profile(await cursor.fetchone())
            if profile is None:
                raise ValueError("Persona version not found")
            return profile

    async def for_generation(self, config, generation_id=""):
        if generation_id:
            async with self.state._connect() as db:
                cursor = await db.execute(
                    "SELECT persona_json FROM email_generations WHERE id = ?", (generation_id,),
                )
                row = await cursor.fetchone()
                if row:
                    return json.loads(row[0])
        return await self.resolve(config)

    async def save(self, data: dict, persona_id=""):
        """Serialize version increments with default/archive changes."""
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if persona_id:
                cursor = await db.execute(
                    "SELECT p.*, v.revision, v.tone, v.instructions, v.examples "
                    "FROM personas p JOIN persona_versions v ON p.id = v.persona_id "
                    "WHERE p.id = ? ORDER BY v.revision DESC LIMIT 1", (persona_id,),
                )
                old = await cursor.fetchone()
                if old is None:
                    raise ValueError("Persona not found")
                if old["archived"]:
                    raise ValueError("Restore the persona before editing it")
                if data.get("expected_revision") != old["revision"]:
                    raise ValueError("This persona changed. Reload it before saving")
                changed = any(data[key] != old[key] for key in ("tone", "instructions", "examples"))
                revision = old["revision"] + int(changed)
                await db.execute(
                    "UPDATE personas SET name = ?, description = ?, avatar_seed = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (data["name"], data["description"], data["avatar_seed"], persona_id),
                )
            else:
                persona_id, revision, changed = uuid.uuid4().hex, 1, True
                await db.execute(
                    "INSERT INTO personas (id, name, description, avatar_seed) VALUES (?, ?, ?, ?)",
                    (persona_id, data["name"], data["description"], data["avatar_seed"]),
                )
            if changed:
                await db.execute(
                    "INSERT INTO persona_versions (id, persona_id, revision, tone, instructions, examples) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, persona_id, revision, data["tone"], data["instructions"], data["examples"]),
                )
            await db.commit()
        return persona_id

    async def set_default(self, persona_id):
        async with self.state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT archived FROM personas WHERE id = ?", (persona_id,))
            row = await cursor.fetchone()
            if row is None or row[0]:
                raise ValueError("Choose an active persona")
            await db.execute(
                "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE key = 'default_persona_id'", (persona_id,),
            )
            await db.commit()

    async def archive(self, persona_id, archived):
        async with self.state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'default_persona_id'")
            row = await cursor.fetchone()
            if archived and row and row[0] == persona_id:
                raise ValueError("Choose another default before archiving this persona")
            cursor = await db.execute(
                "UPDATE personas SET archived = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(archived), persona_id),
            )
            if not cursor.rowcount:
                raise ValueError("Persona not found")
            await db.commit()

    async def versions(self, persona_id):
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM persona_versions WHERE persona_id = ? ORDER BY revision DESC",
                (persona_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def record(self, profile, config, prompt, output, task, instruction="", json_mode=True):
        generation_id = uuid.uuid4().hex
        async with self.state._connect() as db:
            await db.execute(
                "INSERT INTO email_generations (id, persona_version_id, persona_json, config_json, "
                "prompt, output_json, task, instruction) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (generation_id, profile["version_id"], json.dumps(profile, ensure_ascii=False),
                 json.dumps(generation_config(config), ensure_ascii=False),
                 prompt + (JSON_INSTRUCTION if json_mode else ""),
                 json.dumps(output, ensure_ascii=False), task, instruction),
            )
            await db.commit()
        return generation_id

    async def replace_draft(self, item_id, draft):
        """Attach a regeneration atomically, refusing mail that left the review queue."""
        async with self.state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE outbox SET subject = ?, body = ?, generation_id = ?, manually_edited = 0, "
                "status = 'pending_review', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('pending_review', 'approved')",
                (draft["subject"], draft["body"], draft.get("generation_id", ""), item_id),
            )
            if not cursor.rowcount:
                raise ValueError("This email is no longer available for regeneration")
            if draft.get("generation_id"):
                await db.execute(
                    "INSERT INTO email_generation_history "
                    "(outbox_id, generation_id, original_subject, original_body) VALUES (?, ?, ?, ?)",
                    (item_id, draft["generation_id"], draft["subject"], draft["body"]),
                )
            await db.commit()

    async def history(self, item_id):
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT g.*, h.original_subject, h.original_body, h.attached_at "
                "FROM email_generation_history h JOIN email_generations g ON g.id = h.generation_id "
                "WHERE h.outbox_id = ? ORDER BY h.rowid DESC", (item_id,),
            )
            result = []
            for row in await cursor.fetchall():
                item = dict(row)
                for key, target in (("persona_json", "persona"), ("config_json", "config"), ("output_json", "output")):
                    item[target] = json.loads(item.pop(key))
                result.append(item)
            return result

    async def enrich(self, rows):
        ids = {row.get("generation_id") for row in rows if row.get("generation_id")}
        profiles = {}
        if ids:
            async with self.state._connect() as db:
                cursor = await db.execute(
                    "SELECT id, persona_json FROM email_generations WHERE id IN (" + ",".join("?" for _ in ids) + ")",
                    tuple(ids),
                )
                profiles = {row[0]: json.loads(row[1]) for row in await cursor.fetchall()}
        for row in rows:
            row["writing_persona"] = profiles.get(row.get("generation_id"))
        return rows
