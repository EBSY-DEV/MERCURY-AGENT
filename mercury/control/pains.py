"""Pain library commands for the dashboard and the CLI.

Mercury and the trainer propose pains; only the person at the keyboard
confirms or rejects them. Every change runs through ``run_command`` so it is
audited with who made it, and an edit can carry the revision the editor was
looking at, so two editors cannot overwrite each other.

Failures raise a ControlError with a stable code: not_found, invalid,
duplicate, matches_rejected, stale_revision.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mercury.control.audit import run_command
from mercury.control.errors import Conflict, Invalid, NotFound
from mercury.pains import CODE_RE, derive_code, find_match, normalize_code

STATUSES = ("proposed", "confirmed", "rejected")
EDITABLE = ("label", "market", "sector", "owner_words", "scene", "cost",
            "signal_codes", "offer_key", "evidence", "avoid_terms")


def _short_list(values: list[str], limit: int, size: int) -> list[str]:
    seen, out = set(), []
    for value in values:
        value = value.strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value[:size])
    return out[:limit]


class PainInput(BaseModel):
    """A pain as a person writes it. On an edit only the fields sent change."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(default="", max_length=48)
    label: str = Field(default="", max_length=200)
    market: str = Field(default="", max_length=80)
    sector: str = Field(default="", max_length=80)
    owner_words: str = Field(default="", max_length=600)
    scene: str = Field(default="", max_length=400)
    cost: str = Field(default="", max_length=300)
    signal_codes: list[str] = Field(default_factory=list, max_length=20)
    offer_key: str = Field(default="", max_length=80)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    avoid_terms: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("scene")
    @classmethod
    def _scene_is_short(cls, v):
        if len([line for line in v.splitlines() if line.strip()]) > 2:
            raise ValueError("keep the scene to one or two lines")
        return v

    @field_validator("offer_key")
    @classmethod
    def _offer_key(cls, v):
        v = v.lower()
        if v and not all(ch.isalnum() or ch in "_-" for ch in v):
            raise ValueError("an offer key is letters, digits, '-' or '_'")
        return v

    @field_validator("signal_codes")
    @classmethod
    def _signals(cls, v):
        return _short_list([s.upper() for s in v], 20, 60)

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, v):
        return _short_list(v, 10, 300)

    @field_validator("avoid_terms")
    @classmethod
    def _avoid(cls, v):
        return _short_list(v, 20, 80)


def validated(data: dict) -> dict:
    try:
        return PainInput(**data).model_dump()
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "input"
        raise Invalid(f"{field}: {first['msg']}") from error


