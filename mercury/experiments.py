"""A/B experiments: stable assignment, exposure and positive-reply attribution.

An experiment compares two arms (A and B) on ONE variable: the opening
angle, the subject line, or the writing persona. An arm is a generation
instruction and/or a persona version. The definition lives in a revision;
starting the experiment freezes it (persona versions resolved to exact
versions), and a substantive edit after that is a new revision, so the
exposure history is never rewritten.

Assignment. When the Writer is about to draft for a prospect, ``enroll``
assigns them to an arm of the first running experiment whose cohort they
match and stores the assignment BEFORE anything is generated. The arm is a
hash of (experiment, revision number, prospect), so it is reproducible,
independent of the order or score prospects arrive in, and survives
retries and restarts. A prospect is assigned once per experiment (primary
key) and enters at most one experiment ever, so no one carries a previous
variant into another comparison. An assignment is kept for the prospect's
whole sequence, whatever happens to the experiment afterwards.

Exposure. ``writer_groups`` is the Writer's seam: it splits a batch by arm
and gives each arm's share a persona snapshot carrying the arm marker and
instruction. The generation records that snapshot, and a trigger stamps
every outbox row written from it with experiment, revision and arm ids
(migration v26). Follow-ups come from the same arm-written sequence, and a
regenerated draft reuses its generation's snapshot, so a prospect's whole
sequence stays in one arm. Mail written before experiments existed, or
for prospects outside one, keeps empty ids: explicitly unassigned.

Controls. ``paused`` stops new enrollment only: assigned prospects are
still written in their arm and their approved mail still sends. ``hold``
stops every unsent sequence email of the experiment at the send claim
(``experiment_hold``); nothing is unapproved or cancelled, and on release
each email goes out on its schedule (a follow-up still waits its delay
after the previous step). A hold never lets anything bypass review,
exclusions, pauses or company holds: those are checked as before.
``completed`` ends enrollment for good; enrolled sequences finish unless
held, and results keep maturing.

Outcomes and metrics: see ``results`` and docs/experiments.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from mercury.experiment_stats import METHOD, RATE_METHOD, newcombe_difference, wilson

logger = logging.getLogger("mercury.experiments")

ARM_KEYS = ("A", "B")
VARIABLES = {
    "opening_angle": "Opening angle",
    "subject_line": "Subject line",
    "persona": "Persona",
}
STATUS_LABELS = {"draft": "Draft", "running": "Running", "paused": "Paused",
                 "completed": "Completed"}
PRIMARY_METRICS = {
    "positive_reply_rate": "Positive-reply rate",
    "any_reply_rate": "Any-reply rate",
}

# ── Outcome labels ──

OUTCOME_LABELS = (
    "positive_interested", "positive_soft", "positive_referral", "neutral_question",
    "not_now", "not_interested", "hostile", "unsubscribe", "ooo", "bounce", "other",
)
POSITIVE_LABELS = frozenset({"positive_interested", "positive_soft", "positive_referral"})
OUTCOME_DESCRIPTIONS = {
    "positive_interested": "wants to talk, see more or start",
    "positive_soft": "open but noncommittal: send details, maybe later with interest",
    "positive_referral": "points to the right person by name or address",
    "neutral_question": "asks a question before deciding",
    "not_now": "timing: not now, next quarter, after a project",
    "not_interested": "a clear no",
    "hostile": "angry, threatens a complaint or legal action",
    "unsubscribe": "asks not to be contacted again",
    "ooo": "out of office or another automatic reply",
    "bounce": "the message was not delivered",
    "other": "none of the above",
}
# The handler's intent, mapped to an outcome without another model call.
# The confidence says how well the coarse intent pins the label: an intent
# that covers several outcomes (objection: timing, price, competition) sits
# below the default threshold and reads as uncertain until classified.
INTENT_OUTCOMES = {
    "interested": ("positive_interested", 0.8),
    "question": ("neutral_question", 0.8),
    "not_interested": ("not_interested", 0.8),
    "unsubscribe": ("unsubscribe", 1.0),  # keyword rules run before any model
    "objection": ("not_now", 0.5),
    "wrong_person": ("positive_referral", 0.5),  # a referral only if they name someone
    "escalate": ("hostile", 0.6),  # also where a failed classifier lands
    "ooo": ("ooo", 1.0),
}
# Intents the outcome classifier refines. Unsubscribe and out-of-office are
# settled by rules already.
CLASSIFY_INTENTS = ("interested", "question", "not_interested", "objection",
                    "wrong_person", "escalate")

DEFINITIONS = {
    "enrolled": "Prospects assigned to the arm.",
    "awaiting_first_touch": "Enrolled prospects whose first email has not been sent yet "
                            "(drafts, review, scheduled or failed).",
    "contacted": "Enrolled prospects with a successfully sent first touch: the earliest "
                 "sent email of the arm's sequence, normally step 1. Drafts, rejected, "
                 "cancelled and failed sends never count.",
    "mature": "Contacted prospects whose response window has ended. The primary "
              "denominator.",
    "pending": "Contacted prospects still inside their response window. Shown apart, "
               "never in a rate.",
    "sent": "Sequence emails of the arm that were sent (every step).",
    "replied": "Prospects with at least one human reply attributed to the arm. "
               "Automatic replies, bounces, duplicates and Mercury's own mail are excluded.",
    "positive": "Prospects with at least one reply labelled positive (interested, soft or "
                "referral) at or above the confidence threshold. One per prospect.",
    "uncertain": "Prospects whose only positive label is below the confidence threshold. "
                 "Not counted as positive until the classifier or a person settles it.",
    "bounced": "Prospects with a bounce attributed to the arm (mailbox-full and similar "
               "temporary bounces excluded).",
    "opted_out": "Prospects with a reply labelled unsubscribe.",
    "late_replies": "Prospects whose first human reply came after their response window. "
                    "Visible, never in the primary rate.",
    "rates": "Primary rates are counts among mature prospects, within the window, divided "
             "by mature.",
    "attribution": "A reply belongs to the arm of the sent email it answers (In-Reply-To or "
                   "References, followed back through Mercury's own replies). With no such "
                   "header, it belongs to the most recent sequence email sent to that prospect "
                   "from the mailbox that received it. Never to whichever campaign owns the "
                   "prospect now.",
}

RESULT_LABELS = {
    "not_started": "Not started",
    "insufficient_data": "Not enough data yet",
    "check_deliverability": "Check deliverability first",
    "no_clear_difference": "No clear difference",
    "a_ahead": "A ahead",
    "b_ahead": "B ahead",
}

_DEFAULTS = {
    "classify_outcomes": True, "confidence_threshold": 0.7, "response_window_days": 14,
    "min_per_arm": 50, "min_duration_days": 14, "health_min_mature": 30,
    "low_reply_rate": 0.01, "high_bounce_rate": 0.05, "max_classifications_per_cycle": 20,
}


def setting(config, name: str):
    """experiments.<name> from the config, or its default."""
    section = getattr(config, "experiments", None)
    value = getattr(section, name, None) if section is not None else None
    return _DEFAULTS[name] if value is None else value


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ts(when: datetime) -> str:
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when.replace(microsecond=0).isoformat()


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).strip().replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


# ── Assignment ──

def assignment_bucket(experiment_id: str, revision_number: int, prospect_id: str) -> float:
    """A uniform value in [0, 1) that depends only on these three ids."""
    digest = hashlib.sha256(f"{experiment_id}:{int(revision_number)}:{prospect_id}".encode()).hexdigest()
    return int(digest[:13], 16) / float(16 ** 13)


def arm_for_bucket(bucket: float, allocation_a: int) -> str:
    """Arm A takes the first allocation_a percent of the bucket range."""
    return "A" if bucket < int(allocation_a) / 100.0 else "B"


# ── Cohorts ──

COHORT_LISTS = ("industries", "import_batch_ids", "require_signals", "exclude_signals",
                "prospect_ids")


def normalize_cohort(raw) -> dict:
    """The eligible cohort, every filter optional and combined with AND:
    industries (any of, case-insensitive), import_batch_ids, require_signals
    (company has all), exclude_signals (company has none), min_score,
    prospect_ids. Empty: every prospect the Writer is about to draft for."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("cohort must be an object")
    unknown = set(raw) - set(COHORT_LISTS) - {"min_score"}
    if unknown:
        raise ValueError(f"unknown cohort filter(s): {', '.join(sorted(unknown))}")
    cohort: dict = {}
    for key in COHORT_LISTS:
        values = raw.get(key) or []
        if isinstance(values, str):
            values = [v for v in values.split(",")]
        if not isinstance(values, list):
            raise ValueError(f"cohort.{key} must be a list")
        values = [str(v).strip() for v in values if str(v).strip()]
        if key in ("require_signals", "exclude_signals"):
            values = [v.upper() for v in values]
        if values:
            cohort[key] = sorted(dict.fromkeys(values)) if key != "prospect_ids" else list(dict.fromkeys(values))
    if raw.get("min_score") not in (None, ""):
        try:
            cohort["min_score"] = int(raw["min_score"])
        except (TypeError, ValueError) as error:
            raise ValueError("cohort.min_score must be a whole number") from error
    return cohort


