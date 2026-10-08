"""Bounce classification: read the DSN status code, bucket it, act on the bucket.

A bounce used to be one number. That hid the difference that matters: a bad
address (LIST) costs us one prospect, while a blocked sender (SENDER, BURNED)
means the mailbox or the whole domain is being rejected on reputation and every
further send makes it worse.

The enhanced status code (RFC 3463, ``5.1.1``) is read from the bounce and
stored on the ``bounce`` action as ``dsn_code`` and ``bucket``:

====== =============================================== ==============================
LIST      5.1.1 5.1.10 5.1.0 5.2.1 5.4.1 5.5.0          bad address: prospect invalid
SENDER    5.7.1 5.7.0 5.7.23 5.7.26 5.7.509 5.7.520     auth/policy: pause the mailbox
BURNED    5.7.606 - 5.7.614                             pause the domain, CANCEL_CANDIDATE
THROTTLE  any 4.x.x                                     halve the mailbox cap for 7 days
NOISE     5.2.2 5.3.4 5.7.133                           ignored for every rate
UNKNOWN   no code, or a code in none of the above       handled as before: address invalid
====== =============================================== ==============================

Two choices lean toward stopping rather than carrying on:

* any other ``5.7.x`` code is SENDER (RFC 3463 reads 5.7 as "security or
  policy": a block, not a typo in an address);
* a SENDER or BURNED bounce that cannot be pinned to a mailbox in the pool
  trips the global kill switch, because the narrower action is not available.

Kill switch (``kill_switch_reason``): SENDER + BURNED above 20% of the
classified bounces (after 30 of them), or the overall bounce rate above
``channels.email.max_bounce_rate`` (after 50 sends). LIST bounces alone never
trip the share rule.

Counters live in the ``settings`` table. They are reset only when a person
clears the kill switch explicitly (``mercury.holds.clear_health_hold``), never
by a resume, so the evidence behind a hold survives it. Nothing here needs a
schema change: the code and bucket ride in the bounce action's JSON, and the
per-mailbox throttle and per-domain verdict are settings keys.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("mercury.bounces")

LIST = "LIST"
SENDER = "SENDER"
BURNED = "BURNED"
THROTTLE = "THROTTLE"
NOISE = "NOISE"
UNKNOWN = "UNKNOWN"

BUCKETS = (LIST, SENDER, BURNED, THROTTLE, NOISE, UNKNOWN)
# What the share rule counts as "classified".
CLASSIFIED = (LIST, SENDER, BURNED, THROTTLE)

LIST_CODES = frozenset({"5.1.1", "5.1.10", "5.1.0", "5.2.1", "5.4.1", "5.5.0"})
SENDER_CODES = frozenset({"5.7.1", "5.7.0", "5.7.23", "5.7.26", "5.7.509", "5.7.520"})
NOISE_CODES = frozenset({"5.2.2", "5.3.4", "5.7.133"})
BURNED_RANGE = range(606, 615)  # 5.7.606 .. 5.7.614

# The address is the problem, or we cannot tell: mark it invalid and stop
# its queued sequence. Every other bucket leaves the prospect alone.
ADDRESS_BUCKETS = frozenset({LIST, UNKNOWN})

# Kill switch thresholds (the issue's numbers).
SHARE_MIN_CLASSIFIED = 30
SHARE_LIMIT = 0.20
MIN_SENDS_FOR_RATE = 50

THROTTLE_DAYS = 7
THROTTLE_FACTOR = 0.5

VERDICT_CANCEL_CANDIDATE = "CANCEL_CANDIDATE"

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"
_BUCKET_KEY = "bounce_bucket:"
_THROTTLE_KEY = "mailbox_throttle:"
_VERDICT_KEY = "domain_verdict:"

# ── Reading the code ──

# 550 5.1.1 / 550-5.7.26 / 421 4.7.0 : the reply code and its enhanced code.
_AFTER_REPLY_CODE = re.compile(
    r"(?<![\w.])[45]\d\d[ -]+([45])\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")
# Any x.y.z that is not a slice of a longer dotted number (an IP address, a
# "15.20.5.1" build id, a version string).
_ANY_CODE = re.compile(r"(?<![\w.])([45])\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")


def _norm(match: re.Match) -> str:
    return f"{match.group(1)}.{int(match.group(2))}.{int(match.group(3))}"


def _is_generic(code: str) -> bool:
    """x.0.0 says only "failed": a more specific code elsewhere wins."""
    return code.endswith(".0.0")


def extract_dsn_code(body: str = "", status_hint: str = "") -> str:
    """The enhanced status code of a bounce, '' when there is none.

    ``status_hint`` is the machine-readable ``Status:`` field of the
    delivery-status part when the provider handed it over. Order: the hint,
    then a code that follows an SMTP reply code (``550 5.1.1``, the form every
    MTA writes the real answer in), then the first code anywhere in the text.
    The first non-generic one (not ``x.0.0``) wins; with only generic ones,
    the first of those.
    """
    candidates: list[str] = []
    if status_hint:
        m = _ANY_CODE.search(status_hint)
        if m:
            candidates.append(_norm(m))
    text = body or ""
    m = _AFTER_REPLY_CODE.search(text)
    if m:
        candidates.append(_norm(m))
    m = _ANY_CODE.search(text)
    if m:
        candidates.append(_norm(m))
    for code in candidates:
        if not _is_generic(code):
            return code
    return candidates[0] if candidates else ""


def classify(code: str) -> str:
    """Bucket for an enhanced status code (UNKNOWN for '' or unlisted)."""
    m = _ANY_CODE.fullmatch((code or "").strip())
    if not m:
        return UNKNOWN
    cls, subject, detail = int(m.group(1)), int(m.group(2)), int(m.group(3))
    norm = f"{cls}.{subject}.{detail}"
    if norm in NOISE_CODES:
        return NOISE
    if cls == 4:
        return THROTTLE
    if norm in LIST_CODES:
        return LIST
    if cls == 5 and subject == 7 and detail in BURNED_RANGE:
        return BURNED
    if norm in SENDER_CODES or (cls == 5 and subject == 7):
        return SENDER
    return UNKNOWN


def classify_bounce(headers: dict | None, body: str = "") -> tuple[str, str]:
    """``(dsn_code, bucket)`` for an inbound bounce. Never raises: anything
    unreadable is ('', UNKNOWN)."""
    try:
        hint = str((headers or {}).get("dsn_status", "") or "")
        code = extract_dsn_code(body, hint)
        return code, classify(code)
    except Exception:  # pragma: no cover - defensive
        return "", UNKNOWN


# ── Kill switch ──


def kill_switch_reason(
    bucket_counts: dict[str, int],
    bounce_total: int,
    total_sent: int,
    max_rate: float,
) -> str:
    """Why sending should stop, or '' to carry on. Pure.

    ``bounce_total`` is every bounce since the last resume (NOISE is taken
    out here, so it never counts toward a rate).
    """
    counts = {b: int(bucket_counts.get(b, 0) or 0) for b in BUCKETS}
    classified = sum(counts[b] for b in CLASSIFIED)
    bad = counts[SENDER] + counts[BURNED]
    if classified >= SHARE_MIN_CLASSIFIED and bad / classified > SHARE_LIMIT:
        return (
            f"{bad} of {classified} classified bounces are sender or reputation "
            f"blocks ({bad / classified:.0%}, limit {SHARE_LIMIT:.0%}). "
            "Check the Mailboxes tab and your DNS before clearing the hold"
        )
    countable = max(0, int(bounce_total) - counts[NOISE])
    if max_rate and max_rate > 0 and total_sent >= MIN_SENDS_FOR_RATE \
            and countable / total_sent > max_rate:
        return (
            f"bounce rate {countable}/{total_sent} exceeded "
            f"{max_rate:.0%}. Check list quality before clearing the hold"
        )
    return ""


async def load_counts(state) -> dict[str, int]:
    out = {}
    for bucket in BUCKETS:
        try:
            out[bucket] = int(await state.get_setting(_BUCKET_KEY + bucket, "0") or 0)
        except ValueError:
            out[bucket] = 0
    return out


async def record_bucket(state, bucket: str) -> int:
    return await state.increment_setting(_BUCKET_KEY + bucket)


async def reset_counters(state) -> None:
    """Start a clean count (only when the kill switch is cleared explicitly)."""
    await state.set_setting(BOUNCE_COUNT_KEY, "0")
    for bucket in BUCKETS:
        await state.set_setting(_BUCKET_KEY + bucket, "0")


async def engage_kill_switch(state, reason: str) -> bool:
    """Flip the global kill switch. An existing reason is kept (the first
    cause is the useful one). Returns True when the switch is set afterwards,
    False if the write failed, so a caller can refuse to drop the evidence.

    An operator pause lives under its own key (``mercury.holds``), so a pause
    already in place never hides this hold."""
    from mercury.holds import migrate_legacy

    try:
        await migrate_legacy(state)
        if not await state.get_setting(KILL_SWITCH_KEY):
            await state.set_setting(KILL_SWITCH_KEY, reason)
        logger.error(f"Bounces: KILL SWITCH ENGAGED — {reason}")
        return True
    except Exception as e:
        logger.error(f"Bounces: could NOT engage the kill switch ({reason}): {e}")
        return False


# ── Acting on a bucket ──


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def throttle_key(mailbox: str) -> str:
    return _THROTTLE_KEY + (mailbox or "").strip().lower()


def verdict_key(domain: str) -> str:
    return _VERDICT_KEY + (domain or "").strip().lower()


async def throttled_until(state, mailbox: str, now: datetime | None = None) -> str | None:
    """ISO expiry of a mailbox's throttle, None when it has none or it ended."""
    raw = await state.get_setting(throttle_key(mailbox), "")
    if not raw:
        return None
    return raw if raw > _iso(now or _now()) else None


