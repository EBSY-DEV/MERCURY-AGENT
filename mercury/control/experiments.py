"""A/B experiment commands for the dashboard and the CLI.

Create a draft, preview it, start it (which freezes its revision), pause
and resume enrollment, hold and release its unsent mail, complete it, and
read results. Effects on mail (see mercury/experiments.py):

* pause      stops new enrollment. Prospects already assigned are still
             written in their arm, and their approved mail still sends.
* hold       stops every unsent sequence email of the experiment at the send
             claim. Nothing is unapproved, rejected or cancelled; released,
             each email goes out on its schedule. Review, exclusions, pauses
             and company holds apply exactly as before.
* complete   ends enrollment for good. Enrolled sequences finish unless held,
             and results keep maturing.

An edit to the name, hypothesis, enrollment end or cap applies in place. A
substantive edit (variable, arms, cohort, split, window, minimum, metric)
rewrites a draft, and on a started experiment creates the next revision,
frozen at once: new enrollments go to it, earlier ones stay where they were.

Experiments are referenced by id, exact name (any case) or a unique id
prefix. Failures raise ControlError subclasses with stable codes:
not_found, invalid, not_editable, stale_version, confirmation_required,
ambiguous.
"""

from __future__ import annotations

import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mercury import experiments as ex
from mercury.control.audit import run_command
from mercury.control.errors import Conflict, Invalid, NotFound

NAME_MAX, HYPOTHESIS_MAX, ARM_NAME_MAX, INSTRUCTION_MAX = 80, 1000, 60, 2000
IN_PLACE = ("name", "hypothesis", "enroll_until", "max_enrolled")
SUBSTANTIVE = ("variable", "arms", "cohort", "allocation_a", "response_window_days",
               "min_per_arm", "min_duration_days", "primary_metric")
EFFECTS = {
    "pause": "Stops new enrollment. Prospects already in the experiment are still written "
             "in their arm, and their approved mail still sends.",
    "hold": "Stops every unsent email of this experiment at the send step. Nothing is "
            "unapproved or cancelled; on release each email goes out on its schedule.",
    "complete": "Ends enrollment for good. Enrolled sequences finish unless their mail is "
                "held, and results keep maturing.",
}


class ArmInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = ""
    name: str = Field(default="", max_length=ARM_NAME_MAX)
    instruction: str = Field(default="", max_length=INSTRUCTION_MAX)
    persona_id: str = ""


class ExperimentInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=NAME_MAX)
    hypothesis: str = Field(default="", max_length=HYPOTHESIS_MAX)
    variable: str
    arms: list[ArmInput] = Field(min_length=2, max_length=2)
    cohort: dict = Field(default_factory=dict)
    allocation_a: int = Field(default=50, ge=1, le=99)
    enroll_until: str | None = None
    max_enrolled: int = Field(default=0, ge=0)
    response_window_days: int = Field(ge=1, le=90)
    min_per_arm: int = Field(ge=1, le=100000)
    min_duration_days: int = Field(default=0, ge=0, le=365)
    primary_metric: str = "positive_reply_rate"


def _invalid(message: str, field: str = "") -> Invalid:
    return Invalid(message, code="invalid", **({"field": field} if field else {}))


