"""Which voice each sending mailbox writes in, and the name it signs with.

The sign-off name belongs to the mailbox: harvey@ signs "Harvey" whatever
voice it writes in. A mailbox that sets none (a shared hello@) uses the
persona's suggested sign-off name, then the sender name in mercury.yaml.

When every mailbox that can start a thread would write the same way, nothing
is pinned and the sender rotates at send time as before. When they differ,
each new thread gets its mailbox when it is written, so the voice, the
sign-off and the From address always match.
"""

import aiosqlite

from mercury.personas import PersonaError, PersonaStore


def configured_mailboxes(config, env=None) -> list[dict]:
    """The sending mailboxes, in rotation order, with the name their From shows."""
    email_cfg = config.channels.email
    fallback = config.persona.name
    listed = list(getattr(email_cfg, "mailboxes", None) or [])
    if (getattr(email_cfg, "provider", "") or "").lower() == "smtp" and listed:
        return [{"email": m.email.strip().lower(), "from_name": m.name or fallback,
                 "enabled": bool(getattr(m, "enabled", True)), "daily_cap": int(m.daily_cap or 0)}
                for m in listed]
    email = (config.persona.email or getattr(env, "smtp_username", "") or "").strip().lower()
    return [{"email": email, "from_name": fallback, "enabled": True,
             "daily_cap": int(getattr(email_cfg, "max_daily_sends", 0) or 0)}] if email else []


class MailboxVoices:
    def __init__(self, state, config, env=None):
        self.state, self.config, self.env = state, config, env
        self.personas = PersonaStore(state)

    async def _stored(self) -> dict:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT email, persona_id, sign_name FROM mailbox_voices")
            return {row["email"]: dict(row) for row in await cursor.fetchall()}

    async def assignments(self) -> list[dict]:
        """Every configured mailbox with its voice and the name it signs with."""
        await self.personas.ensure_default(self.config)
        stored = await self._stored()
        personas = {p["id"]: p for p in await self.personas.list()}
        default_id = await self.state.get_setting("default_persona_id")
        result = []
        for mailbox in configured_mailboxes(self.config, self.env):
            row = stored.get(mailbox["email"], {})
            chosen = row.get("persona_id") or ""
            # An assigned persona that was archived or deleted falls back to the
            # default, and the mailbox reports that it follows the default so an
            # edit saved from the UI or CLI does not re-post the stale id.
            if chosen and personas.get(chosen, {}).get("archived", True):
                chosen = ""
            persona = personas[chosen] if chosen else personas[default_id]
            sign_name = row.get("sign_name") or ""
            suggested = persona.get("sign_name") or self.config.persona.name
            result.append(mailbox | {"persona_id": chosen, "follows_default": not chosen,
                                     "persona": persona, "sign_name": sign_name, "suggested": suggested,
                                     "signer": sign_name or suggested})
        return result

    async def assign(self, email: str, persona_id: str = "", sign_name: str = "") -> dict:
        email = (email or "").strip().lower()
        mailbox = next((m for m in await self.assignments() if m["email"] == email), None)
        if not mailbox:
            raise PersonaError("not_found", f"{email or 'That mailbox'} is not a configured sending mailbox")
        sign_name = (sign_name or "").strip()
        if len(sign_name) > 80 or "\n" in sign_name or "\r" in sign_name:
            raise PersonaError("invalid", "A sign-off name is one line, up to 80 characters")
        if persona_id:
            persona = next((p for p in await self.personas.list() if p["id"] == persona_id), None)
            if not persona or persona["archived"]:
                raise PersonaError("invalid", "Choose an active persona")
        async with self.state._connect() as db:
            await db.execute(
                "INSERT INTO mailbox_voices (email, persona_id, sign_name) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET persona_id = excluded.persona_id, "
                "sign_name = excluded.sign_name, updated_at = CURRENT_TIMESTAMP",
                (email, persona_id, sign_name),
            )
            await db.commit()
        return next(m for m in await self.assignments() if m["email"] == email)

    async def profile_for(self, email: str = "") -> dict:
        """The persona snapshot a draft from this mailbox is written with,
        carrying the sign-off name. Unknown or empty: the default voice."""
        mailboxes = await self.assignments()
        mailbox = next((m for m in mailboxes if m["email"] == (email or "").lower()), None)
        if mailbox is None:
            profile = await self.personas.resolve(self.config)
            return profile | {"signer": profile.get("sign_name") or self.config.persona.name, "mailbox": ""}
        profile = await self.personas.resolve(self.config, mailbox["persona"]["version_id"])
        return profile | {"signer": mailbox["signer"], "mailbox": mailbox["email"]}

    async def plan(self) -> dict:
        """Whether new threads need a mailbox chosen when they are written.

        Only mailboxes that accept new threads count. If they all share one
        voice version and one signer, rotation stays at send time.
        """
        eligible = [m for m in await self.assignments() if m["enabled"]]
        identities = {(m["persona"]["version_id"], m["signer"]) for m in eligible}
        if len(identities) <= 1:
            profile = await self.profile_for(eligible[0]["email"] if eligible else "")
            return {"pinned": False, "profile": profile | {"mailbox": ""}, "mailboxes": eligible}
        return {"pinned": True, "profile": None, "mailboxes": eligible}

    async def spread(self, count: int, mailboxes: list[dict]) -> list[str]:
        """Choose a mailbox for each of `count` new threads, filling the one
        with the most room first: fewest unsent first emails per daily send."""
        async with self.state._connect() as db:
            cursor = await db.execute(
                "SELECT mailbox, COUNT(*) FROM outbox WHERE step = 1 AND mailbox != '' "
                "AND status IN ('pending_review', 'approved') GROUP BY mailbox")
            load = {row[0]: row[1] for row in await cursor.fetchall()}
        picks = []
        for _ in range(count):
            mailbox = min(mailboxes, key=lambda m: (load.get(m["email"], 0) / max(m["daily_cap"], 1), mailboxes.index(m)))
            load[mailbox["email"]] = load.get(mailbox["email"], 0) + 1
            picks.append(mailbox["email"])
        return picks