async def load_throttles(state, emails, now: datetime | None = None) -> dict[str, float]:
    """``{mailbox: cap factor}`` for the mailboxes with a live throttle."""
    out = {}
    for email in emails:
        if email and await throttled_until(state, email, now):
            out[email] = THROTTLE_FACTOR
    return out


async def get_verdict(state, domain: str) -> dict | None:
    raw = await state.get_setting(verdict_key(domain), "")
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and data.get("verdict") else None


async def clear_verdict(state, domain: str) -> None:
    if await state.get_setting(verdict_key(domain), ""):
        await state.set_setting(verdict_key(domain), "")


def _pause_reason(bucket: str, code: str, mailbox: str) -> str:
    if bucket == BURNED:
        return (
            f"{code} blocked {mailbox}: the receiving server rejects this domain on "
            "reputation. All of its mailboxes are paused and the domain is a "
            "cancel candidate. Do not send from it again; plan a replacement domain."
        )
    return (
        f"{code} blocked {mailbox}: the receiving server rejected it on authentication "
        "or policy. Paused. Run the DNS check on the Mailboxes tab (SPF, DKIM, DMARC "
        "and the warm-up checklist), fix what fails, then resume."
    )


async def apply_bucket(state, pool, *, bucket: str, code: str, mailbox: str) -> str:
    """Take the reputation action for one classified bounce.

    LIST/UNKNOWN (the address) and NOISE need none here: the caller owns the
    address, and NOISE is ignored. Returns a short note for the log.

    Fails closed. A SENDER/BURNED bounce that cannot be pinned to a mailbox of
    the pool, or a pause that cannot be written, engages the global kill
    switch instead.
    """
    from mercury import warmup

    if bucket == THROTTLE:
        if not mailbox:
            return "throttle bounce with no mailbox: nothing to slow"
        until = _iso(_now() + timedelta(days=THROTTLE_DAYS))
        await state.set_setting(throttle_key(mailbox), until)
        return f"{mailbox} cap halved until {until}"

    if bucket not in (SENDER, BURNED):
        return ""

    target = pool.resolve(mailbox) if pool is not None else None
    if target is None or not target.email:
        await engage_kill_switch(
            state,
            f"{code} ({bucket.lower()} block) came back but not to a mailbox Mercury "
            "knows, so no single mailbox can be paused. Sending is stopped.",
        )
        return "global kill switch (mailbox unknown)"

    mailboxes = [target]
    if bucket == BURNED:
        mailboxes = [mb for mb in pool.mailboxes if mb.domain and mb.domain == target.domain] \
            or [target]
    try:
        for mb in mailboxes:
            await warmup.set_paused(state, mb.email, _pause_reason(bucket, code, target.email))
        if bucket == BURNED:
            await state.set_setting(verdict_key(target.domain), json.dumps({
                "verdict": VERDICT_CANCEL_CANDIDATE,
                "code": code,
                "mailbox": target.email,
                "at": _iso(_now()),
            }))
    except Exception as e:
        await engage_kill_switch(
            state, f"{code} ({bucket.lower()} block) on {target.email} but the pause "
                   f"could not be saved ({e}). Sending is stopped.")
        return "global kill switch (pause failed)"
    if bucket == BURNED:
        return f"domain {target.domain} paused, verdict {VERDICT_CANCEL_CANDIDATE}"
    return f"{target.email} paused"