def _until(value) -> str | None:
    """An enrollment end as stored: a date means the end of that day (UTC)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) == 10:
        text += "T23:59:59"
    when = ex.parse_ts(text)
    if when is None:
        raise _invalid("enroll_until must be a date (YYYY-MM-DD) or an ISO time", "enroll_until")
    return ex.ts(when)


class ExperimentService:
    def __init__(self, ctx, state, config, clock=ex.utcnow):
        self.ctx, self.state, self.config, self.clock = ctx, state, config, clock

    async def ready(self):
        await self.state.init_db()
        return self

    # ── Definitions ──

    def defaults(self) -> dict:
        return {"allocation_a": 50,
                "response_window_days": int(ex.setting(self.config, "response_window_days")),
                "min_per_arm": int(ex.setting(self.config, "min_per_arm")),
                "min_duration_days": int(ex.setting(self.config, "min_duration_days")),
                "primary_metric": "positive_reply_rate",
                "confidence_threshold": float(ex.setting(self.config, "confidence_threshold"))}

    def options(self) -> dict:
        return {"variables": [{"key": k, "label": v} for k, v in ex.VARIABLES.items()],
                "primary_metrics": [{"key": k, "label": v} for k, v in ex.PRIMARY_METRICS.items()],
                "outcome_labels": [{"key": k, "description": ex.OUTCOME_DESCRIPTIONS[k],
                                    "positive": k in ex.POSITIVE_LABELS}
                                   for k in ex.OUTCOME_LABELS],
                "cohort_filters": list(ex.COHORT_LISTS) + ["min_score"],
                "defaults": self.defaults(), "effects": EFFECTS}

    async def _personas(self) -> dict:
        from mercury.personas import PersonaStore

        store = PersonaStore(self.state)
        await store.ensure_default(self.config)
        return {p["id"]: p for p in await store.list()}

    async def validate(self, data: dict) -> dict:
        """A complete definition, checked: two arms that differ in exactly the
        one variable under test, known personas, a known metric."""
        data = {**self.defaults(), **{k: v for k, v in data.items() if v is not None}}
        data.pop("confidence_threshold", None)
        try:
            clean = ExperimentInput(**data).model_dump()
        except ValidationError as error:
            first = error.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "input"
            raise _invalid(f"{field}: {first['msg']}", field) from error
        if clean["variable"] not in ex.VARIABLES:
            raise _invalid(f"variable must be one of {', '.join(ex.VARIABLES)}", "variable")
        if clean["primary_metric"] not in ex.PRIMARY_METRICS:
            raise _invalid(f"primary_metric must be one of {', '.join(ex.PRIMARY_METRICS)}",
                           "primary_metric")
        try:
            clean["cohort"] = ex.normalize_cohort(clean["cohort"])
        except ValueError as error:
            raise _invalid(str(error), "cohort") from error
        clean["enroll_until"] = _until(clean["enroll_until"])
        arms = clean["arms"]
        keys = [a["key"].upper() for a in arms]
        if keys == ["", ""]:
            keys = list(ex.ARM_KEYS)
        if sorted(keys) != list(ex.ARM_KEYS):
            raise _invalid("arms are A and B", "arms")
        for arm, key in zip(arms, keys):
            arm["key"] = key
            arm["name"] = arm["name"] or f"Variant {key}"
        arms.sort(key=lambda a: a["key"])
        personas = await self._personas()
        default_id = await self.state.get_setting("default_persona_id")
        for arm in arms:
            if arm["persona_id"]:
                persona = personas.get(arm["persona_id"])
                if persona is None or persona["archived"]:
                    raise _invalid(f"Arm {arm['key']}: choose an active persona", "arms")
        a, b = arms
        persona_a, persona_b = (a["persona_id"] or default_id), (b["persona_id"] or default_id)
        if clean["variable"] == "persona":
            if persona_a == persona_b:
                raise _invalid("A persona experiment needs a different persona in each arm", "arms")
            if a["instruction"] != b["instruction"]:
                raise _invalid("Compare one variable at a time: a persona experiment gives both "
                               "arms the same instruction", "arms")
        else:
            if persona_a != persona_b:
                raise _invalid("Compare one variable at a time: both arms use the same persona "
                               "unless the persona is what you test", "arms")
            if a["instruction"] == b["instruction"]:
                raise _invalid("The arms need different instructions for the "
                               f"{ex.VARIABLES[clean['variable']].lower()}", "arms")
        return clean

    # ── Reads ──

    async def find(self, ref: str) -> dict:
        ref = (ref or "").strip()
        if not ref:
            raise _invalid("Name an experiment")
        async with self.state._connect() as db:
            rows = await ex._rows(db, "SELECT id, name FROM experiments")
        for match in (lambda r: r["id"] == ref,
                      lambda r: r["name"].casefold() == ref.casefold(),
                      lambda r: len(ref) >= 4 and r["id"].startswith(ref)):
            found = [r for r in rows if match(r)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise Invalid(f"'{ref}' matches {len(found)} experiments. Use the id instead",
                              code="ambiguous")
        raise NotFound(f"No experiment called '{ref}'")

    async def _loaded(self, ref: str, revision: int | None = None) -> dict:
        found = await self.find(ref)
        loaded = await ex.load(self.state, found["id"], revision)
        if loaded is None:
            raise NotFound(f"Revision {revision} of '{found['name']}' does not exist")
        return loaded

    async def _queued(self, experiment_id: str) -> dict:
        async with self.state._connect() as db:
            rows = await ex._rows(
                db, "SELECT status, COUNT(*) AS n FROM outbox WHERE experiment_id = ? "
                "AND kind = 'sequence' AND status IN ('pending_review', 'approved', 'blocked', "
                "'sending') GROUP BY status", (experiment_id,))
        counts = {r["status"]: r["n"] for r in rows}
        return {"pending_review": counts.get("pending_review", 0),
                "approved": counts.get("approved", 0), "blocked": counts.get("blocked", 0),
                "sending": counts.get("sending", 0),
                "unsent": sum(counts.values()) - counts.get("sending", 0)}

    def _setup_line(self, revision: dict, experiment: dict) -> str:
        a = int(revision["allocation_a"])
        parts = [ex.VARIABLES.get(revision["variable"], revision["variable"]),
                 f"{a}/{100 - a} split",
                 f"{revision['response_window_days']}-day response window",
                 f"{revision['min_per_arm']} mature per arm",
                 ex.PRIMARY_METRICS.get(revision["primary_metric"], revision["primary_metric"])]
        if experiment.get("enroll_until"):
            parts.append(f"enrolling until {experiment['enroll_until'][:10]}")
        if experiment.get("max_enrolled"):
            parts.append(f"at most {experiment['max_enrolled']} prospects")
        return " · ".join(parts)

    async def _public(self, loaded: dict) -> dict:
        experiment, revision, arms = loaded["experiment"], loaded["revision"], loaded["arms"]
        personas = await self._personas()
        status = experiment["status"]
        arm_list = []
        for key in ex.ARM_KEYS:
            arm = dict(arms.get(key) or {})
            persona = personas.get(arm.get("persona_id") or "")
            arm["persona_name"] = persona["name"] if persona else "Default voice"
            arm_list.append(arm)
        return {
            **experiment,
            "hold_mail": bool(experiment["hold_mail"]),
            "status_label": ex.STATUS_LABELS.get(status, status),
            "revision": revision["number"],
            "revision_id": revision["id"],
            "frozen": bool(revision["frozen_at"]),
            "variable": revision["variable"],
            "variable_label": ex.VARIABLES.get(revision["variable"], revision["variable"]),
            "allocation_a": revision["allocation_a"],
            "allocation_b": 100 - int(revision["allocation_a"]),
            "cohort": revision["cohort"],
            "cohort_description": ex.describe_cohort(revision["cohort"]),
            "response_window_days": revision["response_window_days"],
            "min_per_arm": revision["min_per_arm"],
            "min_duration_days": revision["min_duration_days"],
            "primary_metric": revision["primary_metric"],
            "primary_metric_label": ex.PRIMARY_METRICS.get(revision["primary_metric"], ""),
            "setup_line": self._setup_line(revision, experiment),
            "arms": arm_list,
            "revisions": loaded["revisions"],
            "controls": {
                "can_edit": status != "completed",
                "edit_creates_revision": status in ("running", "paused"),
                "can_start": status == "draft",
                "can_pause": status == "running",
                "can_resume": status == "paused",
                "can_complete": status in ("running", "paused"),
                "can_hold": status != "draft" and not experiment["hold_mail"],
                "can_release": bool(experiment["hold_mail"]),
                "effects": EFFECTS,
            },
            "queued": await self._queued(experiment["id"]),
        }

    async def list(self) -> dict:
        async with self.state._connect() as db:
            ids = [r["id"] for r in await ex._rows(
                db, "SELECT id FROM experiments ORDER BY CASE status WHEN 'running' THEN 0 "
                "WHEN 'paused' THEN 1 WHEN 'draft' THEN 2 ELSE 3 END, created_at DESC")]
        rows = []
        for experiment_id in ids:
            loaded = await ex.load(self.state, experiment_id)
            result = await ex.results(self.state, self.config, experiment_id, now=self.clock())
            public = await self._public(loaded)
            a, b = result["arms"]
            rows.append({
                "id": experiment_id, "name": public["name"], "status": public["status"],
                "status_label": public["status_label"], "hold_mail": public["hold_mail"],
                "revision": public["revision"], "variable": public["variable"],
                "variable_label": public["variable_label"],
                "primary_metric": result["primary_metric"],
                "primary_metric_label": result["primary_metric_label"],
                "enrolled": {"A": a["enrolled"], "B": b["enrolled"],
                             "total": a["enrolled"] + b["enrolled"]},
                "mature": {"A": a["mature"], "B": b["mature"]},
                "rate": {"A": a["primary"]["rate"], "B": b["primary"]["rate"]},
                "result": {"code": result["decision"]["code"],
                           "label": result["decision"]["label"]},
                "warnings": len(result["health"]["warnings"]),
                "created_at": public["created_at"], "started_at": public["started_at"],
                "completed_at": public["completed_at"],
            })
        return {"experiments": rows, "options": self.options()}

    async def get(self, ref: str, revision: int | None = None) -> dict:
        loaded = await self._loaded(ref, revision)
        return {"experiment": await self._public(loaded),
                "results": await ex.results(self.state, self.config, loaded["experiment"]["id"],
                                            loaded["revision"]["number"], now=self.clock())}

    async def results(self, ref: str, revision: int | None = None) -> dict:
        found = await self.find(ref)
        result = await ex.results(self.state, self.config, found["id"], revision, now=self.clock())
        if result is None:
            raise NotFound(f"Revision {revision} of '{found['name']}' does not exist")
        return result

    async def exposures(self, ref: str, revision: int | None = None, arm: str = "",
                        limit: int = 100, offset: int = 0) -> dict:
        found = await self.find(ref)
        return await ex.exposures(self.state, found["id"], revision_number=revision, arm=arm,
                                  limit=limit, offset=offset)

    async def _eligible(self) -> list[dict]:
        statuses = ["verified"]
        if getattr(getattr(getattr(self.config, "channels", None), "email", None),
                   "send_to_risky", False):
            statuses.append("risky")
        marks = ",".join("?" for _ in statuses)
        async with self.state._connect() as db:
            return await ex._rows(
                db, "SELECT * FROM prospects WHERE status = 'new' AND email != '' "
                f"AND email_status IN ({marks}) AND id NOT IN "
                "(SELECT prospect_id FROM experiment_assignments) ORDER BY created_at, id",
                statuses)

    async def preview(self, ref: str = "", definition: dict | None = None,
                      sample: int = 10, prompt_prospect: str = "") -> dict:
        """Who would be enrolled now and what each arm adds to the prompt. No
        model call, nothing enrolled. A saved experiment shows each sampled
        prospect's real arm; an unsaved definition shows the expected split.
        ``prompt_prospect`` (an id) adds each arm's full first-email prompt."""
        from mercury.personas import PersonaStore, voice_instructions

        experiment_id, number, frozen = "", 1, {}
        if ref:
            loaded = await self._loaded(ref)
            experiment = loaded["experiment"]
            experiment_id, number = experiment["id"], loaded["revision"]["number"]
            revision = loaded["revision"]
            clean = {"variable": revision["variable"], "allocation_a": revision["allocation_a"],
                     "cohort": revision["cohort"], "name": experiment["name"],
                     "arms": [{"key": k, "name": a["name"], "instruction": a["instruction"],
                               "persona_id": a["persona_id"]} for k, a in loaded["arms"].items()]}
            frozen = {k: a["persona_version_id"] for k, a in loaded["arms"].items()}
        else:
            clean = await self.validate(definition or {})
        matcher = await ex.cohort_matcher(self.state, clean["cohort"])
        eligible = [p for p in await self._eligible() if matcher(p)]
        allocation = int(clean["allocation_a"])
        if experiment_id:
            arms_of = {p["id"]: ex.arm_for_bucket(ex.assignment_bucket(experiment_id, number, p["id"]),
                                                  allocation) for p in eligible}
            expected = {k: sum(1 for v in arms_of.values() if v == k) for k in ex.ARM_KEYS}
        else:
            arms_of = {}
            a = round(len(eligible) * allocation / 100)
            expected = {"A": a, "B": len(eligible) - a}

        store = PersonaStore(self.state)
        await store.ensure_default(self.config)
        arms = []
        for arm in sorted(clean["arms"], key=lambda a: a["key"]):
            version_id = frozen.get(arm["key"]) or await self._latest_version(store, arm["persona_id"])
            profile = await store.resolve(self.config, version_id)
            profile = profile | {"experiment": {"instruction": arm["instruction"]}}
            entry = {"key": arm["key"], "name": arm["name"], "instruction": arm["instruction"],
                     "persona": {"id": profile["id"], "name": profile["name"],
                                 "version_id": profile["version_id"],
                                 "revision": profile["revision"], "tone": profile["tone"]},
                     "voice_block": voice_instructions(profile)}
            if prompt_prospect:
                entry["prompt"] = await self._prompt(prompt_prospect, profile)
            arms.append(entry)

        warnings = []
        if not eligible:
            warnings.append({"code": "no_eligible",
                             "message": "No new prospect with a deliverable address matches "
                                        "this cohort right now."})
        taken = await self._taken_by_others(eligible, experiment_id)
        for name, count in taken:
            warnings.append({"code": "overlap",
                             "message": f"{count} of these prospects also match '{name}', which "
                                        "started first and enrolls them before this one."})
        return {
            "experiment_id": experiment_id, "eligible": len(eligible), "expected": expected,
            "allocation": {"A": allocation, "B": 100 - allocation},
            "cohort_description": ex.describe_cohort(clean["cohort"]),
            "sample": [{"prospect_id": p["id"],
                        "name": f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                        "company": p.get("company") or "", "industry": p.get("industry") or "",
                        "arm": arms_of.get(p["id"], "")} for p in eligible[:max(0, int(sample))]],
            "arms": arms, "warnings": warnings,
            "note": "Preview makes no model call and enrolls no one. Prospects are assigned "
                    "when the Writer drafts for them.",
        }

    async def _prompt(self, prospect_id: str, profile: dict) -> str:
        from mercury.agents.writer import Writer
        from mercury.brain import Brain

        prospect = await self.state.get_prospect(prospect_id)
        if prospect is None:
            raise NotFound("Contact not found")
        writer = Writer(Brain(self.state), self.state, self.config)
        prompt, _profile = await writer.build_personal_prompt(prospect, "", profile)
        return prompt

    @staticmethod
    async def _latest_version(store, persona_id: str) -> str:
        """The version a revision would freeze: the persona's latest, or the
        default persona's latest when the arm names none."""
        if persona_id:
            versions = await store.versions(persona_id)
            if not versions:
                raise _invalid("That persona has no versions", "arms")
            return versions[0]["id"]
        return ""

    async def _taken_by_others(self, eligible: list[dict], experiment_id: str) -> list[tuple]:
        now = self.clock()
        async with self.state._connect() as db:
            running = await ex._rows(
                db, "SELECT e.*, r.cohort_json FROM experiments e JOIN experiment_revisions r "
                "ON r.experiment_id = e.id AND r.number = e.current_revision "
                "WHERE e.status = 'running' AND e.id != ? ORDER BY e.started_at, e.created_at, e.rowid",
                (experiment_id,))
        out = []
        for other in running:
            if not ex._enrollment_open(other, now):
                continue
            matcher = await ex.cohort_matcher(self.state, json.loads(other["cohort_json"] or "{}"))
            count = sum(1 for p in eligible if matcher(p))
            if count:
                out.append((other["name"], count))
        return out

    # ── Commands ──

    async def _run(self, action: str, scope: str, params: dict, work, object_id: str = ""):
        return await run_command(self.state, self.ctx, f"experiments.{action}", scope=scope,
                                 params=params, work=work, object_type="experiment",
                                 object_id=object_id)

    async def _write_revision(self, db, experiment_id: str, number: int, clean: dict,
                              versions: dict | None, now: str) -> str:
        """Insert a revision and its arms. With ``versions`` (from
        _pinned_versions) it is frozen at once."""
        revision_id = uuid.uuid4().hex[:12]
        await db.execute(
            "INSERT INTO experiment_revisions (id, experiment_id, number, variable, allocation_a, "
            "cohort_json, response_window_days, min_per_arm, min_duration_days, primary_metric, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, experiment_id, number, clean["variable"], clean["allocation_a"],
             json.dumps(clean["cohort"], sort_keys=True), clean["response_window_days"],
             clean["min_per_arm"], clean["min_duration_days"], clean["primary_metric"], now))
        for arm in clean["arms"]:
            await db.execute(
                "INSERT INTO experiment_arms (id, revision_id, arm_key, name, instruction, "
                "persona_id) VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:12], revision_id, arm["key"], arm["name"], arm["instruction"],
                 arm["persona_id"]))
        if versions is not None:
            await self._freeze(db, revision_id, versions, now)
        return revision_id

    async def _pinned_versions(self, persona_ids) -> dict:
        """{persona_id: the exact version a freeze pins}: the persona's latest,
        and for '' the default persona's latest. Read before the write
        transaction opens."""
        from mercury.personas import PersonaStore

        store = PersonaStore(self.state)
        pinned = {}
        for persona_id in set(persona_ids):
            pinned[persona_id] = (await self._latest_version(store, persona_id)
                                  or (await store.resolve(self.config))["version_id"])
        return pinned

    @staticmethod
    async def _freeze(db, revision_id: str, versions: dict, now: str) -> None:
        """Pin each arm to an exact persona version, then freeze the revision."""
        arms = await ex._rows(db, "SELECT * FROM experiment_arms WHERE revision_id = ?", (revision_id,))
        for arm in arms:
            await db.execute("UPDATE experiment_arms SET persona_version_id = ? WHERE id = ?",
                             (versions[arm["persona_id"] or ""], arm["id"]))
        await db.execute("UPDATE experiment_revisions SET frozen_at = ? WHERE id = ?",
                         (now, revision_id))

    async def create(self, data: dict) -> dict:
        clean = await self.validate(data)

        async def work(trail):
            await self._unique_name(clean["name"])
            experiment_id = uuid.uuid4().hex[:12]
            now = ex.ts(self.clock())
            async with self.state._connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO experiments (id, name, hypothesis, enroll_until, max_enrolled, "
                    "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (experiment_id, clean["name"], clean["hypothesis"], clean["enroll_until"],
                     clean["max_enrolled"], self.ctx.operator, now, now))
                await self._write_revision(db, experiment_id, 1, clean, None, now)
                await db.commit()
            trail.record(experiment_id, None, 1, name=clean["name"], variable=clean["variable"])
            return await self.get(experiment_id)
        return await self._run("create", "edit", {"definition": clean}, work)

    async def _unique_name(self, name: str, experiment_id: str = "") -> None:
        async with self.state._connect() as db:
            rows = await ex._rows(db, "SELECT id, name FROM experiments")
        if any(r["id"] != experiment_id and r["name"].casefold() == name.casefold() for r in rows):
            raise _invalid(f"An experiment called '{name}' already exists", "name")

    @staticmethod
    def _current_definition(loaded: dict) -> dict:
        experiment, revision = loaded["experiment"], loaded["revision"]
        return {
            "name": experiment["name"], "hypothesis": experiment["hypothesis"] or "",
            "enroll_until": experiment["enroll_until"], "max_enrolled": experiment["max_enrolled"],
            "variable": revision["variable"], "allocation_a": revision["allocation_a"],
            "cohort": revision["cohort"], "response_window_days": revision["response_window_days"],
            "min_per_arm": revision["min_per_arm"], "min_duration_days": revision["min_duration_days"],
            "primary_metric": revision["primary_metric"],
            "arms": [{"key": k, "name": a["name"], "instruction": a["instruction"] or "",
                      "persona_id": a["persona_id"] or ""} for k, a in sorted(loaded["arms"].items())],
        }

    async def update(self, ref: str, changes: dict, expected_version: int | None = None) -> dict:
        unknown = set(changes) - set(IN_PLACE) - set(SUBSTANTIVE)
        if unknown:
            raise _invalid(f"Cannot edit {', '.join(sorted(unknown))}")
        loaded = await self._loaded(ref)
        experiment = loaded["experiment"]
        current = self._current_definition(loaded)
        if isinstance(changes.get("arms"), list):
            # An arm names only what changes: {"key": "B", "instruction": ...}.
            by_key = {a["key"]: dict(a) for a in current["arms"]}
            for i, arm in enumerate(changes["arms"][:2]):
                arm = arm if isinstance(arm, dict) else {}
                key = str(arm.get("key") or "AB"[i]).upper()
                if key not in by_key:
                    raise _invalid("arms are A and B", "arms")
                by_key[key].update({**arm, "key": key})
            changes = {**changes, "arms": [by_key[k] for k in ex.ARM_KEYS]}
        clean = await self.validate({**current, **changes})

        async def work(trail):
            if experiment["status"] == "completed":
                raise Conflict("A completed experiment cannot be edited", code="not_editable")
            if expected_version is not None and int(expected_version) != experiment["version"]:
                raise Conflict("This experiment changed. Reload it before saving",
                               code="stale_version", version=experiment["version"])
            await self._unique_name(clean["name"], experiment["id"])
            substantive = any(clean[k] != current[k] for k in SUBSTANTIVE)
            versions = None
            if substantive and experiment["status"] != "draft":
                versions = await self._pinned_versions(a["persona_id"] for a in clean["arms"])
            now = ex.ts(self.clock())
            number = experiment["current_revision"]
            async with self.state._connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE experiments SET name = ?, hypothesis = ?, enroll_until = ?, "
                    "max_enrolled = ?, version = version + 1, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (clean["name"], clean["hypothesis"], clean["enroll_until"],
                     clean["max_enrolled"], now, experiment["id"], experiment["version"]))
                if not cursor.rowcount:
                    await db.rollback()
                    raise Conflict("This experiment changed. Reload it before saving",
                                   code="stale_version")
                if substantive and experiment["status"] == "draft":
                    revision_id = loaded["revision"]["id"]
                    await db.execute("DELETE FROM experiment_arms WHERE revision_id = ?", (revision_id,))
                    await db.execute("DELETE FROM experiment_revisions WHERE id = ?", (revision_id,))
                    await self._write_revision(db, experiment["id"], number, clean, None, now)
                elif substantive:
                    number += 1
                    await self._write_revision(db, experiment["id"], number, clean, versions, now)
                    await db.execute("UPDATE experiments SET current_revision = ? WHERE id = ?",
                                     (number, experiment["id"]))
                await db.commit()
            trail.record(experiment["id"], experiment["current_revision"], number,
                         fields=sorted(k for k in clean if k in current and clean[k] != current[k]),
                         new_revision=number != experiment["current_revision"])
            return {**await self.get(experiment["id"]),
                    "new_revision": number != experiment["current_revision"]}
        return await self._run("update", "edit", {"id": experiment["id"], "changes": changes,
                                                  "expected_version": expected_version},
                               work, experiment["id"])

    async def _transition(self, action: str, ref: str, allowed: tuple, target_status: str | None,
                          sets: str, values: tuple, scope: str = "run",
                          refuse: str = "", extra_params: dict | None = None) -> dict:
        found = await self.find(ref)

        async def work(trail):
            loaded = await ex.load(self.state, found["id"])
            experiment = loaded["experiment"]
            if experiment["status"] not in allowed:
                raise Conflict(refuse or f"A {experiment['status']} experiment cannot be {action}d",
                               code="not_editable", status=experiment["status"])
            versions = (await self._pinned_versions(a["persona_id"] for a in loaded["arms"].values())
                        if action == "start" else None)
            now = ex.ts(self.clock())
            async with self.state._connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                if action == "start":
                    await self._freeze(db, loaded["revision"]["id"], versions, now)
                cursor = await db.execute(
                    f"UPDATE experiments SET {sets}, version = version + 1, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (*[now if v is _NOW else v for v in values], now, experiment["id"],
                     experiment["status"]))
                if not cursor.rowcount:
                    await db.rollback()
                    raise Conflict("This experiment changed. Reload it and try again",
                                   code="stale_version")
                await db.commit()
            trail.record(experiment["id"], experiment["status"], target_status or experiment["status"])
            return await self.get(experiment["id"])
        return await self._run(action, scope, {"id": found["id"], **(extra_params or {})},
                               work, found["id"])

    async def start(self, ref: str) -> dict:
        """Freeze the revision (persona versions pinned) and open enrollment."""
        return await self._transition("start", ref, ("draft",), "running",
                                      "status = 'running', started_at = ?", (_NOW,),
                                      refuse="Only a draft can be started")

    async def pause(self, ref: str) -> dict:
        return await self._transition("pause", ref, ("running",), "paused",
                                      "status = 'paused', paused_at = ?", (_NOW,),
                                      refuse="Only a running experiment can pause enrollment")

    async def resume(self, ref: str) -> dict:
        return await self._transition("resume", ref, ("paused",), "running",
                                      "status = 'running', paused_at = NULL", (),
                                      refuse="Only a paused experiment can resume enrollment")

    async def complete(self, ref: str, confirm: bool = False) -> dict:
        if not confirm:
            raise Conflict("Completing ends enrollment for good. Confirm to continue",
                           code="confirmation_required")
        return await self._transition("complete", ref, ("running", "paused"), "completed",
                                      "status = 'completed', completed_at = ?", (_NOW,),
                                      refuse="Only a running or paused experiment can be completed")

    async def hold(self, ref: str, reason: str = "") -> dict:
        """Hold every unsent email of the experiment. Approvals stay as they are."""
        reason = (reason or "").strip()[:300]
        result = await self._transition("hold", ref, ("running", "paused", "completed"), None,
                                        "hold_mail = 1, hold_reason = ?, held_at = ?",
                                        (reason, _NOW), refuse="A draft has no mail to hold",
                                        extra_params={"reason": reason})
        return result

    async def release(self, ref: str) -> dict:
        return await self._transition("release", ref, ("running", "paused", "completed"), None,
                                      "hold_mail = 0, hold_reason = '', held_at = NULL", (),
                                      refuse="A draft has no mail to release")

    async def label(self, inbound_id: str, label: str) -> dict:
        """A person's outcome label for one reply. Final: the classifier
        never replaces it."""
        async def work(trail):
            message = await self.state.get_inbound(inbound_id)
            if message is None:
                raise NotFound("No such message")
            try:
                row = await ex.set_outcome(self.state, inbound_id, label, actor=self.ctx.operator)
            except ValueError as error:
                raise _invalid(str(error), "label") from error
            trail.record(inbound_id, None, label, object_type="inbound_message")
            return {"outcome": row}
        return await self._run("label", "edit", {"inbound_id": inbound_id, "label": label},
                               work, inbound_id)


class _Now:
    """Placeholder for the command's own timestamp in a transition."""


_NOW = _Now()


def parse_revision(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise _invalid("revision must be a whole number", "revision") from error
    if number < 1:
        raise _invalid("revision must be 1 or more", "revision")
    return number
