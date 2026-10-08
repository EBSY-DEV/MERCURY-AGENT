"""Demo bookkeeping commands for the dashboard and the CLI.

A demo is named by its id, by its contact (id or email), or by a queued
outbox row of that contact, so a person at a terminal can type an email
address and the Outbox can pass the row it shows. Every failure is a
DemoError carrying a stable code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mercury.control.errors import ControlError
from mercury.demos import offers_by_key, retire_after_days, waiting_for_demo


class DemoError(ControlError):
    """Codes: not_found, ambiguous, invalid, unknown_offer, retired, no_config."""

    def __init__(self, code: str, message: str):
        super().__init__(message, code)


class Artifacts(BaseModel):
    """What was built. All optional: a demo can be marked ready first and
    the recording attached later."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    demo_url: str | None = Field(default=None, max_length=500)
    recording_path: str | None = Field(default=None, max_length=500)
    agent_id: str | None = Field(default=None, max_length=200)
    built_by: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("demo_url")
    @classmethod
    def _url(cls, v):
        if v and not v.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v

    @field_validator("recording_path", "agent_id", "built_by")
    @classmethod
    def _one_line(cls, v):
        if v and ("\n" in v or "\r" in v):
            raise ValueError("must be a single line")
        return v


def artifacts(data: dict) -> dict:
    try:
        return Artifacts(**data).model_dump(exclude_none=True)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "input"
        raise DemoError("invalid", f"{field}: {first['msg']}") from error


class DemoService:
    def __init__(self, state, config):
        # config None: mercury.yaml could not be read. Listing still works
        # (everything with an offer shows as held); changes are refused.
        self.state, self.config = state, config

    async def ready(self):
        await self.state.init_db()
        return self

    def _offers(self) -> dict:
        if self.config is None:
            raise DemoError("no_config", "Mercury could not read mercury.yaml, so it can't check offers.")
        return offers_by_key(self.config)

    async def overview(self, include_retired: bool = False) -> dict:
        demos = await self.state.list_demos()
        if not include_retired:
            demos = [d for d in demos if d["status"] != "retired"]
        offers = offers_by_key(self.config) if self.config is not None else {}
        return {
            "waiting": await waiting_for_demo(self.state, self.config),
            "demos": demos,
            "offers": [{"key": o.key, "requires_demo": o.requires_demo, "demo_kind": o.demo_kind}
                       for o in offers.values()],
            "retire_after_days": retire_after_days(self.config) if self.config is not None else None,
        }

    async def _prospect(self, ref: str):
        """A contact by id or email, or the contact of an outbox row."""
        prospect = await self.state.get_prospect(ref)
        if prospect is None and "@" in ref:
            prospect = await self.state.get_prospect_by_email(ref)
        if prospect is None:
            item = await self.state.get_outbox_item(ref)
            if item and item.get("prospect_id"):
                prospect = await self.state.get_prospect(item["prospect_id"])
        return prospect

    async def find(self, ref: str, offer_key: str = "") -> tuple[dict | None, str, str]:
        """(live demo or None, prospect id, offer key) for a reference.

        Without an offer the contact's only live demo is used, else the only
        offer that needs a demo, else the offer of its queued emails."""
        ref = (ref or "").strip()
        offer_key = (offer_key or "").strip().lower()
        if not ref:
            raise DemoError("invalid", "Name a demo, a contact (id or email) or an outbox email")
        demo = await self.state.get_demo(ref)
        if demo is None and len(ref) >= 4 and "@" not in ref:
            matches = [d for d in await self.state.list_demos(limit=5000) if d["id"].startswith(ref)]
            if len(matches) > 1:
                raise DemoError("ambiguous", f"'{ref}' matches {len(matches)} demos. Use the full id")
            demo = matches[0] if matches else None
        if demo is not None:
            if offer_key and offer_key != demo["offer_key"]:
                raise DemoError("invalid", f"Demo {demo['id']} is for the {demo['offer_key']} offer")
            return demo, demo["prospect_id"], demo["offer_key"]

        item = await self.state.get_outbox_item(ref)
        if item is not None and not offer_key:
            offer_key = await self.state.outbox_offer_key(item)
        prospect = await self._prospect(ref)
        if prospect is None:
            raise DemoError("not_found", f"No demo, contact or outbox email matches '{ref}'")
        if not offer_key:
            live = [d for d in await self.state.list_demos(prospect_id=prospect.id)
                    if d["status"] != "retired"]
            queued = {r["offer"] for r in await self.state.queued_offer_outbox()
                      if r.get("prospect_id") == prospect.id}
            demo_offers = [k for k, o in offers_by_key(self.config).items() if o.requires_demo]
            for candidates in ({d["offer_key"] for d in live}, queued, set(demo_offers)):
                if len(candidates) == 1:
                    offer_key = next(iter(candidates))
                    break
                if len(candidates) > 1:
                    raise DemoError("ambiguous", f"{prospect.email} has more than one offer "
                                    f"({', '.join(sorted(candidates))}). Say which with the offer key")
        if not offer_key:
            raise DemoError("invalid", "Which offer? No offer in mercury.yaml needs a demo")
        return await self.state.find_live_demo(prospect.id, offer_key), prospect.id, offer_key

    def _offer(self, offer_key: str):
        offer = self._offers().get(offer_key)
        if offer is None:
            raise DemoError("unknown_offer", f"The {offer_key} offer is not in mercury.yaml (offers)")
        return offer

    async def request(self, ref: str, offer_key: str = "") -> dict:
        """Register a demo to build. A live one is returned as it is."""
        demo, prospect_id, offer_key = await self.find(ref, offer_key)
        if demo is None:
            offer = self._offer(offer_key)
            demo_id, _ = await self.state.request_demo(prospect_id, offer_key, offer.demo_kind)
            demo = await self.state.get_demo(demo_id)
        return demo

    async def mark_ready(self, ref: str, offer_key: str = "", **fields) -> dict:
        """Mark a demo ready (registering it first if needed) and attach what
        was built. The held emails go out on the next heartbeat."""
        data = artifacts(fields)
        demo, prospect_id, offer_key = await self.find(ref, offer_key)
        if demo is not None and demo["status"] == "retired":
            raise DemoError("retired", "That demo is retired. Mark the contact's new demo ready instead")
        if demo is None:
            offer = self._offer(offer_key)
            demo_id, _ = await self.state.request_demo(prospect_id, offer_key, offer.demo_kind)
        else:
            self._offer(offer_key)
            demo_id = demo["id"]
        if not await self.state.mark_demo_ready(demo_id, **data):
            raise DemoError("retired", "That demo was retired meanwhile")
        await self.state.log_action("demo_ready", "user", {"demo_id": demo_id, "offer": offer_key,
                                                           "prospect_id": prospect_id})
        return await self.state.get_demo(demo_id)

    async def retire(self, ref: str, offer_key: str = "", reason: str = "") -> dict:
        demo, _prospect_id, offer_key = await self.find(ref, offer_key)
        if demo is None:
            raise DemoError("not_found", f"No live {offer_key} demo for '{ref}'")
        if demo["status"] == "retired":
            raise DemoError("retired", "That demo is already retired")
        await self.state.retire_demo(demo["id"], (reason or "retired by hand").strip()[:200])
        await self.state.log_action("demo_retired", "user", {"demo_id": demo["id"], "offer": offer_key})
        return await self.state.get_demo(demo["id"])