class PainService:
    def __init__(self, state, ctx):
        self.state, self.ctx = state, ctx

    async def ready(self):
        await self.state.init_db()
        return self

    def _actor(self) -> str:
        return f"{self.ctx.client}:{self.ctx.operator}"

    async def _run(self, action: str, scope: str, params: dict, work, object_id: str = ""):
        return await run_command(self.state, self.ctx, f"pains.{action}", scope=scope,
                                 params=params, work=work, object_type="pain",
                                 object_id=object_id)

    # ── Reading ──

    async def _public(self, pains: list[dict]) -> list[dict]:
        stats = await self.state.pain_stats()
        empty = {"sends": 0, "prospects": 0, "replies": 0, "positive": 0}
        out = []
        for pain in pains:
            counts = {**empty, **stats.get(pain["code"], {})}
            counts["reply_rate"] = (round(counts["replies"] / counts["prospects"], 4)
                                    if counts["prospects"] else None)
            out.append({**{k: v for k, v in pain.items() if k != "origin_text"}, "stats": counts})
        return out

    async def list(self, status: str = "", market: str | None = None,
                   offer_key: str | None = None) -> dict:
        """The pains (optionally filtered) and the whole library's tally."""
        self.ctx.require("read")
        if status and status not in STATUSES:
            raise Invalid(f"status must be one of {', '.join(STATUSES)}")
        everything = await self.state.list_pains()
        summary = {s: sum(1 for p in everything if p["status"] == s) for s in STATUSES}
        summary["total"] = len(everything)
        rows = await self.state.list_pains(status or None, market, offer_key)
        return {"pains": await self._public(rows), "summary": summary}

    async def get(self, code: str) -> dict:
        self.ctx.require("read")
        pain = await self.state.get_pain(code)
        if not pain:
            raise NotFound(f"No pain {code!r}.")
        return (await self._public([pain]))[0]

    async def vocabulary(self) -> list[dict]:
        """The signal codes a pain may name (the governed vocabulary)."""
        from mercury.signals import seed_signal_catalog
        await seed_signal_catalog(self.state)
        return [{"code": s["code"], "label": s["label"], "status": s["status"]}
                for s in await self.state.get_signal_codes()]

    async def _check_signals(self, codes: list[str]) -> None:
        known = {s["code"] for s in await self.vocabulary()}
        unknown = [c for c in codes if c not in known]
        if unknown:
            raise Invalid(f"unknown signal code(s): {', '.join(unknown)}",
                          code="unknown_signal", unknown=unknown)

    # ── Changing ──

    async def add(self, data: dict, *, confirm: bool = False, note: str = "") -> dict:
        """Add a pain by hand. It starts 'proposed' unless ``confirm`` says
        the person adding it is also deciding it."""
        fields = validated(data)
        if not (fields["label"] or fields["owner_words"]):
            raise Invalid("a pain needs a label or the owner's words")
        label = fields["label"] or fields["owner_words"]
        code = normalize_code(fields["code"]) or derive_code(label)

        async def work(trail):
            if not CODE_RE.match(code):
                raise Invalid("a code is capital letters, digits and underscores, e.g. PAIN_MISSED_CALLS")
            await self._check_signals(fields["signal_codes"])
            known = await self.state.list_pains()
            match = find_match(" ".join([fields["label"], fields["owner_words"]]).strip(), known, code)
            if match and match["code"] == code:
                raise Conflict(f"{code} already exists", code="duplicate", matched=code)
            if match and match["status"] == "rejected":
                raise Conflict(f"This repeats {match['code']}, which was rejected. Confirm that one "
                               "again if you changed your mind.", code="matches_rejected",
                               matched=match["code"])
            status = "confirmed" if confirm else "proposed"
            added = await self.state.add_pain(
                code, **{k: fields[k] for k in EDITABLE}, origin_text=label,
                source="manual", status=status,
                status_by=self._actor() if confirm else "", status_note=note)
            if not added:
                raise Conflict(f"{code} already exists", code="duplicate", matched=code)
            trail.record(code, None, 1, status=status)
            return await self.get(code)

        return await self._run("add", "edit", {"code": code, **fields, "confirm": confirm},
                               work, object_id=code)

    async def edit(self, code: str, data: dict, expected_revision: int | None = None) -> dict:
        """Change the fields sent. The status never changes here."""
        sent = {k: v for k, v in validated(data).items()
                if k in EDITABLE and k in {n for n in data}}
        if "code" in data and normalize_code(data["code"]) not in ("", normalize_code(code)):
            raise Invalid("a pain's code cannot change")
        if not sent:
            raise Invalid("nothing to change")
        code = normalize_code(code)

        async def work(trail):
            current = await self.state.get_pain(code)
            if not current:
                raise NotFound(f"No pain {code!r}.")
            if "signal_codes" in sent:
                await self._check_signals(sent["signal_codes"])
            if expected_revision is not None and current["revision"] != expected_revision:
                raise Conflict(f"{code} changed since revision {expected_revision} "
                               f"(now {current['revision']}); reload it", code="stale_revision",
                               revision=current["revision"])
            revision = await self.state.update_pain(code, sent, expected_revision)
            if revision is None:
                raise Conflict(f"{code} changed while you edited it; reload it",
                               code="stale_revision")
            trail.record(code, current["revision"], revision, fields=sorted(sent))
            return await self.get(code)

        return await self._run("edit", "edit", {"code": code, **sent, "rev": expected_revision},
                               work, object_id=code)

    async def set_status(self, codes: list[str], status: str, note: str = "",
                         expected_revisions: dict | None = None) -> dict:
        """Confirm, reject or reopen pains. Only a person does this: the
        actor is the client and operator running the command."""
        if status not in STATUSES:
            raise Invalid(f"status must be one of {', '.join(STATUSES)}")
        codes = [normalize_code(c) for c in codes if normalize_code(c)]
        if not codes:
            raise Invalid("no pain codes given")
        note = (note or "").strip()[:300]

        async def work(trail):
            changed, unknown, stale = [], [], []
            for code in codes:
                current = await self.state.get_pain(code)
                if not current:
                    unknown.append(code)
                    continue
                want = (expected_revisions or {}).get(code)
                if not await self.state.set_pain_status(code, status, self._actor(), note, want):
                    stale.append(code)
                    continue
                changed.append(code)
                trail.record(code, current["revision"], current["revision"] + 1,
                             status_before=current["status"], status=status)
            return {"changed": len(changed), "codes": changed, "unknown": unknown,
                    "stale": stale, "status": status}

        return await self._run("status", "approve",
                               {"codes": codes, "status": status, "note": note}, work,
                               object_id=",".join(codes)[:200])
