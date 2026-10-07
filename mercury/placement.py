"""Inbox placement test: where does email 1 actually land?

Zero replies on a young domain can be the copy, the list, or spam
placement, and the outbox cannot tell which. This test can. It sends the
campaign's real email 1 (with the legal footer, exactly as a prospect gets
it) from every configured mailbox to a few seed inboxes the operator owns
(Gmail, Outlook, Yahoo), and, as a control, the same text from a mailbox
with a known-good reputation (a personal Gmail). Then it reads the seeds
over IMAP and records where each copy landed: Primary, Promotions (or
another Gmail tab), Inbox (non-Gmail), Spam, or not found.

Decision rule (``summarize``):

    fleet mostly in spam, control in the inbox  -> domain / reputation problem
    fleet and control both in spam              -> copy problem
    fleet in spam, no control result            -> assume the copy
    fleet mostly in the inbox                   -> placement is fine; look at
                                                   the copy and the list

"Mostly" is at least half of the copies found. Copies not found are left
out of the shares (they may still be in transit; ``check`` reads again).

Isolation: the test never touches the prospect outbox. It sends straight
through each mailbox's provider, so nothing it sends counts toward a daily
cap, a warm-up ramp, the trends or the health verdict. Results live in the
``placement_tests`` table (one row per sender and seed, with the folder it
landed in) and the last run's summary line in the ``settings`` table.

The table is created here, idempotently, instead of as a numbered migration,
so it cannot collide with migrations added on other branches.

Seeds Mercury cannot read (no app password; Outlook.com has moved IMAP to
OAuth and may refuse one) stay "not checked"; record them by hand with
``mercury mail placement mark``.
"""

from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger("mercury.placement")

LAST_RUN_KEY = "placement:last_run"

FOLDER_LABELS = {
    "primary": "Primary",
    "inbox": "Inbox",
    "promotions": "Promotions",
    "other_tab": "Other tab",
    "spam": "Spam",
    "missing": "Not found",
    "unchecked": "Not checked",
    "send_failed": "Send failed",
}
FOLDERS = tuple(FOLDER_LABELS)
INBOX_FOLDERS = {"primary", "inbox"}
TAB_FOLDERS = {"promotions", "other_tab"}
LANDED = INBOX_FOLDERS | TAB_FOLDERS
PENDING = {"unchecked", "missing"}
# Folders an operator may record by hand.
MARKABLE = ("primary", "inbox", "promotions", "other_tab", "spam", "missing")