def describe_cohort(cohort: dict) -> str:
    parts = []
    if cohort.get("industries"):
        parts.append("industry " + " or ".join(cohort["industries"]))
    if cohort.get("import_batch_ids"):
        parts.append("import " + ", ".join(cohort["import_batch_ids"]))
    if cohort.get("require_signals"):
        parts.append("with " + ", ".join(cohort["require_signals"]))
    if cohort.get("exclude_signals"):
        parts.append("without " + ", ".join(cohort["exclude_signals"]))
    if cohort.get("min_score") is not None:
        parts.append(f"score {cohort['min_score']} or more")
    if cohort.get("prospect_ids"):
        parts.append(f"{len(cohort['prospect_ids'])} chosen prospects")
    return ", ".join(parts) if parts else "every new prospect Mercury drafts for"


async def cohort_matcher(state, cohort: dict):
    """A predicate over prospect dicts. Signal filters are resolved to company
    sets once, in SQL (StateManager.cohort)."""
    require = cohort.get("require_signals") or []
    exclude = cohort.get("exclude_signals") or []
    allowed = set(await state.cohort(require, exclude, limit=1_000_000)) if require else None
    blocked: set[str] = set()
    if exclude and not require:
        for code in exclude:
            blocked |= set(await state.cohort([code], limit=1_000_000))
    industries = {i.casefold() for i in cohort.get("industries") or []}
    batches = set(cohort.get("import_batch_ids") or [])
    ids = set(cohort.get("prospect_ids") or [])
    min_score = cohort.get("min_score")

    def matches(p: dict) -> bool:
        if ids and p.get("id") not in ids:
            return False
        if industries and (p.get("industry") or "").casefold() not in industries:
            return False
        if batches and (p.get("import_batch_id") or "") not in batches:
            return False
        if min_score is not None and int(p.get("score") or 0) < min_score:
            return False
        company = p.get("company_id") or ""
        if allowed is not None and company not in allowed:
            return False
        if blocked and company in blocked:
            return False
        return True

    return matches


def _as_dict(prospect) -> dict:
    if isinstance(prospect, dict):
        return prospect
    if hasattr(prospect, "model_dump"):
        return prospect.model_dump()
    return dict(vars(prospect))


# ── Loading ──