OUTCOMES = {
    "fleet_inbox": ("Lands in the inbox", "good"),
    "domain": ("Domain problem", "bad"),
    "copy": ("Copy problem", "waiting"),
    "copy_assumed": ("Probably the copy", "waiting"),
    "inconclusive": ("Inconclusive", "idle"),
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS placement_tests (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        sender TEXT NOT NULL,
        role TEXT NOT NULL,
        domain TEXT DEFAULT '',
        seed TEXT NOT NULL,
        seed_provider TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        folder TEXT DEFAULT 'unchecked',
        detail TEXT DEFAULT '',
        sent_at TIMESTAMP,
        checked_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_placement_run ON placement_tests(run_id)",
)


class PlacementError(Exception):
    """The test can't run as configured; the message says what to fix."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[1].lower() if "@" in (email or "") else ""


# ── Providers: hosts guessed from the address ───────────────────────

# provider -> (imap host, smtp host, second-level labels it owns)
_PROVIDERS = {
    "gmail": ("imap.gmail.com", "smtp.gmail.com", ("gmail", "googlemail")),
    "outlook": ("outlook.office365.com", "smtp-mail.outlook.com",
                ("outlook", "hotmail", "live", "msn")),
    "yahoo": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", ("yahoo", "ymail", "rocketmail")),
    "icloud": ("imap.mail.me.com", "smtp.mail.me.com", ("icloud", "me", "mac")),
}


def guess_provider(email: str) -> str:
    label = _domain(email).split(".")[0]
    for name, (_imap, _smtp, labels) in _PROVIDERS.items():
        if label in labels:
            return name
    return "other"


class Seed:
    """A seed inbox, resolved from config + env."""

    def __init__(self, cfg, env):
        self.email = cfg.email
        self.provider = (cfg.provider or "").strip().lower() or guess_provider(cfg.email)
        hosts = _PROVIDERS.get(self.provider)
        self.imap_host = cfg.imap_host or (hosts[0] if hosts else "")
        self.imap_port = int(cfg.imap_port or 993)
        self.username = cfg.username or cfg.email
        self.password = env.secret(cfg.password_env) if cfg.password_env else ""
        self.password_env = cfg.password_env

    @property
    def readable(self) -> bool:
        return bool(self.imap_host and self.password)


def seeds_from_config(config, env) -> list[Seed]:
    placement = getattr(config.channels.email, "placement", None)
    return [Seed(s, env) for s in (getattr(placement, "seeds", None) or [])]


def control_provider(config, env):
    """(address, provider, ready) for the control sender, or None when none
    is configured. ``ready`` is False without a password or an SMTP host."""
    placement = getattr(config.channels.email, "placement", None)
    control = getattr(placement, "control", None)
    if control is None:
        return None
    from mercury.config import MailboxConfig
    from mercury.integrations.smtp_mail import SmtpImapProvider

    hosts = _PROVIDERS.get(guess_provider(control.email))
    host = control.smtp_host or (hosts[1] if hosts else "")
    mailbox = MailboxConfig(
        email=control.email, name=control.name, username=control.username,
        # MailboxConfig defaults to SMTP_PASSWORD, the sending mailbox's
        # password; a control without its own must read as unconfigured.
        password_env=control.password_env or "PLACEMENT_UNSET",
        smtp_host=host or "smtp.invalid",
        smtp_port=int(control.smtp_port or 587),
    )
    provider = SmtpImapProvider(config, env, mailbox=mailbox)
    return control.email, provider, bool(host) and provider.is_configured()


# ── Reading a seed inbox over IMAP ───────────────────────────────────

_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_JUNK_NAMES = ("[gmail]/spam", "[google mail]/spam", "junk", "junk email", "junk e-mail",
               "bulk mail", "bulk", "spam")
_GMAIL_TABS = (("promotions", "promotions"), ("social", "other_tab"),
               ("updates", "other_tab"), ("forums", "other_tab"))


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean_mid(mid: str) -> str:
    return re.sub(r'["\\\s]', "", mid or "")


def find_junk_folder(list_lines) -> str | None:
    """The spam folder's name from an IMAP LIST response: the one flagged
    \\Junk (RFC 6154), else a well-known name."""
    names = []
    for raw in list_lines or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "\\junk" in m.group("flags").lower():
            return name
        names.append(name)
    for wanted in _JUNK_NAMES:
        for name in names:
            if name.lower() == wanted:
                return name
    return None


class ImapSeedReader:
    """Finds test emails in a seed inbox by Message-ID. Read-only: folders
    are opened with EXAMINE and messages are never fetched or flagged."""

    def __init__(self, seed: Seed, connect=None):
        self.seed = seed
        self._connect = connect or (lambda host, port: imaplib.IMAP4_SSL(host, port, timeout=30))

    @staticmethod
    def _has(conn, mid: str) -> bool:
        typ, data = conn.search(None, "HEADER", "Message-ID", _imap_quote(_clean_mid(mid)))
        return typ == "OK" and bool(data and data[0] and data[0].split())

    @staticmethod
    def _gmail_tab(conn, mid: str) -> str:
        bare = _clean_mid(mid).strip("<>")
        for category, folder in _GMAIL_TABS:
            typ, data = conn.search(None, "X-GM-RAW",
                                    _imap_quote(f"rfc822msgid:{bare} category:{category}"))
            if typ == "OK" and data and data[0] and data[0].split():
                return folder
        return "primary"

    def locate(self, message_ids: list[str]) -> dict[str, str]:
        """``{message_id: folder}`` for the ids found; absent ids weren't."""
        conn = self._connect(self.seed.imap_host, self.seed.imap_port)
        found: dict[str, str] = {}
        try:
            conn.login(self.seed.username, self.seed.password)
            _typ, listing = conn.list()
            junk = find_junk_folder(listing)
            typ, _ = conn.select("INBOX", readonly=True)
            if typ == "OK":
                for mid in message_ids:
                    if self._has(conn, mid):
                        found[mid] = (self._gmail_tab(conn, mid) if self.seed.provider == "gmail"
                                      else "inbox")
            rest = [m for m in message_ids if m not in found]
            if rest and junk:
                typ, _ = conn.select(_imap_quote(junk), readonly=True)
                if typ == "OK":
                    for mid in rest:
                        if self._has(conn, mid):
                            found[mid] = "spam"
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return found


# ── Storage ──────────────────────────────────────────────────────────


async def ensure_schema(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        for statement in SCHEMA:
            await db.execute(statement)
        await db.commit()


async def _insert(db_path: str, row: dict) -> None:
    cols = ("id", "run_id", "sender", "role", "domain", "seed", "seed_provider",
            "message_id", "subject", "folder", "detail", "sent_at")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"INSERT INTO placement_tests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(row.get(c, "") for c in cols),
        )
        await db.commit()


async def _set_folder(db_path: str, row_id: str, folder: str, detail: str = "") -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE placement_tests SET folder = ?, detail = ?, checked_at = ? WHERE id = ?",
            (folder, detail, _utcnow_iso(), row_id),
        )
        await db.commit()


async def get_rows(db_path: str, run_id: str) -> list[dict]:
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM placement_tests WHERE run_id = ? ORDER BY role DESC, sender, seed",
                (run_id,),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]
    except aiosqlite.OperationalError:   # no table yet: no test has run
        return []


async def resolve_run(state, run_id: str = "") -> str:
    """A run id from a unique prefix, or the last run when empty."""
    if not run_id:
        raw = await state.get_setting(LAST_RUN_KEY)
        try:
            return (json.loads(raw) or {}).get("run_id", "") if raw else ""
        except json.JSONDecodeError:
            return ""
    try:
        async with aiosqlite.connect(state.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT run_id FROM placement_tests WHERE run_id LIKE ? LIMIT 2",
                (run_id + "%",),
            ) as cursor:
                hits = [r[0] for r in await cursor.fetchall()]
    except aiosqlite.OperationalError:
        return ""
    return hits[0] if len(hits) == 1 else ""


# ── The decision rule (pure) ─────────────────────────────────────────


def _side(rows: list[dict]) -> dict:
    c = {f: 0 for f in FOLDERS}
    for r in rows:
        c[r.get("folder") or "unchecked"] = c.get(r.get("folder") or "unchecked", 0) + 1
    inbox = sum(c[f] for f in INBOX_FOLDERS)
    tabs = sum(c[f] for f in TAB_FOLDERS)
    return {"total": len(rows), "inbox": inbox, "promotions": c["promotions"],
            "other_tab": c["other_tab"], "tabs": tabs, "landed": inbox + tabs,
            "spam": c["spam"], "found": inbox + tabs + c["spam"], "missing": c["missing"],
            "unchecked": c["unchecked"], "failed": c["send_failed"]}


def summarize(rows: list[dict]) -> dict:
    """The decision rule over one run's rows (or one domain's rows plus
    the control's). Returns ``{outcome, label, tone, text, fleet, control}``."""
    fleet = _side([r for r in rows if r.get("role") == "fleet"])
    control_rows = [r for r in rows if r.get("role") == "control"]
    control = _side(control_rows)

    def of(n: int, side: dict) -> str:
        return f"{n} of {side['found']}"

    if not fleet["found"]:
        outcome = "inconclusive"
        if fleet["total"] and fleet["failed"] == fleet["total"]:
            text = "Every test email failed to send, so there is nothing to read."
        else:
            text = ("None of the test emails from your mailboxes were found yet. Read the seeds "
                    "again later (mercury mail placement check), or record the folders by hand.")
    elif fleet["spam"] * 2 < fleet["found"]:
        outcome = "fleet_inbox"
        extras = []
        if fleet["tabs"]:
            extras.append(f"{fleet['tabs']} in Promotions or another tab")
        if fleet["spam"]:
            extras.append(f"{fleet['spam']} in spam")
        text = (f"Your mailboxes land in the inbox: {of(fleet['landed'], fleet)} found"
                + (f" ({', '.join(extras)})" if extras else "") + ". If replies are still low, "
                "the copy or the list is the problem, not placement.")
    elif control["found"] and control["spam"] * 2 < control["found"]:
        outcome = "domain"
        text = (f"Domain or reputation problem: {of(fleet['spam'], fleet)} from your mailboxes "
                "landed in spam, while the same text from the control landed in the inbox.")
    elif control["found"]:
        outcome = "copy"
        text = (f"Copy problem: the text lands in spam from the control sender too "
                f"({of(control['spam'], control)}). Rewrite it and test again.")
    else:
        outcome = "copy_assumed"
        why = ("The control email wasn't found" if control_rows
               else "No control sender is configured")
        text = (f"{of(fleet['spam'], fleet)} from your mailboxes landed in spam. {why}, so assume "
                "the copy first: rewrite it and test again.")
    label, tone = OUTCOMES[outcome]
    return {"outcome": outcome, "label": label, "tone": tone, "text": text,
            "fleet": fleet, "control": control}