async def _rows(db, sql: str, params=()) -> list[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _one(db, sql: str, params=()) -> dict | None:
    rows = await _rows(db, sql, params)
    return rows[0] if rows else None


async def load(state, experiment_id: str, revision_number: int | None = None) -> dict | None:
    """The experiment with one revision (the current one by default) and its
    arms keyed A and B."""
    async with state._connect() as db:
        experiment = await _one(db, "SELECT * FROM experiments WHERE id = ?", (experiment_id,))
        if experiment is None:
            return None
        number = int(revision_number or experiment["current_revision"])
        revision = await _one(db, "SELECT * FROM experiment_revisions WHERE experiment_id = ? "
                              "AND number = ?", (experiment_id, number))
        if revision is None:
            return None
        arms = await _rows(db, "SELECT * FROM experiment_arms WHERE revision_id = ? "
                           "ORDER BY arm_key", (revision["id"],))
        revisions = await _rows(db, "SELECT id, number, created_at, frozen_at FROM "
                                "experiment_revisions WHERE experiment_id = ? ORDER BY number",
                                (experiment_id,))
    revision["cohort"] = json.loads(revision.pop("cohort_json") or "{}")
    return {"experiment": experiment, "revision": revision,
            "arms": {a["arm_key"]: a for a in arms}, "revisions": revisions}


def _enrollment_open(experiment: dict, now: datetime) -> bool:
    if experiment["status"] != "running":
        return False
    until = parse_ts(experiment.get("enroll_until"))
    return until is None or now <= until


async def enroll(state, config, prospects, now: datetime | None = None) -> dict[str, dict]:
    """Assign each prospect that matches a running experiment's cohort, and
    return the arm of every given prospect that has an assignment (made now
    or before). The assignment is committed before this returns, so it
    exists before any draft is generated."""
    now = now or utcnow()
    given = [_as_dict(p) for p in prospects]
    ids = [p["id"] for p in given if p.get("id")]
    if not ids:
        return {}
    async with state._connect() as db:
        running = await _rows(
            db, "SELECT e.*, r.id AS revision_id, r.number AS revision_number, r.allocation_a, "
            "r.cohort_json FROM experiments e JOIN experiment_revisions r "
            "ON r.experiment_id = e.id AND r.number = e.current_revision "
            "WHERE e.status = 'running' AND r.frozen_at IS NOT NULL "
            "ORDER BY e.started_at, e.created_at, e.rowid")
    candidates = []
    for experiment in running:
        if not _enrollment_open(experiment, now):
            continue
        matcher = await cohort_matcher(state, json.loads(experiment["cohort_json"] or "{}"))
        candidates.append((experiment, matcher))
    if not candidates:
        return await assignments_for(state, ids)

    marks = ",".join("?" for _ in ids)
    from mercury.state import BUSY_TIMEOUT_SECONDS

    async with aiosqlite.connect(state.db_path, timeout=BUSY_TIMEOUT_SECONDS,
                                 isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(f"SELECT prospect_id FROM experiment_assignments "
                                  f"WHERE prospect_id IN ({marks})", ids) as cur:
                assigned = {r[0] for r in await cur.fetchall()}
            for experiment, matcher in candidates:
                # Paused, completed or edited since it was read: re-checked
                # under the write lock.
                fresh = await _one(db, "SELECT * FROM experiments WHERE id = ?", (experiment["id"],))
                if (fresh is None or not _enrollment_open(fresh, now)
                        or fresh["current_revision"] != experiment["revision_number"]):
                    continue
                arms = {r["arm_key"]: r for r in await _rows(
                    db, "SELECT * FROM experiment_arms WHERE revision_id = ?",
                    (experiment["revision_id"],))}
                if set(arms) != set(ARM_KEYS):
                    continue
                async with db.execute("SELECT COUNT(*) FROM experiment_assignments "
                                      "WHERE experiment_id = ?", (experiment["id"],)) as cur:
                    (count,) = await cur.fetchone()
                cap = int(fresh["max_enrolled"] or 0)
                for prospect in given:
                    pid = prospect.get("id")
                    if not pid or pid in assigned or (cap and count >= cap):
                        continue
                    if not matcher(prospect):
                        continue
                    bucket = assignment_bucket(experiment["id"], experiment["revision_number"], pid)
                    arm = arms[arm_for_bucket(bucket, experiment["allocation_a"])]
                    cursor = await db.execute(
                        "INSERT OR IGNORE INTO experiment_assignments (experiment_id, prospect_id, "
                        "revision_id, arm_id, arm_key, bucket, assigned_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (experiment["id"], pid, experiment["revision_id"], arm["id"],
                         arm["arm_key"], bucket, ts(now)))
                    if cursor.rowcount:
                        count += 1
                    assigned.add(pid)
            await db.execute("COMMIT")
        except BaseException:
            await db.execute("ROLLBACK")
            raise
    return await assignments_for(state, ids)


async def assignments_for(state, prospect_ids: list[str]) -> dict[str, dict]:
    """{prospect_id: arm context} for the prospects that have an assignment."""
    if not prospect_ids:
        return {}
    marks = ",".join("?" for _ in prospect_ids)
    async with state._connect() as db:
        rows = await _rows(
            db, "SELECT a.prospect_id, a.experiment_id, a.revision_id, a.arm_id, a.arm_key, "
            "a.bucket, a.assigned_at, e.name AS experiment_name, r.number AS revision, "
            "r.variable, m.name AS arm_name, m.instruction, m.persona_version_id "
            "FROM experiment_assignments a JOIN experiments e ON e.id = a.experiment_id "
            "JOIN experiment_revisions r ON r.id = a.revision_id "
            "JOIN experiment_arms m ON m.id = a.arm_id "
            f"WHERE a.prospect_id IN ({marks}) ORDER BY a.assigned_at", list(prospect_ids))
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(row["prospect_id"], row)
    return out


def arm_marker(context: dict) -> dict:
    """What a generation's persona snapshot carries for its arm."""
    return {"experiment_id": context["experiment_id"], "revision_id": context["revision_id"],
            "arm_id": context["arm_id"], "arm_key": context["arm_key"],
            "instruction": context.get("instruction") or ""}


async def arm_profile(state, config, context: dict, base: dict | None) -> dict:
    """The persona snapshot an arm writes with: the arm's frozen persona
    version, the sign-off and mailbox of the group it came from, and the arm
    marker with its instruction (rendered by personas.voice_instructions)."""
    from mercury.personas import PersonaStore

    personas = PersonaStore(state)
    base = base or await personas.resolve(config)
    if context.get("persona_version_id"):
        profile = await personas.resolve(config, context["persona_version_id"])
    else:
        profile = dict(base)
    keep = {k: base[k] for k in ("signer", "mailbox") if k in base}
    return {**profile, **keep, "experiment": arm_marker(context)}


async def writer_groups(state, config, groups: list[tuple], now: datetime | None = None) -> list[tuple]:
    """The Writer's seam. Takes its (name, prospects, mailbox, profile)
    groups, enrolls eligible prospects, and returns the groups with each
    arm's share split off and given that arm's profile. Prospects in no
    experiment stay in their group untouched. An arm whose persona cannot be
    loaded is skipped this cycle: its prospects stay 'new' and are tried
    again, in the same arm."""
    everyone = [p for _name, members, _mailbox, _profile in groups for p in members]
    contexts = await enroll(state, config, everyone, now)
    if not contexts:
        return groups
    out = []
    for name, members, mailbox, profile in groups:
        plain = [p for p in members if _as_dict(p).get("id") not in contexts]
        if plain:
            out.append((name, plain, mailbox, profile))
        by_arm: dict[str, tuple[dict, list]] = {}
        for p in members:
            context = contexts.get(_as_dict(p).get("id"))
            if context:
                by_arm.setdefault(context["arm_id"], (context, []))[1].append(p)
        for context, share in by_arm.values():
            try:
                arm = await arm_profile(state, config, context, profile)
            except Exception as e:
                logger.error(f"Experiments: cannot load the persona of "
                             f"{context['experiment_name']} {context['arm_name']}: {e}")
                continue
            out.append((f"{name} · {context['experiment_name']} · {context['arm_name']}",
                        share, mailbox, arm))
    return out


# ── Outcomes ──

def mapped_outcome(row: dict) -> tuple[str, float, str]:
    """(label, confidence, source) for a stored inbound message, from what
    the handler already decided: no model call."""
    kind = row.get("kind") or "message"
    if kind == "bounce":
        return "bounce", 1.0, "rule"
    if kind == "automatic":
        return ("ooo" if row.get("auto_kind") == "out_of_office" else "other"), 1.0, "rule"
    label, confidence = INTENT_OUTCOMES.get(row.get("intent") or "", ("other", 0.0))
    return label, confidence, "intent"


async def set_outcome(state, inbound_id: str, label: str, *, confidence: float = 1.0,
                      source: str = "manual", detail: str = "", actor: str = "") -> dict:
    """Store a label for one inbound message. A person's label is final: the
    classifier never replaces it."""
    if label not in OUTCOME_LABELS:
        raise ValueError(f"label must be one of {', '.join(OUTCOME_LABELS)}")
    now = ts(utcnow())
    confidence = max(0.0, min(1.0, float(confidence)))
    async with state._connect() as db:
        guard = "" if source == "manual" else " WHERE experiment_outcomes.source != 'manual'"
        await db.execute(
            "INSERT INTO experiment_outcomes (inbound_id, label, confidence, source, detail, "
            "labeled_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(inbound_id) DO UPDATE SET label = excluded.label, "
            "confidence = excluded.confidence, source = excluded.source, "
            "detail = excluded.detail, labeled_by = excluded.labeled_by, "
            "updated_at = excluded.updated_at" + guard,
            (inbound_id, label, confidence, source, detail[:500], actor, now, now))
        await db.commit()
        row = await _one(db, "SELECT * FROM experiment_outcomes WHERE inbound_id = ?", (inbound_id,))
    return row


def outcome_prompt(text: str) -> str:
    labels = "\n".join(f'- "{k}": {v}' for k, v in OUTCOME_DESCRIPTIONS.items())
    return f"""Label the outcome of this reply to a cold email with exactly ONE label:
{labels}

Judge only what the person wrote. "Send me more info" without other interest
is positive_soft. A named colleague or address to contact is positive_referral;
"not the right person" with no one named is not_interested.

Reply:
\"\"\"{text}\"\"\"

Return ONLY JSON: {{"label": "...", "confidence": 0.0}} where confidence is
your probability (0 to 1) that the label is right."""


async def classify_pending(state, brain, config, limit: int | None = None) -> int:
    """Label human replies of assigned prospects with the outcome classifier.
    One Claude call each; a reply already labelled (by it or a person) is
    never sent again. A classifier failure stores the intent mapping, so a
    bad reply cannot cost a call every cycle. Returns how many were labelled."""
    if brain is None or not setting(config, "classify_outcomes"):
        return 0
    limit = int(limit or setting(config, "max_classifications_per_cycle"))
    marks = ",".join("?" for _ in CLASSIFY_INTENTS)
    async with state._connect() as db:
        pending = await _rows(
            db, "SELECT i.* FROM inbound_messages i WHERE i.kind = 'message' "
            f"AND i.status = 'processed' AND i.intent IN ({marks}) "
            "AND i.prospect_id IN (SELECT prospect_id FROM experiment_assignments) "
            "AND NOT EXISTS (SELECT 1 FROM experiment_outcomes o WHERE o.inbound_id = i.id) "
            "ORDER BY i.created_at, i.rowid LIMIT ?", (*CLASSIFY_INTENTS, limit))
    if not pending:
        return 0
    from mercury.agents.handler import strip_quoted

    done = 0
    for row in pending:
        own = strip_quoted(row.get("body") or "") or (row.get("body") or "")
        try:
            result = await brain.think_json(outcome_prompt(own[:4000]), session_id="mercury-experiments",
                                            agent="handler", task="classify_outcome")
        except Exception as e:  # a failed call is not a label
            logger.warning(f"Experiments: outcome classifier failed: {e}")
            continue
        label, confidence = None, None
        if isinstance(result, dict):
            label = str(result.get("label") or "").strip().lower()
            try:
                confidence = float(result.get("confidence"))
            except (TypeError, ValueError):
                confidence = None
        if label in OUTCOME_LABELS and confidence is not None and 0 <= confidence <= 1:
            await set_outcome(state, row["id"], label, confidence=confidence, source="classifier")
        else:
            mapped, mapped_conf, _ = mapped_outcome(row)
            await set_outcome(state, row["id"], mapped, confidence=mapped_conf, source="intent",
                              detail="the classifier gave no usable label")
        done += 1
    return done


# ── Results ──

def _rate(count: int, n: int) -> float | None:
    return round(count / n, 6) if n else None


def _interval(count: int, n: int) -> dict | None:
    bounds = wilson(count, n)
    return {"low": round(bounds[0], 6), "high": round(bounds[1], 6)} if bounds else None


async def _our_addresses(db, config) -> set[str]:
    async with db.execute("SELECT DISTINCT lower(mailbox) FROM outbox WHERE mailbox != '' "
                          "UNION SELECT DISTINCT lower(mailbox) FROM inbound_messages "
                          "WHERE mailbox != ''") as cur:
        own = {r[0] for r in await cur.fetchall() if r[0]}
    persona = getattr(getattr(config, "persona", None), "email", "") or ""
    if persona:
        own.add(persona.strip().lower())
    return own


def _bounce_counts(row: dict) -> bool:
    """A bounce that says something about the address (not mailbox full)."""
    from mercury import bounces as bounce_policy

    try:
        headers = json.loads(row.get("headers_json") or "{}")
    except (TypeError, ValueError):
        headers = {}
    _code, bucket = bounce_policy.classify_bounce(headers if isinstance(headers, dict) else {},
                                                  row.get("body") or "")
    return bucket != bounce_policy.NOISE


def _week_start(when: datetime):
    """The Monday (a date) of the week a time falls in."""
    return (when - timedelta(days=when.weekday())).date()


def weekly_breakdown(entries: list[tuple[dict, dict]]) -> list[dict]:
    """Per week of first email: each arm's contacted, mature, pending, positive
    and replied counts. Each prospect is in exactly one week, the week of their
    first email, and counts once. A week is open while any of its prospects
    is still inside the response window."""
    weeks: dict = {}
    for person, t in entries:
        monday = _week_start(person["first"])
        row = weeks.setdefault(monday, {k: {"contacted": 0, "mature": 0, "pending": 0,
                                           "positive": 0, "replied": 0} for k in ARM_KEYS})
        cell = row[person["arm"]]
        cell["contacted"] += 1
        if person["mature"]:
            cell["mature"] += 1
            cell["positive"] += int(t["positive"])
            cell["replied"] += int(t["replied"])
        else:
            cell["pending"] += 1
    out = []
    if weeks:
        monday, last = min(weeks), max(weeks)
        while monday <= last:
            row = weeks.get(monday) or {k: {"contacted": 0, "mature": 0, "pending": 0,
                                            "positive": 0, "replied": 0} for k in ARM_KEYS}
            for cell in row.values():
                cell["positive_rate"] = _rate(cell["positive"], cell["mature"])
                cell["reply_rate"] = _rate(cell["replied"], cell["mature"])
            out.append({"week": monday.isoformat(),
                        "ends": (monday + timedelta(days=6)).isoformat(),
                        "open": any(row[k]["pending"] for k in ARM_KEYS), **row})
            monday += timedelta(days=7)
    return out


def daily_series(entries: list[tuple[dict, dict]], window: timedelta, now: datetime,
                 max_days: int = 120) -> list[dict]:
    """Cumulative counts per day among prospects whose window had closed by the
    end of that day, per arm. Rates are counts over ``mature``. It starts the
    day the first prospect matured and ends today, so the last row equals the
    arm totals of the results."""
    matured = sorted(((p["first"] + window, p["arm"], t) for p, t in entries if p["mature"]),
                     key=lambda m: m[0])
    if not matured:
        return []
    zero = {k: {"mature": 0, "positive": 0, "replied": 0, "bounced": 0, "opted_out": 0}
            for k in ARM_KEYS}
    day, last, i, out = matured[0][0].date(), now.date(), 0, []
    while day <= last:
        end = min(datetime.combine(day + timedelta(days=1), datetime.min.time()), now)
        while i < len(matured) and matured[i][0] <= end:
            _, key, t = matured[i]
            zero[key]["mature"] += 1
            for name in ("positive", "replied", "bounced", "opted_out"):
                zero[key][name] += int(t[name])
            i += 1
        out.append({"date": day.isoformat(), **{k: dict(v) for k, v in zero.items()}})
        day += timedelta(days=1)
    return out[-max_days:]


async def results(state, config, experiment_id: str, revision_number: int | None = None,
                  now: datetime | None = None) -> dict | None:
    """Per-arm counts, rates, the B minus A difference with its interval,
    the decision and health warnings for one revision (current by default).
    Every number the experiments screen shows comes from here."""
    loaded = await load(state, experiment_id, revision_number)
    if loaded is None:
        return None
    now = now or utcnow()
    experiment, revision, arms = loaded["experiment"], loaded["revision"], loaded["arms"]
    window = timedelta(days=int(revision["response_window_days"]))
    threshold = float(setting(config, "confidence_threshold"))

    async with state._connect() as db:
        assignments = await _rows(db, "SELECT * FROM experiment_assignments WHERE revision_id = ?",
                                  (revision["id"],))
        pids = [a["prospect_id"] for a in assignments]
        outbox, inbound, outcomes = [], [], {}
        for start in range(0, len(pids), 500):
            chunk = pids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            outbox += await _rows(db, f"SELECT * FROM outbox WHERE prospect_id IN ({marks})", chunk)
            inbound += await _rows(db, f"SELECT * FROM inbound_messages WHERE prospect_id IN ({marks}) "
                                   "AND status IN ('processed', 'skipped')", chunk)
        # Messages linked to our mail but not (yet) to a prospect.
        ids = [o["id"] for o in outbox]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            inbound += await _rows(db, f"SELECT * FROM inbound_messages WHERE outbox_id IN ({marks}) "
                                   "AND prospect_id = '' AND status IN ('processed', 'skipped')", chunk)
        inbound_ids = [i["id"] for i in inbound]
        for start in range(0, len(inbound_ids), 500):
            chunk = inbound_ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in await _rows(db, f"SELECT * FROM experiment_outcomes WHERE inbound_id IN ({marks})",
                                   chunk):
                outcomes[row["inbound_id"]] = row
        own = await _our_addresses(db, config)
        async with db.execute("SELECT COUNT(*) FROM inbound_messages WHERE prospect_id IN "
                              "(SELECT prospect_id FROM experiment_assignments WHERE revision_id = ?) "
                              "AND status IN ('received', 'retry', 'failed')", (revision["id"],)) as cur:
            (unhandled,) = await cur.fetchone()
        # A second copy of a message (another inbox) is never handled, so it
        # is found through the copy that was.
        async with db.execute("SELECT COUNT(*) FROM inbound_messages WHERE status = 'duplicate' "
                              "AND duplicate_of IN (SELECT id FROM inbound_messages WHERE prospect_id "
                              "IN (SELECT prospect_id FROM experiment_assignments WHERE revision_id = ?))",
                              (revision["id"],)) as cur:
            (duplicates,) = await cur.fetchone()

    outbox_by_id = {o["id"]: o for o in outbox}
    inbound_by_id = {i["id"]: i for i in inbound}
    assigned = {a["prospect_id"]: a for a in assignments}

    sent_seq: dict[str, list[dict]] = {}
    for o in outbox:
        if o["kind"] == "sequence" and o["status"] == "sent" and o.get("sent_at"):
            sent_seq.setdefault(o["prospect_id"], []).append(o)
    for rows in sent_seq.values():
        rows.sort(key=lambda o: parse_ts(o["sent_at"]) or datetime.min)

    def sequence_row(outbox_id: str, depth: int = 0) -> dict | None:
        row = outbox_by_id.get(outbox_id)
        if row is None or depth > 10:
            return None
        if row["kind"] == "sequence":
            return row
        answered = inbound_by_id.get(row.get("answers_inbound_id") or "")
        return sequence_row(answered["outbox_id"], depth + 1) if answered and answered.get("outbox_id") else None

    def attribute(message: dict, when: datetime) -> tuple[dict | None, str]:
        if message.get("outbox_id"):
            return sequence_row(message["outbox_id"]), "thread"
        mailbox = (message.get("mailbox") or "").lower()
        earlier = [o for o in sent_seq.get(message.get("prospect_id") or "", [])
                   if (not mailbox or (o.get("mailbox") or "").lower() == mailbox)
                   and (parse_ts(o["sent_at"]) or datetime.max) <= when]
        return (earlier[-1] if earlier else None), "mailbox"

    per: dict[str, dict] = {}
    for pid, a in assigned.items():
        mine = [o for o in sent_seq.get(pid, []) if o["experiment_revision_id"] == revision["id"]]
        first = parse_ts(mine[0]["sent_at"]) if mine else None
        per[pid] = {"arm": a["arm_key"], "first": first, "sent": len(mine),
                    "mature": bool(first and first + window <= now), "events": []}

    quality = {"unhandled_messages": unhandled, "automatic_excluded": 0, "own_mail_excluded": 0,
               "duplicates_excluded": duplicates, "arm_mismatches": 0, "unattributed_messages": 0}
    for message in inbound:
        when = parse_ts(message.get("received_at")) or parse_ts(message.get("created_at"))
        if when is None:
            continue
        seq, method = attribute(message, when)
        if seq is None or seq.get("experiment_revision_id") != revision["id"]:
            quality["unattributed_messages"] += 1
            continue
        pid = seq["prospect_id"]
        if pid not in per:
            quality["unattributed_messages"] += 1
            continue
        if seq.get("experiment_arm_id") != assigned[pid]["arm_id"]:
            quality["arm_mismatches"] += 1
            continue
        kind = message.get("kind") or "message"
        if kind == "automatic":
            quality["automatic_excluded"] += 1
            continue
        if kind == "message" and (message.get("from_email") or "").lower() in own:
            quality["own_mail_excluded"] += 1
            continue
        if kind == "bounce" and not _bounce_counts(message):
            continue
        stored = outcomes.get(message["id"])
        if stored:
            label, confidence, source = stored["label"], float(stored["confidence"]), stored["source"]
        else:
            label, confidence, source = mapped_outcome(message)
        first = per[pid]["first"] or parse_ts(seq["sent_at"])
        per[pid]["events"].append({
            "inbound_id": message["id"], "kind": kind, "label": label, "confidence": confidence,
            "source": source, "at": when, "method": method,
            "in_window": bool(first and when <= first + window),
        })

    def tally(pid: dict) -> dict:
        human = [e for e in pid["events"] if e["kind"] == "message"]
        inside = [e for e in human if e["in_window"]]
        bounces = [e for e in pid["events"] if e["kind"] == "bounce"]
        sure = lambda e: e["confidence"] >= threshold  # noqa: E731
        return {
            "replied": bool(inside),
            "positive": any(e["label"] in POSITIVE_LABELS and sure(e) for e in inside),
            "uncertain": (not any(e["label"] in POSITIVE_LABELS and sure(e) for e in inside)
                          and any(e["label"] in POSITIVE_LABELS for e in inside)),
            "opted_out": any(e["label"] == "unsubscribe" and sure(e) for e in inside),
            "bounced": any(e["in_window"] for e in bounces),
            "late": bool(human) and not inside,
            "labels": sorted({e["label"] for e in inside}),
            "replied_any": bool(human),
            "positive_any": any(e["label"] in POSITIVE_LABELS and sure(e) for e in human),
            "opted_out_any": any(e["label"] == "unsubscribe" and sure(e) for e in human),
            "bounced_any": bool(bounces),
        }

    metric = revision["primary_metric"]
    arm_results = []
    for key in ARM_KEYS:
        arm = arms.get(key) or {}
        members = [p for p in per.values() if p["arm"] == key]
        contacted = [p for p in members if p["first"]]
        mature = [p for p in contacted if p["mature"]]
        tallies = [(p, tally(p)) for p in members]
        mature_t = [t for p, t in tallies if p["first"] and p["mature"]]
        contacted_t = [t for p, t in tallies if p["first"]]
        n = len(mature)
        counts = {k: sum(1 for t in mature_t if t[k])
                  for k in ("positive", "uncertain", "replied", "opted_out", "bounced", "late")}
        labels: dict[str, int] = {}
        for t in mature_t:
            for label in t["labels"]:
                labels[label] = labels.get(label, 0) + 1
        primary_count = counts["positive"] if metric == "positive_reply_rate" else counts["replied"]
        firsts = sorted(p["first"] for p in contacted)
        arm_results.append({
            "key": key, "id": arm.get("id", ""), "name": arm.get("name", ""),
            "instruction": arm.get("instruction", ""), "persona_id": arm.get("persona_id", ""),
            "persona_version_id": arm.get("persona_version_id", ""),
            "enrolled": len(members),
            "awaiting_first_touch": len(members) - len(contacted),
            "contacted": len(contacted), "mature": n, "pending": len(contacted) - n,
            "primary": {"metric": metric, "count": primary_count, "rate": _rate(primary_count, n),
                        "interval": _interval(primary_count, n)},
            "positive": {"count": counts["positive"], "rate": _rate(counts["positive"], n),
                         "interval": _interval(counts["positive"], n)},
            "any_reply": {"count": counts["replied"], "rate": _rate(counts["replied"], n)},
            "bounce": {"count": counts["bounced"], "rate": _rate(counts["bounced"], n)},
            "opt_out": {"count": counts["opted_out"], "rate": _rate(counts["opted_out"], n)},
            "uncertain": counts["uncertain"],
            "late_replies": sum(1 for t in contacted_t if t["late"]),
            "labels": labels,
            "raw": {"sent": sum(p["sent"] for p in members), "contacted": len(contacted),
                    "replied": sum(1 for t in contacted_t if t["replied_any"]),
                    "positive": sum(1 for t in contacted_t if t["positive_any"]),
                    "bounced": sum(1 for t in contacted_t if t["bounced_any"]),
                    "opted_out": sum(1 for t in contacted_t if t["opted_out_any"])},
            "_firsts": firsts,
        })

    a, b = arm_results
    diff = newcombe_difference(b["primary"]["count"], b["mature"], a["primary"]["count"], a["mature"])
    comparison = {
        "metric": metric, "metric_label": PRIMARY_METRICS.get(metric, metric),
        "difference": round(diff[0], 6) if diff else None,
        "interval": {"low": round(diff[1], 6), "high": round(diff[2], 6)} if diff else None,
        "direction": "B minus A", "method": METHOD, "rate_method": RATE_METHOD,
    }

    min_per_arm = int(revision["min_per_arm"])
    started = parse_ts(experiment.get("started_at"))
    min_days = int(revision["min_duration_days"] or 0)
    mature_min = min(a["mature"], b["mature"])
    duration_met = bool(started and now >= started + timedelta(days=min_days))
    sufficient = bool(started) and mature_min >= min_per_arm and duration_met

    enrolling = _enrollment_open(experiment, now)
    short = [arm["key"] for arm in arm_results
             if not enrolling and arm["enrolled"] < min_per_arm]

    def arm_ready(arm: dict) -> tuple[datetime | None, bool]:
        firsts = arm["_firsts"]
        if len(firsts) >= min_per_arm:
            return firsts[min_per_arm - 1] + window, False
        if not firsts or arm["key"] in short:
            return None, True
        days = max((now - firsts[0]).total_seconds() / 86400, 1.0)
        per_day = len(firsts) / days
        return now + timedelta(days=(min_per_arm - len(firsts)) / per_day) + window, True

    earliest, estimated = None, False
    if started:
        ready = [arm_ready(arm) for arm in arm_results]
        if all(r[0] for r in ready):
            earliest = max([r[0] for r in ready] + [started + timedelta(days=min_days)])
            estimated = any(r[1] for r in ready)

    total_mature = a["mature"] + b["mature"]
    replied = a["any_reply"]["count"] + b["any_reply"]["count"]
    bounced = a["bounce"]["count"] + b["bounce"]["count"]
    warnings = []
    if total_mature >= int(setting(config, "health_min_mature")):
        reply_rate = replied / total_mature
        if reply_rate < float(setting(config, "low_reply_rate")):
            warnings.append({
                "code": "low_reply_rate",
                "message": (f"Only {replied} of {total_mature} mature contacts replied at all "
                            f"({reply_rate:.1%}). That usually means mail is not reaching the "
                            "inbox. Check deliverability before reading this as a copy result."),
                "action": {"label": "Check deliverability", "tab": "mailboxes",
                           "cli": "mercury mail placement"},
            })
        bounce_rate = bounced / total_mature
        if bounce_rate > float(setting(config, "high_bounce_rate")):
            warnings.append({
                "code": "high_bounce_rate",
                "message": (f"{bounced} of {total_mature} mature contacts bounced "
                            f"({bounce_rate:.1%}). Fix the list and the mailboxes before "
                            "comparing the arms."),
                "action": {"label": "Check deliverability", "tab": "mailboxes",
                           "cli": "mercury mail placement"},
            })

    if not started:
        code = "not_started"
    elif not sufficient:
        code = "insufficient_data"
    elif warnings:
        code = "check_deliverability"
    elif diff and diff[1] > 0:
        code = "b_ahead"
    elif diff and diff[2] < 0:
        code = "a_ahead"
    else:
        code = "no_clear_difference"

    reasons = []
    if started and mature_min < min_per_arm:
        reasons.append(f"{mature_min} of {min_per_arm} mature per arm")
    if started and not duration_met:
        reasons.append(f"the experiment runs at least {min_days} days")
    if started and short:
        reasons.append(f"enrollment has ended with fewer than {min_per_arm} in arm "
                       + " and ".join(short))
    if code == "not_started":
        line = "Not started. Nothing is enrolled until you start it."
    elif code == "insufficient_data":
        line = "Insufficient data: " + "; ".join(reasons)
        if earliest:
            line += f", earliest decision {'~' if estimated else ''}{earliest.date().isoformat()}"
    elif code == "check_deliverability":
        line = "Enough data, but the health checks failed. Check deliverability before comparing copy."
    elif code == "no_clear_difference":
        line = "The 95% interval for B minus A includes zero: no clear difference."
    else:
        line = (f"{'B' if code == 'b_ahead' else 'A'}'s {comparison['metric_label'].lower()} is "
                "higher and the 95% interval for the difference excludes zero.")

    for arm in arm_results:
        arm.pop("_firsts")
    entries = [(p, tally(p)) for p in per.values() if p["first"]]
    return {
        "experiment_id": experiment_id, "revision": revision["number"],
        "revision_id": revision["id"], "as_of": ts(now),
        "window_days": int(revision["response_window_days"]), "min_per_arm": min_per_arm,
        "min_duration_days": min_days, "confidence_threshold": threshold,
        "primary_metric": metric, "primary_metric_label": PRIMARY_METRICS.get(metric, metric),
        "arms": arm_results, "comparison": comparison,
        "by_week": weekly_breakdown(entries),
        "series": daily_series(entries, window, now),
        "decision": {"code": code, "label": RESULT_LABELS[code], "line": line,
                     "sufficient": sufficient, "mature_min": mature_min,
                     "mature_needed": min_per_arm, "duration_met": duration_met,
                     "earliest_decision_at": ts(earliest) if earliest else None,
                     "earliest_estimated": estimated, "reasons": reasons,
                     "recommends_winner": code in ("a_ahead", "b_ahead"),
                     "automatic_changes": False},
        "health": {"warnings": warnings, "limits": {
            "low_reply_rate": float(setting(config, "low_reply_rate")),
            "high_bounce_rate": float(setting(config, "high_bounce_rate")),
            # Mercury stops sending altogether past this bounce rate.
            "sending_stops_at": getattr(getattr(getattr(config, "channels", None), "email", None),
                                        "max_bounce_rate", None)}},
        "data_quality": quality,
        "definitions": DEFINITIONS,
    }


async def exposures(state, experiment_id: str, *, revision_number: int | None = None,
                    arm: str = "", limit: int = 100, offset: int = 0) -> dict:
    """Who is in which arm and what was sent to them: each assignment with
    the outbox rows stamped for it, their generation and persona version."""
    loaded = await load(state, experiment_id, revision_number)
    if loaded is None:
        return {"assignments": [], "total": 0}
    revision = loaded["revision"]
    where, params = "a.revision_id = ?", [revision["id"]]
    if arm:
        where += " AND a.arm_key = ?"
        params.append(arm.upper())
    async with state._connect() as db:
        async with db.execute(f"SELECT COUNT(*) FROM experiment_assignments a WHERE {where}",
                              params) as cur:
            (total,) = await cur.fetchone()
        rows = await _rows(
            db, "SELECT a.*, p.first_name, p.last_name, p.email, p.company, p.status AS prospect_status "
            f"FROM experiment_assignments a LEFT JOIN prospects p ON p.id = a.prospect_id WHERE {where} "
            "ORDER BY a.assigned_at, a.prospect_id LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))))
        for row in rows:
            row["emails"] = await _rows(
                db, "SELECT o.id, o.step, o.status, o.sent_at, o.mailbox, o.campaign_id, "
                "o.generation_id, g.persona_version_id, o.experiment_arm_id FROM outbox o "
                "LEFT JOIN email_generations g ON g.id = o.generation_id "
                "WHERE o.prospect_id = ? AND o.experiment_revision_id = ? ORDER BY o.step",
                (row["prospect_id"], revision["id"]))
    return {"revision": revision["number"], "total": total, "assignments": rows}