# Worst first: a run's overall outcome is its worst domain's.
_SEVERITY = ("domain", "copy", "copy_assumed", "inconclusive", "fleet_inbox")
_SHORT = {
    "fleet_inbox": "lands in the inbox",
    "domain": "lands in spam while the control lands in the inbox (a domain problem)",
    "copy": "lands in spam, and so does the control (a copy problem)",
    "copy_assumed": "lands in spam and there is no control result (assume the copy)",
    "inconclusive": "wasn't found in the seeds yet",
}


def summarize_run(rows: list[dict]) -> tuple[dict, dict]:
    """(overall summary, {domain: summary + rows}). Each fleet domain is
    judged on its own rows against the same control, so one burned domain
    can't hide behind a healthy one. With several domains that disagree,
    the overall outcome is the worst one and the text names each domain."""
    control = [r for r in rows if r.get("role") == "control"]
    domains = {}
    for dom in sorted({r.get("domain", "") for r in rows if r.get("role") == "fleet"}):
        mine = [r for r in rows if r.get("role") == "fleet" and r.get("domain", "") == dom]
        domains[dom] = {**summarize(mine + control), "rows": mine}
    overall = summarize(rows)
    outcomes = {d["outcome"] for d in domains.values()}
    if len(outcomes) > 1:
        worst = min(outcomes, key=_SEVERITY.index)
        label, tone = OUTCOMES[worst]
        overall = {**overall, "outcome": worst, "label": label, "tone": tone,
                   "text": "Results differ by domain. " + " ".join(
                       f"{dom} {_SHORT[d['outcome']]}." for dom, d in sorted(
                           domains.items(), key=lambda kv: _SEVERITY.index(kv[1]["outcome"])))}
    return overall, domains


def summary_line(run_id: str, summary: dict) -> str:
    return f"Placement {run_id}: {summary['label']}. {summary['text']}"


# ── Running a test ───────────────────────────────────────────────────


async def pick_email(state, outbox_id: str = "") -> dict | None:
    """The email 1 to test: a given outbox row, else the newest step-1
    sequence email that is drafted, approved or sent (the current copy)."""
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        if outbox_id:
            sql = ("SELECT id, subject, body, step, kind FROM outbox WHERE id LIKE ? "
                   "ORDER BY created_at DESC LIMIT 2")
            params: tuple = (outbox_id + "%",)
        else:
            sql = ("SELECT id, subject, body, step, kind FROM outbox WHERE kind = 'sequence' "
                   "AND step = 1 AND status IN ('pending_review', 'approved', 'sending', 'sent') "
                   "AND body != '' ORDER BY datetime(created_at) DESC, rowid DESC LIMIT 1")
            params = ()
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
    if outbox_id and len(rows) != 1:
        return None
    return rows[0] if rows else None


def plan(config, env, pool, only: list[str] | None = None) -> dict:
    """Who sends to whom, without sending: ``{fleet, control, seeds,
    problems}``. ``only`` keeps mailboxes whose address or domain matches."""
    wanted = {o.strip().lower() for o in (only or []) if o.strip()}
    fleet = []
    if pool is not None:
        for mb in pool.mailboxes:
            if wanted and mb.email not in wanted and mb.domain not in wanted:
                continue
            fleet.append({"email": mb.email, "domain": mb.domain, "provider": mb.provider,
                          "configured": mb.provider.is_configured()})
    seeds = seeds_from_config(config, env)
    control = control_provider(config, env)
    problems = []
    if pool is None:
        problems.append("The placement test needs a native mail provider "
                        "(channels.email.provider: gmail or smtp).")
    elif not any(f["configured"] for f in fleet):
        problems.append("No sending mailbox with a password matches." if wanted
                        else "No sending mailbox has its password set.")
    if not seeds:
        problems.append("No seed inboxes. Add channels.email.placement.seeds in your config "
                        "(a Gmail, an Outlook and a Yahoo address you own).")
    if not (getattr(config.compliance, "postal_address", "") or "").strip():
        problems.append("compliance.postal_address is empty, so email 1 can't carry its "
                        "legal footer (the sender holds real email for the same reason).")
    return {
        "fleet": fleet,
        "control": ({"email": control[0], "provider": control[1], "configured": control[2]}
                    if control else None),
        "seeds": seeds,
        "problems": problems,
    }


async def run(state, config, env, pool, *, subject: str, body: str,
              only: list[str] | None = None, readers: dict | None = None,
              wait_seconds: int | None = None, poll_seconds: int = 30,
              gap_seconds: float = 2.0, sleep=asyncio.sleep, progress=None) -> dict:
    """Send the test, read the seeds until everything is found or the wait
    is over, store the rows and the summary. Returns ``{run_id, rows,
    summary}``. Never touches the outbox."""
    from mercury.agents.sender import with_legal_footer

    p = plan(config, env, pool, only)
    if p["problems"]:
        raise PlacementError(" ".join(p["problems"]))
    say = progress or (lambda _msg: None)
    body_out = with_legal_footer(config, body)
    senders = [(f["email"], f["domain"], "fleet", f["provider"])
               for f in p["fleet"] if f["configured"]]
    if p["control"] and p["control"]["configured"]:
        c = p["control"]
        senders.append((c["email"], _domain(c["email"]), "control", c["provider"]))
    elif p["control"]:
        say(f"Control {p['control']['email']} has no password or SMTP host set; "
            "running without it.")

    await ensure_schema(state.db_path)
    run_id = uuid.uuid4().hex[:8]
    first = True
    for address, domain, role, provider in senders:
        for seed in p["seeds"]:
            if not first and gap_seconds:
                await sleep(gap_seconds)
            first = False
            try:
                result = await provider.send_email(seed.email, subject, body_out)
            except Exception as e:  # noqa: BLE001 — one failed send must not stop the test
                result = None
                error = f"{type(e).__name__}: {e}"
            else:
                error = "" if result.ok else (result.error or "send failed")
            ok = result is not None and result.ok
            await _insert(state.db_path, {
                "id": uuid.uuid4().hex, "run_id": run_id, "sender": address, "role": role,
                "domain": domain, "seed": seed.email, "seed_provider": seed.provider,
                "message_id": result.message_id if ok else "", "subject": subject,
                "folder": "unchecked" if ok else "send_failed", "detail": error[:300],
                "sent_at": _utcnow_iso() if ok else None,
            })
            say(f"{'Sent' if ok else 'Failed'}: {address} -> {seed.email}"
                + (f" ({error[:120]})" if not ok else ""))

    wait = wait_seconds
    if wait is None:
        wait = getattr(getattr(config.channels.email, "placement", None), "wait_seconds", 180)
    readers = readers if readers is not None else default_readers(p["seeds"])
    if readers:
        say(f"Looking for the emails in {len(readers)} seed inbox"
            f"{'' if len(readers) == 1 else 'es'} for up to {int(wait)} seconds.")
        attempts = max(1, -(-int(max(0, wait)) // max(1, poll_seconds)))
        for _ in range(attempts):
            if wait:
                await sleep(min(poll_seconds, wait))
            if not await check(state, run_id, readers):
                break
    return await finish(state, run_id)


def default_readers(seeds: list[Seed]) -> dict:
    return {s.email: ImapSeedReader(s) for s in seeds if s.readable}


async def check(state, run_id: str, readers: dict) -> int:
    """Read the seeds once for this run's copies not found yet. Returns how
    many are still pending in seeds that can be read."""
    rows = await get_rows(state.db_path, run_id)
    by_seed: dict[str, list[dict]] = {}
    for r in rows:
        if r["folder"] in PENDING and r["message_id"] and r["seed"] in readers:
            by_seed.setdefault(r["seed"], []).append(r)
    pending = 0
    for seed, items in by_seed.items():
        try:
            found = await asyncio.to_thread(readers[seed].locate, [r["message_id"] for r in items])
        except Exception as e:  # noqa: BLE001 — a seed that can't be read stays unchecked
            logger.warning(f"Placement: couldn't read {seed}: {e}")
            for r in items:
                await _set_folder(state.db_path, r["id"], r["folder"],
                                  f"couldn't read the seed: {str(e)[:200]}")
            continue
        for r in items:
            folder = found.get(r["message_id"], "missing")
            pending += folder == "missing"
            await _set_folder(state.db_path, r["id"], folder)
    return pending


async def finish(state, run_id: str) -> dict:
    rows = await get_rows(state.db_path, run_id)
    summary, _domains = summarize_run(rows)
    created = min((r["created_at"] for r in rows if r.get("created_at")), default=_utcnow_iso())
    await state.set_setting(LAST_RUN_KEY, json.dumps({
        "run_id": run_id, "created_at": created, "outcome": summary["outcome"],
        "line": summary_line(run_id, summary),
    }))
    return {"run_id": run_id, "rows": rows, "summary": summary}


async def mark(state, run_id: str, seed: str, sender: str, folder: str) -> int:
    """Record by hand where a copy landed. Returns the rows updated."""
    if folder not in MARKABLE:
        raise PlacementError(f"folder must be one of {', '.join(MARKABLE)}")
    rows = await get_rows(state.db_path, run_id)
    seed, sender = seed.strip().lower(), sender.strip().lower()
    hits = [r for r in rows if r["seed"] == seed
            and (r["sender"] == sender or (sender == "control" and r["role"] == "control"))
            and r["folder"] != "send_failed"]
    for r in hits:
        await _set_folder(state.db_path, r["id"], folder, "recorded by hand")
    if hits:
        await finish(state, run_id)
    return len(hits)


async def report(state, run_id: str = "") -> dict | None:
    """The last (or a given) run for the dashboard and ``show``: rows, the
    overall summary and one summary per fleet domain (its rows judged
    against the same control)."""
    run_id = await resolve_run(state, run_id)
    if not run_id:
        return None
    rows = await get_rows(state.db_path, run_id)
    if not rows:
        return None
    control = [r for r in rows if r["role"] == "control"]
    summary, domains = summarize_run(rows)
    return {
        "run_id": run_id,
        "created_at": min((r["created_at"] for r in rows if r.get("created_at")), default=None),
        "subject": rows[0].get("subject", ""),
        "summary": summary,
        "line": summary_line(run_id, summary),
        "rows": rows,
        "control": control,
        "domains": domains,
        "folder_labels": FOLDER_LABELS,
    }


# ── Text rendering (mercury mail placement) ──────────────────────────


def format_report(rep: dict) -> str:
    rows = rep.get("rows") or []
    lines = ["", f"  Placement test {rep['run_id']}  ({(rep.get('created_at') or '')[:16]})"]
    if rep.get("subject"):
        lines.append(f"  Subject: {rep['subject']}")
    lines.append("  " + "=" * 72)
    w1 = max(12, *(len(r["sender"]) for r in rows)) if rows else 12
    w2 = max(10, *(len(r["seed"]) for r in rows)) if rows else 10
    lines.append(f"  {'From':<{w1 + 10}} {'Seed':<{w2}}  Folder")
    for r in rows:
        who = r["sender"] + (" (control)" if r["role"] == "control" else "")
        note = f"  {r['detail']}" if r.get("detail") else ""
        lines.append(f"  {who:<{w1 + 10}} {r['seed']:<{w2}}  "
                     f"{FOLDER_LABELS.get(r['folder'], r['folder'])}{note}")
    s = rep["summary"]
    lines += ["", f"  {s['label']}. {s['text']}"]
    domains = rep.get("domains") or {}
    if len(domains) > 1:
        for dom, d in domains.items():
            lines.append(f"    {dom}: {d['label']}. {d['text']}")
    if any(r["folder"] == "unchecked" for r in rows):
        lines.append("  Seeds Mercury can't read stay 'Not checked'. Record them with: "
                     f"mercury mail placement mark {rep['run_id']} --seed SEED --sender FROM "
                     "--folder spam")
    lines.append("")
    return "\n".join(lines)
