"""SQLite state manager. All of Mercury's memory lives here.

Concurrency: the DB runs in WAL mode (set persistently at init) so the
dashboard can read while the agent writes. Every connection gets a busy
timeout so concurrent writers wait instead of raising "database is locked".

Migrations: schema changes are applied via a linear, idempotent migration
list tracked with SQLite's ``PRAGMA user_version`` so existing user DBs
upgrade cleanly in place.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import aiosqlite

from mercury.draft_rules import count_words, decode_flags, draft_flags, encode_flags, recheck_flags
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.models.campaign import Campaign, EmailStep  # noqa: F401 (EmailStep re-exported)
from mercury.models.conversation import Conversation, Message  # noqa: F401

from mercury.paths import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "mercury.db"

# How long (seconds) a connection waits on a locked database before failing.
BUSY_TIMEOUT_SECONDS = 30.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    """Naive UTC now (matches how timestamps are stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _split_sql(script: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` would be simpler but it COMMITs first, which breaks
    the single-transaction guarantee. ``complete_statement`` keeps trigger
    bodies (``BEGIN ... ; ... END;``) in one piece.
    """
    import sqlite3

    statements, buf = [], ""
    for part in script.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \n\t;"):
                statements.append(buf.strip())
            buf = ""
    if buf.strip(" \n\t;"):
        statements.append(buf.strip())
    return statements


def _norm(value: str | None) -> str:
    """Normalize an identity key (email/domain) for dedup: strip + lowercase."""
    return (value or "").strip().lower()


# ── Schema migrations ─────────────────────────────────────────────────
# Each entry is an idempotent SQL script. The index into this list + 1 is
# the schema version stored in PRAGMA user_version. Never edit or reorder
# released migrations — append new ones.

MIGRATIONS: list[str] = [
    # ── v1: base schema (idempotent, so pre-migration DBs adopt cleanly) ──
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        description TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        phone_verified INTEGER DEFAULT 0,
        linkedin_url TEXT DEFAULT '',
        title TEXT DEFAULT '',
        seniority TEXT DEFAULT '',
        department TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        company TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        instantly_campaign_id TEXT DEFAULT '',
        sequence_json TEXT DEFAULT '[]',
        prospect_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
    CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
    CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
    CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
    CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
    """,
    # ── v2: normalize identity keys, dedup, uniqueness, hot-path indexes ──
    """
    -- Normalize emails/domains so uniqueness is case-insensitive going forward.
    UPDATE prospects SET email = LOWER(TRIM(email)) WHERE email != LOWER(TRIM(email));
    UPDATE companies SET domain = LOWER(TRIM(domain)) WHERE domain != LOWER(TRIM(domain));

    -- Dedup existing rows (keep the earliest) so unique indexes can be built.
    DELETE FROM prospects WHERE email != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE email != '' GROUP BY email
    );
    DELETE FROM prospects WHERE linkedin_url != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE linkedin_url != '' GROUP BY linkedin_url
    );
    DELETE FROM companies WHERE domain != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM companies WHERE domain != '' GROUP BY domain
    );

    -- Enforce uniqueness at the DB level (partial: blank values allowed).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_email
        ON prospects(email) WHERE email != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_linkedin
        ON prospects(linkedin_url) WHERE linkedin_url != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_domain
        ON companies(domain) WHERE domain != '';

    -- Hot-path indexes: campaign stats subqueries, reply handling, dedup checks.
    CREATE INDEX IF NOT EXISTS idx_conversations_campaign_id ON conversations(campaign_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_prospect_id ON conversations(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_intent ON conversations(intent);
    CREATE INDEX IF NOT EXISTS idx_prospects_name_company
        ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
    CREATE INDEX IF NOT EXISTS idx_prospects_status_updated ON prospects(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);
    """,
    # ── v3: per-call usage accounting (tokens, cost, attribution) ──
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        request_key TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER DEFAULT 0,
        num_turns INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0,
        source TEXT DEFAULT 'result_json'
    );

    CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_usage_events_agent ON usage_events(agent);
    CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
    -- Transcript-backfilled rows carry a request_key; uniqueness makes
    -- reconciliation idempotent (INSERT OR IGNORE).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_request
        ON usage_events(request_key) WHERE request_key != '';
    """,
    # ── v4: honest email statuses + per-domain pattern cache ──
    """
    -- verified: a provider confirmed the mailbox exists
    -- risky:    catch-all / accept-all domain — sendable only in low volume
    -- guess:    pattern guess, never verified — never auto-sent
    -- invalid:  provider said undeliverable
    ALTER TABLE prospects ADD COLUMN email_status TEXT DEFAULT '';

    -- Backfill: the old email_verified flag over-reported (pattern guesses
    -- at any MX-bearing domain were marked verified), so every existing
    -- address is downgraded to an honest 'guess' and must re-verify.
    UPDATE prospects SET email_status = 'guess', email_verified = 0
        WHERE email != '';

    CREATE TABLE IF NOT EXISTS email_patterns (
        domain TEXT PRIMARY KEY,
        pattern TEXT DEFAULT '',
        source TEXT DEFAULT '',
        confidence REAL DEFAULT 0.0,
        mx_type TEXT DEFAULT '',
        is_catch_all INTEGER DEFAULT -1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v5: buying signals on companies (tech stack, hiring, etc.) ──
    """
    ALTER TABLE companies ADD COLUMN tech_stack_json TEXT DEFAULT '[]';
    ALTER TABLE companies ADD COLUMN signals_json TEXT DEFAULT '[]';
    """,
    # ── v6: outbox (approval ladder + native sending) and settings KV ──
    """
    -- Every outgoing email becomes an outbox row first. Status ladder:
    --   pending_review -> approved -> sent
    --   (or rejected / cancelled / failed)
    -- The unique index on (campaign_id, prospect_id, step) is the
    -- double-send guard: retries and re-stages physically cannot
    -- duplicate a send.
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY,
        campaign_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        step INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'sequence',
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        send_at TIMESTAMP,
        sent_at TIMESTAMP,
        provider TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        thread_ref TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_campaign_step
        ON outbox(campaign_id, prospect_id, step)
        WHERE campaign_id != '' AND kind = 'sequence';
    CREATE INDEX IF NOT EXISTS idx_outbox_status_send_at ON outbox(status, send_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_prospect ON outbox(prospect_id);

    -- Simple key/value store for operational flags (kill switch, counters).
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v7: observation model — every fact is a row, never a column ──
    """
    -- The governed vocabulary. A collector may only emit a code that exists
    -- here (enforced by the observations FK), so a typo fails loudly instead
    -- of quietly inventing a junk signal. `status` is the user-confirmation
    -- gate: Mercury PROPOSES signals, the user confirms which ones to
    -- prospect against, and only confirmed signals get collected.
    CREATE TABLE IF NOT EXISTS signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        status TEXT DEFAULT 'proposed',
        confidence_floor REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- One row per fact. Confidence and provenance travel WITH the fact, and
    -- re-observing the same signal over time is a free time series.
    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_obs_company ON observations(company_id);
    CREATE INDEX IF NOT EXISTS idx_obs_signal ON observations(signal_code);
    CREATE INDEX IF NOT EXISTS idx_obs_company_signal
        ON observations(company_id, signal_code, observed_at);
    CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);

    -- The governed-vocabulary guarantee. A trigger (not a foreign key) so it
    -- holds regardless of the per-connection foreign_keys pragma, and applies
    -- only here: the legacy tables default several id columns to '' and would
    -- break under blanket FK enforcement.
    CREATE TRIGGER IF NOT EXISTS trg_observations_signal_known
    BEFORE INSERT ON observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in signal_codes vocabulary');
    END;

    -- A log of what ran, when, how much it produced and cost. Purely a
    -- record: spend is capped inside the collector, never here.
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_runs_stage_started ON runs(stage, started_at);
    """,
    # ── v8: discovery — entity resolution for businesses without a website ──
    """
    -- The best prospect for anyone selling websites is a business that has
    -- none, so `domain` cannot be the only identity key. `external_id` holds
    -- the provider's stable id ("dataforseo:ChIJ...", "osm:node/123") and is
    -- what dedups a re-run for those records.
    ALTER TABLE companies ADD COLUMN external_id TEXT DEFAULT '';
    ALTER TABLE companies ADD COLUMN phone TEXT DEFAULT '';

    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_external
        ON companies(external_id) WHERE external_id != '';
    """,
    # ── v9: mailbox rotation ──
    """
    -- The address an outbox row goes out from. Set when step 1 is sent (or
    -- when a reply is queued, from the inbox it answers) and inherited by
    -- the rest of the thread. '' = sent before mailbox tracking existed.
    ALTER TABLE outbox ADD COLUMN mailbox TEXT DEFAULT '';
    """,
    # ── v10: inbox warm-up overlay + event-log indexes for trends ──
    """
    -- Which inboxes exist, their caps and their warm-up ramp come from
    -- channels.email.mailboxes in mercury.yaml. This table is only an
    -- overlay keyed by mailbox address: a manual or automatic pause, the
    -- warm-up checklist ({task_key: true}) and free-form notes.
    -- `resumed_at` restarts the health window after a manual resume, so an
    -- old bounce spike can't immediately re-pause a fixed inbox.
    CREATE TABLE IF NOT EXISTS warmup_inboxes (
        email TEXT PRIMARY KEY,
        status TEXT DEFAULT 'active',
        notes TEXT DEFAULT '',
        tasks_json TEXT DEFAULT '{}',
        paused_at TIMESTAMP,
        pause_reason TEXT DEFAULT '',
        resumed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Trends count reply/bounce events out of the actions log by type + day.
    CREATE INDEX IF NOT EXISTS idx_actions_type_created
        ON actions(action_type, created_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_sent_at ON outbox(status, sent_at);
    """,
    # ── v11: versioned writing personas and immutable generation history ──
    """
    CREATE TABLE personas (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        avatar_seed TEXT NOT NULL,
        archived INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE persona_versions (
        id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL REFERENCES personas(id),
        revision INTEGER NOT NULL,
        tone TEXT NOT NULL,
        instructions TEXT DEFAULT '',
        examples TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(persona_id, revision)
    );
    CREATE TABLE email_generations (
        id TEXT PRIMARY KEY,
        persona_version_id TEXT NOT NULL REFERENCES persona_versions(id),
        persona_json TEXT NOT NULL,
        config_json TEXT NOT NULL,
        prompt TEXT NOT NULL,
        output_json TEXT NOT NULL,
        task TEXT NOT NULL,
        instruction TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE email_generation_history (
        outbox_id TEXT NOT NULL REFERENCES outbox(id),
        generation_id TEXT NOT NULL REFERENCES email_generations(id),
        original_subject TEXT NOT NULL,
        original_body TEXT NOT NULL,
        attached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(outbox_id, generation_id)
    );
    ALTER TABLE outbox ADD COLUMN generation_id TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN manually_edited INTEGER DEFAULT 0;
    CREATE INDEX idx_generation_version ON email_generations(persona_version_id);
    """,
    # ── v12: a voice and a sign-off per mailbox ──
    """
    -- Who a mailbox writes as. persona_id '' follows the default persona.
    -- sign_name is the name its emails are signed with; '' uses the
    -- persona's suggested sign-off name.
    CREATE TABLE mailbox_voices (
        email TEXT PRIMARY KEY,
        persona_id TEXT DEFAULT '',
        sign_name TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- A persona's suggested sign-off name, for mailboxes that set none.
    ALTER TABLE personas ADD COLUMN sign_name TEXT DEFAULT '';
    -- A campaign written for one mailbox keeps every step on it.
    ALTER TABLE campaigns ADD COLUMN mailbox TEXT DEFAULT '';
    """,
    # ── v13: CSV import batches ──
    """
    -- One row per committed import. The fingerprint covers the file and
    -- every choice made about it, so committing the same thing twice
    -- returns the first result instead of importing again.
    CREATE TABLE import_batches (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        filename TEXT DEFAULT '',
        origin TEXT DEFAULT '',
        policy TEXT DEFAULT 'skip',
        total_rows INTEGER DEFAULT 0,
        created INTEGER DEFAULT 0,
        filled INTEGER DEFAULT 0,
        skipped INTEGER DEFAULT 0,
        excluded INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- What happened to each data row. Outcomes and ids only: the imported
    -- values themselves live in prospects, or nowhere.
    CREATE TABLE import_rows (
        batch_id TEXT NOT NULL REFERENCES import_batches(id),
        row_number INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        PRIMARY KEY(batch_id, row_number)
    );
    ALTER TABLE prospects ADD COLUMN import_batch_id TEXT DEFAULT '';
    ALTER TABLE prospects ADD COLUMN import_row INTEGER DEFAULT 0;
    CREATE INDEX idx_prospects_import_batch ON prospects(import_batch_id);
    """,
    # ── v14: exclusions, company holds, and the company on each outbox row ──
    """
    -- An exclusion is a rule about an address, not a prospect, so deleting
    -- or re-importing a contact never lifts it. kind 'email' matches one
    -- address; kind 'domain' matches that exact domain, plus its subdomains
    -- only when include_subdomains is set. source keeps unrelated rules
    -- apart: removing a manual rule never clears the same person's opt-out
    -- (one active row per kind, value and source). Rows are never deleted:
    -- removal stamps removed_at, and every change lands in the event log.
    CREATE TABLE suppressions (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('email', 'domain')),
        value TEXT NOT NULL,
        include_subdomains INTEGER DEFAULT 0,
        source TEXT NOT NULL,
        reason TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        removed_at TIMESTAMP,
        removed_by TEXT DEFAULT '',
        removed_note TEXT DEFAULT ''
    );
    CREATE UNIQUE INDEX uq_suppressions_active
        ON suppressions(kind, value, source) WHERE removed_at IS NULL;
    CREATE INDEX idx_suppressions_value ON suppressions(value);
    CREATE TRIGGER trg_suppressions_no_delete BEFORE DELETE ON suppressions
    BEGIN
        SELECT RAISE(ABORT, 'suppressions are removed by stamping removed_at, never deleted');
    END;

    CREATE TABLE suppression_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        suppression_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX idx_suppression_events_rule ON suppression_events(suppression_id);
    CREATE TRIGGER trg_suppression_events_no_update BEFORE UPDATE ON suppression_events
    BEGIN
        SELECT RAISE(ABORT, 'suppression_events is append-only');
    END;
    CREATE TRIGGER trg_suppression_events_no_delete BEFORE DELETE ON suppression_events
    BEGIN
        SELECT RAISE(ABORT, 'suppression_events is append-only');
    END;

    -- Opt-outs recorded before this table existed become rules now.
    INSERT INTO suppressions (id, kind, value, source, reason, prospect_id, created_by)
        SELECT lower(hex(randomblob(6))), 'email', email, 'opt_out',
               'opted out (recorded before exclusions existed)', MIN(id), 'migration'
        FROM prospects WHERE status = 'opted_out' AND email != '' GROUP BY email;
    INSERT INTO suppression_events (suppression_id, action, actor, note)
        SELECT id, 'added', 'migration', reason FROM suppressions;

    -- A temporary pause on cold mail to one company. Not an exclusion: it
    -- ends when someone resumes it, and replies are never held by it.
    CREATE TABLE company_holds (
        id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        prospect_id TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        released_at TIMESTAMP,
        released_by TEXT DEFAULT '',
        released_note TEXT DEFAULT ''
    );
    CREATE UNIQUE INDEX uq_company_holds_active
        ON company_holds(company_id) WHERE released_at IS NULL;

    -- The company an email counts against for the per-company limits, set
    -- when it is staged and refreshed when it is claimed. '' = unknown
    -- company, which no company limit applies to.
    ALTER TABLE outbox ADD COLUMN company_id TEXT DEFAULT '';
    UPDATE outbox SET company_id = COALESCE(
        (SELECT p.company_id FROM prospects p
         JOIN companies c ON c.id = p.company_id
         WHERE p.id = outbox.prospect_id), '');
    -- Contacts with no company_id belong to the company whose domain is
    -- their email's (shared providers never are a company's domain).
    UPDATE outbox SET company_id = COALESCE(
        (SELECT c.id FROM companies c
         WHERE c.domain != '' AND c.domain = substr(outbox.to_email, instr(outbox.to_email, '@') + 1)
           AND c.domain NOT IN ('gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com',
                                'outlook.com', 'live.com', 'aol.com', 'icloud.com')), '')
        WHERE company_id = '';
    CREATE INDEX idx_outbox_company ON outbox(company_id, kind, step, status);
    """,
    # ── v15: excluded mail needs a fresh human decision after requeue ──
    """
    ALTER TABLE outbox ADD COLUMN requires_manual_review INTEGER NOT NULL DEFAULT 0;
    UPDATE outbox SET requires_manual_review = 1 WHERE status = 'blocked';
    """,
    # ── v16: temporary sequence pauses (out-of-office replies) ──
    """
    -- One row per prospect: the current or most recent pause of their cold
    -- sequence. It is separate from prospects.status (the sales stage) and
    -- from the outbox rows (which keep their approval status while paused).
    --   state: paused (resume_at set) | needs_review (no usable return date)
    --          | resumed (ended) | superseded (a reply, opt-out, bounce or
    --          closure ended it)
    -- trigger_message_id / trigger_at identify the inbound message the pause
    -- was read from, so replaying it changes nothing. manual_override marks a
    -- date an operator set; only a newer message than override_at may replace it.
    CREATE TABLE IF NOT EXISTS sequence_pauses (
        id TEXT PRIMARY KEY,
        prospect_id TEXT NOT NULL UNIQUE,
        reason TEXT DEFAULT 'ooo',
        state TEXT NOT NULL,
        review_reason TEXT DEFAULT '',
        trigger_message_id TEXT DEFAULT '',
        trigger_at TIMESTAMP,
        confidence REAL DEFAULT 0,
        return_text TEXT DEFAULT '',
        resume_at TIMESTAMP,
        manual_override INTEGER DEFAULT 0,
        override_at TIMESTAMP,
        ended_at TIMESTAMP,
        ended_reason TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sequence_pauses_state
        ON sequence_pauses(state, resume_at);
    """,
    # ── v17: offer attribution and the per-prospect demo gate ──
    """
    -- The offer an email was written for (offers[].key in mercury.yaml).
    -- '' = written before offers existed: no offer, so no demo gate. An
    -- outbox row inherits its campaign's key when it is queued.
    ALTER TABLE campaigns ADD COLUMN offer_key TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN offer_key TEXT DEFAULT '';
    -- A demo built for one prospect: the answering line or draft site an
    -- offer's email says already exists. Sequence emails of an offer with
    -- requires_demo wait until the prospect's demo is 'ready'.
    --   requested -> ready -> retired
    CREATE TABLE demos (
        id TEXT PRIMARY KEY,
        prospect_id TEXT NOT NULL,
        offer_key TEXT NOT NULL,
        kind TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'requested'
            CHECK (status IN ('requested', 'ready', 'retired')),
        demo_url TEXT DEFAULT '',
        recording_path TEXT DEFAULT '',
        agent_id TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        built_by TEXT DEFAULT '',
        retire_reason TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ready_at TIMESTAMP,
        retired_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- One live demo per prospect and offer; retired ones stay as history.
    CREATE UNIQUE INDEX uq_demos_live ON demos(prospect_id, offer_key)
        WHERE status != 'retired';
    CREATE INDEX idx_demos_status ON demos(status);
    CREATE INDEX idx_outbox_offer ON outbox(offer_key) WHERE offer_key != '';
    """,
    # ── v18: review revisions, approval snapshots, idempotent commands, audit ──
    """
    -- revision counts changes to what a reviewer reads and decides on: the
    -- recipient, the sending mailbox, the text, its generation and a send
    -- time an operator chose. Status changes and the sender's own timing
    -- (follow-up spacing, retries, out-of-office resumes) leave it alone.
    ALTER TABLE outbox ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
    -- The approval snapshot. approved_revision is the revision a reviewer
    -- (or the no-approval policy) approved, approved_hash the content it
    -- covered (outbox_hash below). NULL = not approved. The send claim
    -- requires both to match the row, so a changed draft cannot go out on
    -- an earlier approval.
    ALTER TABLE outbox ADD COLUMN approved_revision INTEGER;
    ALTER TABLE outbox ADD COLUMN approved_hash TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN approved_by TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN approved_at TIMESTAMP;
    -- Mail approved (or mid-send) before snapshots existed keeps its
    -- approval for the content it holds now.
    UPDATE outbox SET approved_revision = revision,
        approved_hash = outbox_hash(to_email, subject, body, mailbox, generation_id),
        approved_by = 'legacy', approved_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
        WHERE status IN ('approved', 'sending');
    -- One row per request key a client sent with a command. A replay with
    -- the same key returns result_json instead of running again. state is
    -- 'running' while the first attempt is in flight, then 'done'.
    CREATE TABLE command_requests (
        client TEXT NOT NULL,
        operator TEXT NOT NULL,
        request_key TEXT NOT NULL,
        action TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running',
        outcome TEXT DEFAULT '',
        result_json TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP,
        PRIMARY KEY (client, operator, request_key)
    );
    CREATE INDEX idx_command_requests_created ON command_requests(created_at);
    -- Every operator command: who, through which client, on what, the
    -- revisions before and after, and how it ended (ok, replayed, or the
    -- error code). Values are redacted before they get here. Append-only.
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        client TEXT NOT NULL,
        operator TEXT NOT NULL,
        request_key TEXT DEFAULT '',
        batch_id TEXT DEFAULT '',
        action TEXT NOT NULL,
        object_type TEXT DEFAULT '',
        object_id TEXT DEFAULT '',
        revision_before TEXT DEFAULT '',
        revision_after TEXT DEFAULT '',
        outcome TEXT NOT NULL,
        message TEXT DEFAULT '',
        detail_json TEXT DEFAULT '{}'
    );
    CREATE INDEX idx_audit_object ON audit_log(object_type, object_id, id);
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only');
    END;
    CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only');
    END;
    """,
    # ── v19: follow-ups thread under their opener ──
    """
    -- A follow-up sent as a reply inherits in_reply_to / thread_ref from the
    -- step sent before it. thread_references is the whole chain of
    -- Message-IDs before this email (step 1, then step 2 for step 3), space
    -- separated, for the SMTP References header. thread_subject is the
    -- subject of the opener. The wire subject is "Re: " + it, and subject
    -- keeps the text the writer drafted, for review.
    ALTER TABLE outbox ADD COLUMN thread_references TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN thread_subject TEXT DEFAULT '';
    """,
    # v20: normalize both OOO branch schemas without dropping recorded data.
    # Applied transactionally by _normalize_ooo_schema below.
    "",
    # ── v21: why each prospect got its offer ──
    """
    -- The routing decision behind a campaign's offer_key, per prospect: the
    -- offer chosen at write time and the plain reason (the rule that
    -- matched, or the fallback). The Outbox shows it next to the offer.
    CREATE TABLE IF NOT EXISTS offer_routes (
        campaign_id TEXT NOT NULL,
        prospect_id TEXT NOT NULL,
        offer_key TEXT NOT NULL DEFAULT '',
        reason TEXT DEFAULT '',
        is_default INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (campaign_id, prospect_id)
    );
    """,
    # ── v22: governed pain library ──
    """
    -- The pains a cold email may raise, governed like signal_codes: the
    -- trainer and the Writer PROPOSE, a person confirms or rejects, and only
    -- confirmed pains are ever written from. A rejected pain stays on file
    -- (it is the never-use list), so retraining cannot bring it back.
    --   code          stable id, upper snake case (PAIN_...)
    --   market        icp.markets[].name this pain belongs to, '' = any
    --   sector        trade or industry, '' = any
    --   owner_words   how the owner says it, in their own words
    --   scene         the moment it shows up, one or two lines
    --   cost          what it costs them
    --   signal_codes_json  signal codes that make it applicable (any of)
    --   offer_key     the offer that answers it, '' = any offer
    --   evidence_json URLs or notes that support it
    --   avoid_terms_json   extra phrases that mark a draft as using it
    --   origin_text   the wording first proposed, kept to recognise a
    --                 reworded duplicate (never edited)
    CREATE TABLE pains (
        code TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        sector TEXT NOT NULL DEFAULT '',
        owner_words TEXT NOT NULL DEFAULT '',
        scene TEXT NOT NULL DEFAULT '',
        cost TEXT NOT NULL DEFAULT '',
        signal_codes_json TEXT NOT NULL DEFAULT '[]',
        offer_key TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '[]',
        avoid_terms_json TEXT NOT NULL DEFAULT '[]',
        origin_text TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        status TEXT NOT NULL DEFAULT 'proposed'
            CHECK (status IN ('proposed', 'confirmed', 'rejected')),
        status_by TEXT NOT NULL DEFAULT '',
        status_at TIMESTAMP,
        status_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX idx_pains_status ON pains(status);

    -- Only a person moves a pain off 'proposed'. The status change must name
    -- who made it, and that can never be the trainer or the system.
    CREATE TRIGGER trg_pains_decision_insert
    BEFORE INSERT ON pains
    FOR EACH ROW
    WHEN NEW.status != 'proposed'
     AND (NEW.status_by = '' OR lower(NEW.status_by) IN ('trainer', 'system', 'mercury'))
    BEGIN
        SELECT RAISE(ABORT, 'a pain decision needs the person who made it');
    END;
    CREATE TRIGGER trg_pains_decision_update
    BEFORE UPDATE OF status ON pains
    FOR EACH ROW
    WHEN NEW.status != OLD.status
     AND (NEW.status_by = '' OR lower(NEW.status_by) IN ('trainer', 'system', 'mercury'))
    BEGIN
        SELECT RAISE(ABORT, 'a pain decision needs the person who made it');
    END;

    -- The pain an email was written around, for the learning loop. ''
    -- = written without one (or before pains existed).
    ALTER TABLE outbox ADD COLUMN pain_code TEXT NOT NULL DEFAULT '';
    CREATE INDEX idx_outbox_pain ON outbox(pain_code) WHERE pain_code != '';
    """,
    # ── v23: public-registry lookups ──
    """
    -- One row per company: the cached answer of the registry lookup,
    -- including "no match" and "ambiguous". A lookup is made once per
    -- company; this row is what keeps it from being made again. `people_json`
    -- holds every natural person the entity page lists, and `candidates_json`
    -- the entities that were considered, so a reviewer can see why Mercury
    -- matched or abstained.
    CREATE TABLE IF NOT EXISTS registry_lookups (
        company_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        reason TEXT DEFAULT '',
        entity_name TEXT DEFAULT '',
        document_number TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        confidence REAL DEFAULT 0,
        searched_name TEXT DEFAULT '',
        searched_city TEXT DEFAULT '',
        candidates_json TEXT DEFAULT '[]',
        people_json TEXT DEFAULT '[]',
        looked_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- A person's decision on the registry name suggested for one contact.
    -- Keyed by contact, and remembers WHICH name was decided: a refreshed
    -- lookup that finds someone else starts the review over.
    CREATE TABLE IF NOT EXISTS registry_name_reviews (
        prospect_id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        person_name TEXT NOT NULL,
        decision TEXT NOT NULL,
        decided_by TEXT DEFAULT '',
        decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Structured provenance that does not fit value/confidence/url: the
    -- registry document number, the officer's title code, the match rule.
    ALTER TABLE observations ADD COLUMN detail_json TEXT DEFAULT '';
    """,
    # ── v24: word counts and draft flags on the outbox ──
    """
    -- What a reviewer needs to see before approving a draft, and what the
    -- send path checks again: the body's word count (greeting and sign-off
    -- included, see mercury/draft_rules.py), the limit it was held to
    -- (0 = none, as for a reply), and the flags it carries as a JSON list
    -- ('' = none): over_word_limit, generic_greeting. A flagged draft is never
    -- approved by policy; a person approves it with an explicit "approve
    -- anyway", and flags_accepted_by records who. Any change to the draft
    -- clears that acceptance along with the approval.
    ALTER TABLE outbox ADD COLUMN word_count INTEGER;
    ALTER TABLE outbox ADD COLUMN word_limit INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE outbox ADD COLUMN flags TEXT NOT NULL DEFAULT '';
    ALTER TABLE outbox ADD COLUMN flags_accepted_by TEXT NOT NULL DEFAULT '';
    """,
    # ── v25: the unified inbox: stored inbound mail and local triage state ──
    """
    -- Every message read from a mailbox, kept BEFORE it is handled, so a
    -- failure part way through never loses a reply: the row stays 'retry'
    -- and the next heartbeat handles it again from here.
    -- One row per copy: (provider, mailbox, external_id) is the dedup key,
    -- so two inboxes whose providers reuse an id never collide. The same
    -- RFC Message-ID delivered to a second inbox is kept as a 'duplicate'
    -- of the first and handled once.
    --   status: received -> processed
    --           | retry (failed, tried again next cycle) -> failed (gave up)
    --           | duplicate (another inbox has the same message)
    --           | skipped (handled before this table existed)
    -- kind: message | bounce | automatic (vacation reply, receipt)
    -- outbox_id is our sent email it answers (In-Reply-To / References).
    -- received_at comes from the Date header, NULL when it is unreadable;
    -- created_at is when Mercury stored it.
    CREATE TABLE inbound_messages (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL DEFAULT '',
        mailbox TEXT NOT NULL DEFAULT '',
        external_id TEXT NOT NULL,
        rfc_message_id TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        thread_references TEXT DEFAULT '',
        thread_ref TEXT DEFAULT '',
        from_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        headers_json TEXT DEFAULT '{}',
        date_header TEXT DEFAULT '',
        received_at TIMESTAMP,
        kind TEXT NOT NULL DEFAULT 'message',
        auto_kind TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        outbox_id TEXT DEFAULT '',
        intent TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'received'
            CHECK (status IN ('received', 'processed', 'retry', 'failed',
                              'duplicate', 'skipped')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT DEFAULT '',
        duplicate_of TEXT DEFAULT '',
        source TEXT NOT NULL DEFAULT 'poll',
        created_at TIMESTAMP NOT NULL,
        processed_at TIMESTAMP,
        UNIQUE (provider, mailbox, external_id)
    );
    CREATE INDEX idx_inbound_conversation ON inbound_messages(conversation_id, created_at);
    CREATE INDEX idx_inbound_prospect ON inbound_messages(prospect_id, created_at);
    CREATE INDEX idx_inbound_rfc ON inbound_messages(rfc_message_id) WHERE rfc_message_id != '';
    CREATE INDEX idx_inbound_pending ON inbound_messages(status, created_at)
        WHERE status IN ('received', 'retry');
    -- A reply draft names the inbound message it answers.
    ALTER TABLE outbox ADD COLUMN answers_inbound_id TEXT DEFAULT '';
    CREATE INDEX idx_outbox_conversation ON outbox(conversation_id) WHERE conversation_id != '';
    CREATE INDEX idx_outbox_message_id ON outbox(message_id) WHERE message_id != '';
    -- Automatic replies recorded before this table existed are real inbound
    -- mail with honest metadata: keep them as history. Their excerpt is the
    -- sender's own words, not the full body.
    INSERT OR IGNORE INTO inbound_messages
        (id, provider, mailbox, external_id, rfc_message_id, from_email, subject, body,
         received_at, kind, auto_kind, prospect_id, status, source, created_at, processed_at)
        SELECT lower(hex(randomblob(6))), '', COALESCE(mailbox, ''), message_key,
               CASE WHEN message_key LIKE '<%>' THEN message_key ELSE '' END,
               COALESCE(from_email, ''), COALESCE(subject, ''), COALESCE(excerpt, ''),
               received_at, 'automatic', kind, COALESCE(prospect_id, ''), 'processed',
               'backfill', COALESCE(created_at, CURRENT_TIMESTAMP), created_at
        FROM auto_replies;

    -- Local triage state. None of it is synchronized with the provider: a
    -- conversation read here is still unread in Gmail, and the reverse.
    -- Unread = no read_at, or an inbound message stored after it. A snooze
    -- ends at snoozed_until, or earlier when a new message arrives.
    CREATE TABLE inbox_state (
        conversation_id TEXT PRIMARY KEY,
        read_at TIMESTAMP,
        snoozed_until TIMESTAMP,
        snoozed_at TIMESTAMP,
        updated_at TIMESTAMP
    );
    CREATE TABLE contact_notes (
        id TEXT PRIMARY KEY,
        prospect_id TEXT NOT NULL,
        body TEXT NOT NULL,
        created_by TEXT DEFAULT '',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        deleted_at TIMESTAMP
    );
    CREATE INDEX idx_contact_notes_prospect ON contact_notes(prospect_id, created_at);
    -- A reminder only flags a conversation in the inbox and on Today. It
    -- never sends or queues anything.
    CREATE TABLE inbox_reminders (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        prospect_id TEXT DEFAULT '',
        due_at TIMESTAMP NOT NULL,
        note TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TIMESTAMP NOT NULL,
        done_at TIMESTAMP,
        done_by TEXT DEFAULT ''
    );
    CREATE INDEX idx_inbox_reminders_open ON inbox_reminders(due_at) WHERE done_at IS NULL;
    CREATE INDEX idx_inbox_reminders_conversation ON inbox_reminders(conversation_id);
    """,
]


def outbox_hash(to_email, subject, body, mailbox, generation_id) -> str:
    """The content an approval covers: who it goes to, from which mailbox,
    what it says and which generation wrote it. Registered on every
    connection as the SQL function ``outbox_hash``."""
    import hashlib

    payload = json.dumps([str(v or "") for v in (to_email, subject, body, mailbox, generation_id)],
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _register_functions(db) -> None:
    await db.create_function("outbox_hash", 5, outbox_hash, deterministic=True)


# SQL fragment: the content hash of the current outbox row.
_OUTBOX_HASH_SQL = "outbox_hash(to_email, subject, body, mailbox, generation_id)"
# SQL fragment: clear an approval (a reviewable change sends the row back).
_CLEAR_APPROVAL_SQL = (
    "status = CASE WHEN status = 'approved' THEN 'pending_review' ELSE status END, "
    "approved_revision = NULL, approved_hash = '', approved_by = '', approved_at = NULL, "
    "flags_accepted_by = ''"
)

# Pause states that hold a prospect's cold sequence back.
PAUSE_ACTIVE_STATES = ("paused", "needs_review")
# A prospect who reaches one of these has left the cold sequence for good, so
# a temporary pause on them is superseded rather than waited out.
PAUSE_ENDING_STATUSES = frozenset({"replied", "opted_out", "lost", "meeting", "closed", "won"})
_PAUSE_ACTIVE_SQL = ", ".join(f"'{s}'" for s in PAUSE_ACTIVE_STATES)
# SQL fragment: an outbox row of a prospect whose cold sequence is paused.
_PAUSED_OUTBOX_SQL = (
    "(outbox.kind = 'sequence' AND EXISTS (SELECT 1 FROM sequence_pauses sp "
    f"WHERE sp.prospect_id = outbox.prospect_id AND sp.state IN ({_PAUSE_ACTIVE_SQL})))"
)


def _ts(when: datetime | None = None) -> str:
    """Naive-UTC ISO timestamp to the second: the form pauses are stored and
    compared in."""
    when = when or _utcnow()
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when.replace(microsecond=0).isoformat()

# ── Exclusion matching and company capacity (shared SQL) ──
# One definition of "this rule covers this address", used by the sender's
# claim, the dashboard and imports alike. A domain rule matches the exact
# domain; a subdomain matches only when the rule says include_subdomains.

_RULE_COVERS_ADDRESS = """
    removed_at IS NULL AND (
        (kind = 'email' AND value = :email)
        OR (kind = 'domain' AND (
            value = :domain
            OR (include_subdomains = 1 AND length(:domain) > length(value)
                AND substr(:domain, -length(value) - 1) = '.' || value))))"""

_OUTBOX_DOMAIN = "substr(to_email, instr(to_email, '@') + 1)"

# Mail an exclusion stops: everything still queued. 'sending' is already
# with the provider and cannot be recalled.
_BLOCKABLE = ("pending_review", "approved")

SOURCE_LABELS = {
    "opt_out": "opted out",
    "bounce": "bounced",
    "manual": "excluded by you",
    "import": "excluded by an import",
}


# ── Follow-up threading (channels.email.thread_followups) ──

THREAD_FIELDS = ("in_reply_to", "thread_ref", "thread_references", "thread_subject")


def reply_subject(subject: str) -> str:
    """``Re: <subject>``, never ``Re: Re: ...``."""
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def thread_headers(parent: dict | None) -> dict:
    """What a follow-up inherits from the sequence step sent before it, or {}
    when that step has no Message-ID to reply to. References carries the
    whole chain: step 1's id for step 2, then step 1's and step 2's for step 3."""
    parent = parent or {}
    message_id = (parent.get("message_id") or "").strip()
    if not message_id:
        return {}
    chain = (parent.get("thread_references") or "").split()
    if message_id not in chain:
        chain.append(message_id)
    return {
        "in_reply_to": message_id,
        "thread_ref": parent.get("thread_ref") or "",
        "thread_references": " ".join(chain),
        "thread_subject": parent.get("thread_subject") or parent.get("subject") or "",
    }


def wire_subject(item: dict, thread_followups: bool = True) -> str:
    """The subject an outbox row goes (or went) out with. A threaded
    follow-up replies under its opener's subject. ``subject`` keeps the
    writer's own text for review. A sent row records whether it was threaded,
    so the setting only decides for mail still queued."""
    threaded = (item.get("kind") == "sequence" and int(item.get("step") or 1) > 1
                and item.get("in_reply_to") and item.get("thread_subject"))
    if threaded and (thread_followups or item.get("status") == "sent"):
        return reply_subject(item["thread_subject"])
    return item.get("subject") or ""


def describe_rule(rule: dict) -> str:
    """One plain sentence: what the rule matches and why it exists."""
    if rule["kind"] == "email":
        what = rule["value"]
    elif rule.get("include_subdomains"):
        what = f"{rule['value']} and its subdomains"
    else:
        what = f"the domain {rule['value']}"
    why = SOURCE_LABELS.get(rule["source"], rule["source"])
    return f"{what} ({why})"


async def _matching_rules(db, email: str) -> list[dict]:
    email = _norm(email)
    domain = email.rsplit("@", 1)[-1]
    async with db.execute(
        f"SELECT * FROM suppressions WHERE {_RULE_COVERS_ADDRESS} "
        "ORDER BY CASE source WHEN 'opt_out' THEN 0 WHEN 'bounce' THEN 1 ELSE 2 END, "
        "created_at",
        {"email": email, "domain": domain},
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _rule_event(db, rule_id: str, action: str, actor: str, note: str = ""):
    await db.execute(
        "INSERT INTO suppression_events (suppression_id, action, actor, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (rule_id, action, actor, note, _utcnow().isoformat()))


async def _block_queued(db, rule: dict, now: str) -> int:
    """Block queued outbox rows a new rule covers. Returns how many."""
    if rule["kind"] == "email":
        match, params = "to_email = ?", [rule["value"]]
    else:
        match = f"({_OUTBOX_DOMAIN} = ?"
        params = [rule["value"]]
        if rule["include_subdomains"]:
            match += (f" OR (length({_OUTBOX_DOMAIN}) > length(?) AND "
                      f"substr({_OUTBOX_DOMAIN}, -length(?) - 1) = '.' || ?)")
            params += [rule["value"]] * 3
        match += ")"
    marks = ", ".join("?" for _ in _BLOCKABLE)
    cursor = await db.execute(
        f"UPDATE outbox SET status = 'blocked', requires_manual_review = 1, "
        f"error = ?, updated_at = ? "
        f"WHERE status IN ({marks}) AND {match}",
        ("excluded: " + describe_rule(rule), now, *_BLOCKABLE, *params))
    return cursor.rowcount


# A sequence is unfinished while any of its emails is still queued, being
# sent, or blocked waiting on a decision. It occupies a company slot once its
# first email is out (or on its way out).
_UNFINISHED = "('pending_review', 'approved', 'sending', 'blocked')"


async def _company_usage(db, company_id: str, exclude_campaign: str = "",
                         exclude_prospect: str = "") -> dict:
    async with db.execute(
        """SELECT COUNT(DISTINCT prospect_id) FROM outbox
           WHERE company_id = ? AND kind = 'sequence' AND step = 1 AND prospect_id != ?
             AND (status = 'sending' OR (status = 'sent' AND replace(sent_at, 'T', ' ') >=
                  strftime('%Y-%m-%d %H:%M:%S', 'now', '-24 hours')))""",
        (company_id, exclude_prospect),
    ) as cur:
        (new_today,) = await cur.fetchone()
    async with db.execute(
        f"""SELECT COUNT(DISTINCT prospect_id) FROM (
              SELECT campaign_id, prospect_id FROM outbox
              WHERE company_id = ? AND kind = 'sequence' AND campaign_id != ''
                AND prospect_id != ?
              GROUP BY campaign_id, prospect_id
              HAVING SUM(step = 1 AND status IN ('sent', 'sending')) > 0
                 AND SUM(status IN {_UNFINISHED}) > 0)""",
        (company_id, exclude_prospect),
    ) as cur:
        (active,) = await cur.fetchone()
    return {"new_today": new_today, "active": active}


async def _claim_verdict(db, item: dict, company_id: str, max_new: int, max_active: int,
                         respect_holds: bool) -> tuple[str, dict]:
    rules = await _matching_rules(db, item["to_email"])
    if rules:
        return "suppressed", {"rule": rules[0],
                              "error": "excluded: " + describe_rule(rules[0])}
    # An away contact's sequence waits, even when the due scan predates the
    # pause. The sender resumes due pauses before it claims anything.
    if item.get("kind") == "sequence" and item.get("prospect_id"):
        pause = await _active_pause(db, item["prospect_id"])
        if pause:
            return "ooo_pause", {"pause": pause}
    if item.get("kind") != "sequence" or not company_id:
        return "claimed", {}
    if respect_holds:
        async with db.execute(
            "SELECT * FROM company_holds WHERE company_id = ? AND released_at IS NULL",
            (company_id,),
        ) as cur:
            hold = await cur.fetchone()
        if hold:
            return "company_hold", {"hold": dict(hold)}
    if int(item.get("step") or 1) != 1 or not (max_new or max_active):
        return "claimed", {}
    usage = await _company_usage(db, company_id, item.get("campaign_id") or "",
                                 item["prospect_id"])
    if max_new and usage["new_today"] >= max_new:
        return "company_daily_limit", {**usage, "limit": max_new}
    if max_active and usage["active"] >= max_active:
        return "company_active_limit", {**usage, "limit": max_active}
    return "claimed", {}


async def _active_pause(db, prospect_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM sequence_pauses WHERE prospect_id = ? AND ended_at IS NULL",
        (prospect_id,),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def header_time(date_header: str) -> str | None:
    """An inbound Date header as a naive-UTC ISO timestamp, or None when it
    is missing or unreadable. Never "now": a stored time is a claim."""
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(date_header or "")
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return _ts(when)


# Inbound rows the handler still has to (re)try.
INBOUND_PENDING = ("received", "retry")
# How many times one inbound message is attempted before it is left as
# 'failed' for a person to look at.
INBOUND_MAX_ATTEMPTS = 5
# Queued reply drafts: an answer that has not gone out yet.
DRAFT_STATUSES = ("pending_review", "approved", "blocked", "sending")


# Queued sequence mail a pause holds and a resume reschedules. 'blocked'
# waits on an exclusion decision instead, and keeps its own schedule.
_PAUSE_HELD = ("pending_review", "approved")


# Column whitelists for dynamic UPDATEs (prevents SQL injection via kwargs).
_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel", "instantly_campaign_id",
    "sequence_json", "prospect_ids_json", "status", "offer_key",
})
_CONVERSATION_COLUMNS = frozenset({
    "prospect_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)

    @asynccontextmanager
    async def _connect(self):
        """Open a connection with sane concurrency settings.

        `timeout` maps to SQLite's busy handler, so writers wait for locks
        (e.g. while the dashboard holds a read) instead of erroring.
        """
        db = await aiosqlite.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            await _register_functions(db)
            yield db
        finally:
            await db.close()

    async def init_db(self):
        """Create/upgrade the schema. Safe to call on every startup."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            # WAL is persistent in the DB file: readers (dashboard) never
            # block the writer (agent) and vice versa.
            # Switching a fresh file to WAL needs an exclusive lock and SQLite
            # doesn't always run the busy handler for it, so parallel first
            # opens can see "database is locked". The mode is persistent:
            # retry briefly, and if another connection is mid-migration just
            # carry on — whichever connection gets the lock sets it.
            import asyncio
            import sqlite3
            for attempt in range(40):
                try:
                    await db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError:
                    await asyncio.sleep(0.05 * (attempt + 1) ** 0.5)
            await db.execute("PRAGMA synchronous=NORMAL")

            async with db.execute("PRAGMA user_version") as cursor:
                (version,) = await cursor.fetchone()
            if version >= len(MIGRATIONS):
                return

            # Several callers can race here on a fresh DB (the dashboard
            # fires parallel API requests, each calling init_db). Take the
            # write lock FIRST, re-read the version under it, and apply each
            # migration + its version bump in one transaction — otherwise a
            # second caller re-runs a half-applied ALTER TABLE and the DB is
            # stuck forever on "duplicate column".
        await self._migrate()

    async def _migrate(self):
        async with aiosqlite.connect(
            self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
        ) as db:
            await _register_functions(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("PRAGMA user_version") as cursor:
                    (version,) = await cursor.fetchone()
                if 9 <= version < len(MIGRATIONS):
                    # Before later migrations read outbox.mailbox.
                    await self._repair_pre_merge_v9(db)
                version, applied = await self._reconcile_integration_p0(db, version)
                for target, script in enumerate(MIGRATIONS, start=1):
                    if version < target:
                        if target == 20:
                            await self._normalize_ooo_schema(db)
                        elif target not in applied:
                            for statement in _split_sql(script):
                                await db.execute(statement)
                        await db.execute(f"PRAGMA user_version = {target}")
                if version < len(MIGRATIONS):
                    await self._repair_pre_merge_v9(db)
                await db.execute("COMMIT")
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def _reconcile_integration_p0(db, version: int) -> tuple[int, set[int]]:
        """Recognize the development branches by their actual schema.

        Integration used pauses/demos in slots 14/15 before main introduced
        exclusions. The threading branch used slots 16/17 for thread fields
        and pause history. Keep their data while applying every missing feature.
        """
        if version < 14:
            return version, set()
        async with db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
            tables = {row[0] for row in await cur.fetchall()}
        async with db.execute("PRAGMA table_info(outbox)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        applied = {19} if "thread_subject" in columns and "thread_references" in columns else set()
        if "sequence_pauses" in tables:
            applied.add(16)
        if "demos" in tables:
            applied.add(17)
        if "audit_log" in tables and "revision" in columns:
            applied.add(18)
        if "suppressions" not in tables and "sequence_pauses" in tables:
            return 13, applied
        if version >= 16 and ("sequence_pauses" not in tables or "demos" not in tables):
            return 15, applied
        return version, applied

    @staticmethod
    async def _normalize_ooo_schema(db) -> None:
        """Upgrade either pause implementation without dropping its history."""
        async with db.execute("PRAGMA table_info(outbox)") as cur:
            outbox_columns = {row[1] for row in await cur.fetchall()}
        for column in ("thread_references", "thread_subject"):
            if column not in outbox_columns:
                await db.execute(f"ALTER TABLE outbox ADD COLUMN {column} TEXT DEFAULT ''")
        async with db.execute("PRAGMA table_info(sequence_pauses)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        legacy = bool(columns and "status" not in columns)
        if legacy:
            await db.execute("ALTER TABLE sequence_pauses RENAME TO legacy_sequence_pauses")
        script = """
        CREATE TABLE IF NOT EXISTS sequence_pauses (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'out_of_office',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'resumed', 'superseded')),
            review_state TEXT NOT NULL CHECK (review_state IN ('scheduled', 'needs_review')),
            review_reason TEXT DEFAULT '',
            message_key TEXT DEFAULT '',
            message_at TIMESTAMP,
            confidence REAL DEFAULT 0,
            return_text TEXT DEFAULT '',
            return_date TEXT DEFAULT '',
            resume_at TIMESTAMP,
            timezone TEXT DEFAULT '',
            manual_override INTEGER NOT NULL DEFAULT 0,
            override_at TIMESTAMP,
            override_by TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            ended_by TEXT DEFAULT '',
            ended_reason TEXT DEFAULT '',
            state TEXT GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN
                CASE WHEN review_state = 'scheduled' THEN 'paused' ELSE 'needs_review' END
                ELSE status END) VIRTUAL,
            trigger_message_id TEXT GENERATED ALWAYS AS (message_key) VIRTUAL,
            trigger_at TEXT GENERATED ALWAYS AS (message_at) VIRTUAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_sequence_pauses_active
            ON sequence_pauses(prospect_id) WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_sequence_pauses_due
            ON sequence_pauses(review_state, resume_at) WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_sequence_pauses_prospect ON sequence_pauses(prospect_id, ended_at);

        -- Every automatic message from a contact (vacation reply, receipt,
        -- acknowledgement), kept as a record. They never open a conversation and
        -- never count as replies. The primary key is what makes a re-polled
        -- message a no-op instead of a second pause update.
        CREATE TABLE IF NOT EXISTS auto_replies (
            message_key TEXT PRIMARY KEY,
            prospect_id TEXT DEFAULT '',
            from_email TEXT DEFAULT '',
            mailbox TEXT DEFAULT '',
            kind TEXT NOT NULL,
            detected_by TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            excerpt TEXT DEFAULT '',
            received_at TIMESTAMP,
            return_text TEXT DEFAULT '',
            parsed_resume_at TIMESTAMP,
            pause_id TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_auto_replies_prospect ON auto_replies(prospect_id, received_at);
        """
        for statement in _split_sql(script):
            await db.execute(statement)
        if legacy:
            await db.execute("""INSERT INTO sequence_pauses
                (id, prospect_id, reason, status, review_state, review_reason,
                 message_key, message_at, confidence, return_text, resume_at,
                 manual_override, override_at, ended_at, ended_reason, created_at, updated_at)
                SELECT id, prospect_id, reason,
                  CASE WHEN state IN ('paused', 'needs_review') THEN 'active' ELSE state END,
                  CASE WHEN state = 'paused' OR resume_at IS NOT NULL THEN 'scheduled'
                       ELSE 'needs_review' END,
                  review_reason, trigger_message_id, trigger_at, confidence, return_text,
                  resume_at, manual_override, override_at, ended_at, ended_reason,
                  created_at, updated_at FROM legacy_sequence_pauses""")
            await db.execute("DROP TABLE legacy_sequence_pauses")
        else:
            # The history branch already has the canonical columns. Only the
            # compatibility read aliases are missing from its older databases.
            async with db.execute("PRAGMA table_xinfo(sequence_pauses)") as cur:
                all_columns = {row[1] for row in await cur.fetchall()}
            if "return_date" not in all_columns:
                await db.execute("ALTER TABLE sequence_pauses ADD COLUMN return_date TEXT DEFAULT ''")
            for name, expression in {
                "state": "CASE WHEN status = 'active' THEN CASE WHEN review_state = 'scheduled' "
                         "THEN 'paused' ELSE 'needs_review' END ELSE status END",
                "trigger_message_id": "message_key", "trigger_at": "message_at",
            }.items():
                if name not in all_columns:
                    await db.execute(f"ALTER TABLE sequence_pauses ADD COLUMN {name} TEXT "
                                     f"GENERATED ALWAYS AS ({expression}) VIRTUAL")

    @staticmethod
    async def _repair_pre_merge_v9(db) -> None:
        """Add ``outbox.mailbox`` to a dev DB stamped 9 by the pre-merge branch.

        Before mailbox rotation was merged, this branch used v9 for the
        warm-up table, so such a DB skips the real v9 (``outbox.mailbox``).
        No production DB is affected; this only keeps old local copies usable.
        """
        async with db.execute("PRAGMA table_info(outbox)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if columns and "mailbox" not in columns:
            await db.execute("ALTER TABLE outbox ADD COLUMN mailbox TEXT DEFAULT ''")

    # ── Companies ──

    @staticmethod
    def _company_from_row(row: aiosqlite.Row) -> Company:
        d = dict(row)
        for json_col, field in (("tech_stack_json", "tech_stack"), ("signals_json", "signals")):
            raw = d.pop(json_col, None)
            try:
                parsed = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                parsed = []
            d[field] = parsed if isinstance(parsed, list) else []
        return Company(**d)

    async def add_company(self, company: Company) -> str:
        """Insert a company, or return the id of the one already recorded.

        Identity is the normalised domain when there is one, and the
        provider's ``external_id`` when there isn't — a business with no
        website still has to dedup across re-runs, and for anyone selling
        websites those are the best prospects on the list.
        """
        async with self._connect() as db:
            company_id = await self.insert_company(db, company)
            await db.commit()
        return company_id

    @staticmethod
    async def insert_company(db, company: Company) -> str:
        """``add_company`` on an open connection, without committing, so a
        caller can insert many inside one transaction."""
        if not company.id:
            company.id = _new_id()
        company.domain = _norm(company.domain)
        cursor = await db.execute(
            """INSERT OR IGNORE INTO companies
               (id, name, domain, website, description, industry,
                company_size, location, phone, source, source_url,
                external_id, notes, tech_stack_json, signals_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company.id, company.name, company.domain, company.website,
                company.description, company.industry, company.company_size,
                company.location, company.phone, company.source,
                company.source_url, company.external_id, company.notes,
                json.dumps(company.tech_stack), json.dumps(company.signals),
                company.created_at.isoformat(),
                company.updated_at.isoformat(),
            ),
        )
        if cursor.rowcount == 0:
            # Uniqueness conflict: hand back the existing record's id.
            for column, value in (("domain", company.domain),
                                  ("external_id", company.external_id)):
                if not value:
                    continue
                async with db.execute(
                    f"SELECT id FROM companies WHERE {column} = ?", (value,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        company.id = row[0]
                        break
        return company.id

    async def update_company_signals(
        self,
        company_id: str,
        tech_stack: list[str] | None = None,
        new_signals: list[dict] | None = None,
    ):
        """Merge freshly-detected tech + signals into a company record.

        Signals are appended with dedup on (type, detail) so re-scans
        don't multiply the same finding.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT tech_stack_json, signals_json FROM companies WHERE id = ?",
                (company_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return

            def _load(raw):
                try:
                    parsed = json.loads(raw) if raw else []
                except (json.JSONDecodeError, TypeError):
                    parsed = []
                return parsed if isinstance(parsed, list) else []

            tech = _load(row["tech_stack_json"])
            signals = _load(row["signals_json"])

            for t in tech_stack or []:
                if t not in tech:
                    tech.append(t)
            seen = {(s.get("type"), s.get("detail")) for s in signals if isinstance(s, dict)}
            for s in new_signals or []:
                if not isinstance(s, dict):
                    continue
                if (s.get("type"), s.get("detail")) in seen:
                    continue
                signals.append(s)
                seen.add((s.get("type"), s.get("detail")))

            await db.execute(
                "UPDATE companies SET tech_stack_json = ?, signals_json = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(tech), json.dumps(signals),
                 _utcnow().isoformat(), company_id),
            )
            await db.commit()

    async def get_company(self, company_id: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_domain(self, domain: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_external_id(self, external_id: str) -> Company | None:
        """Look a company up by its provider-stable id.

        The identity path for businesses with no website — which is exactly
        the cohort worth the most to anyone selling one.
        """
        if not external_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE external_id = ?", (external_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def find_companies(self, query: str, limit: int = 10) -> list[Company]:
        """Companies matching an id, a domain, or part of a name."""
        q = (query or "").strip()
        if not q:
            return []
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM companies
                   WHERE id = ? OR domain = ? OR name LIKE ? ESCAPE '\\'
                   ORDER BY (id = ?) DESC, (domain = ?) DESC, created_at DESC LIMIT ?""",
                (q, _norm(q), "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                 q, _norm(q), int(limit)),
            ) as cursor:
                return [self._company_from_row(r) for r in await cursor.fetchall()]

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                (company_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def company_exists(self, domain: str) -> bool:
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                return bool(await cursor.fetchone())

    # ── Prospects (Contacts) ──

    @staticmethod
    def _prospect_from_row(row: aiosqlite.Row) -> Prospect:
        d = dict(row)
        d["email_verified"] = bool(d.get("email_verified", 0))
        d["phone_verified"] = bool(d.get("phone_verified", 0))
        d["email_status"] = d.get("email_status") or ""
        return Prospect(**d)

    async def add_prospect(self, prospect: Prospect) -> str:
        """Insert a prospect. Duplicates (same email or LinkedIn URL) are not
        re-inserted; the existing record's id is returned instead."""
        async with self._connect() as db:
            prospect_id = await self.insert_prospect(db, prospect)
            await db.commit()
        return prospect_id

    @staticmethod
    async def insert_prospect(db, prospect: Prospect) -> str:
        """``add_prospect`` on an open connection, without committing, so a
        caller can insert many inside one transaction."""
        if not prospect.id:
            prospect.id = _new_id()
        prospect.email = _norm(prospect.email)
        prospect.linkedin_url = (prospect.linkedin_url or "").strip()
        cursor = await db.execute(
            """INSERT OR IGNORE INTO prospects
               (id, company_id, first_name, last_name, email, email_verified,
                email_status, phone, phone_verified, linkedin_url, title,
                seniority, department, source, source_url, status, score,
                personalization_notes, company, industry, company_size,
                import_batch_id, import_row, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prospect.id, prospect.company_id,
                prospect.first_name, prospect.last_name,
                prospect.email, int(prospect.email_verified),
                prospect.email_status,
                prospect.phone, int(prospect.phone_verified),
                prospect.linkedin_url, prospect.title,
                prospect.seniority, prospect.department,
                prospect.source, prospect.source_url,
                prospect.status, prospect.score,
                prospect.personalization_notes,
                prospect.company, prospect.industry, prospect.company_size,
                prospect.import_batch_id, prospect.import_row,
                prospect.created_at.isoformat(),
                prospect.updated_at.isoformat(),
            ),
        )
        if cursor.rowcount == 0:
            # Unique-constraint conflict: resolve to the existing record.
            for column, value in (
                ("email", prospect.email),
                ("linkedin_url", prospect.linkedin_url),
                ("id", prospect.id),
            ):
                if not value:
                    continue
                async with db.execute(
                    f"SELECT id FROM prospects WHERE {column} = ?", (value,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        prospect.id = row[0]
                        break
        return prospect.id

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()
        if status in PAUSE_ENDING_STATUSES:
            await self.supersede_pause(prospect_id, f"prospect {status}")

    async def get_prospect_by_email(self, email: str) -> Prospect | None:
        """Look up a prospect by email address (indexed, case-insensitive)."""
        email = _norm(email)
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def update_prospect_email(
        self, prospect_id: str, email: str, email_status: str
    ):
        """Set a prospect's email + honesty status (verified/risky/guess/invalid)."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE prospects
                   SET email = ?, email_status = ?, email_verified = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    _norm(email), email_status,
                    1 if email_status == "verified" else 0,
                    _utcnow().isoformat(), prospect_id,
                ),
            )
            await db.commit()

    # ── Email pattern cache (per-domain) ──

    async def get_email_pattern(self, domain: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM email_patterns WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def save_email_pattern(
        self,
        domain: str,
        pattern: str = "",
        source: str = "",
        confidence: float = 0.0,
        mx_type: str = "",
        is_catch_all: int | None = None,
    ):
        """Upsert what we've learned about a domain's email conventions.

        Only overwrites the stored pattern when the new one has equal or
        higher confidence; mx_type/is_catch_all always refresh.
        """
        domain = _norm(domain)
        if not domain:
            return
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO email_patterns
                       (domain, pattern, source, confidence, mx_type, is_catch_all, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(domain) DO UPDATE SET
                       pattern = CASE WHEN excluded.confidence >= email_patterns.confidence
                                       AND excluded.pattern != ''
                                      THEN excluded.pattern ELSE email_patterns.pattern END,
                       source = CASE WHEN excluded.confidence >= email_patterns.confidence
                                      AND excluded.pattern != ''
                                     THEN excluded.source ELSE email_patterns.source END,
                       confidence = MAX(email_patterns.confidence, excluded.confidence),
                       mx_type = CASE WHEN excluded.mx_type != ''
                                      THEN excluded.mx_type ELSE email_patterns.mx_type END,
                       is_catch_all = CASE WHEN excluded.is_catch_all != -1
                                           THEN excluded.is_catch_all
                                           ELSE email_patterns.is_catch_all END,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    domain, pattern, source, float(confidence), mx_type,
                    -1 if is_catch_all is None else int(is_catch_all),
                ),
            )
            await db.commit()

    # ── Outbox (approval ladder + native sending) ──

    async def add_outbox_item(
        self,
        *,
        prospect_id: str,
        to_email: str,
        subject: str,
        body: str,
        send_at: str,
        status: str = "pending_review",
        campaign_id: str = "",
        conversation_id: str = "",
        step: int = 1,
        kind: str = "sequence",
        provider: str = "",
        thread_ref: str = "",
        in_reply_to: str = "",
        mailbox: str = "",
        generation_id: str = "",
        offer_key: str = "",
        approved_by: str = "policy",
        company_id: str = "",
        pain_code: str = "",
        word_limit: int = 0,
        flags: list[str] | None = None,
        thread_references: str = "",
        answers_inbound_id: str = "",
    ) -> str | None:
        """Queue one outgoing email. Returns its id, or None when the
        (campaign, prospect, step) slot already exists — the double-send guard.
        Without an ``offer_key`` the row takes its campaign's, so every path
        that queues a sequence email carries the offer the demo gate reads.

        The row records its word count and the ``word_limit`` it is held to
        (0 = none). ``flags`` are the draft's flags; left out, they are worked
        out from the limit. A flagged draft is never queued as approved: the
        policy that skips review cannot also accept a draft that broke a rule,
        so it waits in review for a person's explicit choice."""
        item_id = _new_id()
        word_limit = max(0, int(word_limit or 0))
        if flags is None:
            flags = draft_flags(body, word_limit)
        if flags and status == "approved":
            status = "pending_review"
        # Queued already approved (approval not required): the snapshot is
        # this first revision, approved by the policy that skipped review.
        approved = status == "approved"
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO outbox
                   (id, campaign_id, prospect_id, conversation_id, step, kind,
                    to_email, subject, body, status, send_at, provider,
                    thread_ref, in_reply_to, mailbox, generation_id, company_id, offer_key,
                    approved_revision, approved_hash, approved_by, approved_at, pain_code,
                    word_count, word_limit, flags, thread_references, answers_inbound_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(NULLIF(?, ''),
                                    (SELECT offer_key FROM campaigns WHERE id = ?), ''),
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, campaign_id, prospect_id, conversation_id,
                    int(step), kind, _norm(to_email), subject, body,
                    status, send_at, provider, thread_ref, in_reply_to,
                    _norm(mailbox), generation_id, company_id, _norm(offer_key), campaign_id,
                    1 if approved else None,
                    outbox_hash(_norm(to_email), subject, body, _norm(mailbox), generation_id)
                    if approved else "",
                    approved_by if approved else "",
                    _utcnow().isoformat() if approved else None,
                    (pain_code or "").strip().upper(),
                    count_words(body), word_limit, encode_flags(flags),
                    thread_references, answers_inbound_id,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted and generation_id:
                await db.execute(
                    "INSERT INTO email_generation_history "
                    "(outbox_id, generation_id, original_subject, original_body) "
                    "VALUES (?, ?, ?, ?)",
                    (item_id, generation_id, subject, body),
                )
            await db.commit()
            return item_id if inserted else None

    async def get_outbox(
        self,
        status: str | None = None,
        due_before: str | None = None,
        limit: int = 200,
        exclude_paused: bool = False,
    ) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if due_before:
            where.append("(send_at IS NULL OR send_at <= ?)")
            params.append(due_before)
        if exclude_paused:
            where.append(f"NOT {_PAUSED_OUTBOX_SQL}")
        sql = "SELECT * FROM outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY send_at ASC, created_at ASC LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_outbox_item(self, item_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE id = ?", (item_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    _OUTBOX_COLUMNS = frozenset({
        "status", "error", "message_id", "thread_ref", "sent_at",
        "subject", "body", "send_at", "provider", "mailbox", "manually_edited",
        "company_id", "in_reply_to", "thread_references", "thread_subject",
    })

    @staticmethod
    async def _with_draft_checks(db, item_id: str, fields: dict, word_limit: int | None = None) -> dict:
        """``fields`` plus a fresh word count and flags when the body changes.
        The limit is the one given, else the one the row was held to."""
        if "body" not in fields:
            return fields
        async with db.execute("SELECT word_limit, flags FROM outbox WHERE id = ?", (item_id,)) as c:
            row = await c.fetchone()
        if row is None:
            return fields
        limit = int(row[0] or 0) if word_limit is None else max(0, int(word_limit))
        return {**fields, "word_count": count_words(fields["body"]), "word_limit": limit,
                "flags": encode_flags(recheck_flags(fields["body"], limit, decode_flags(row[1])))}

    async def update_outbox_item(self, item_id: str, **kwargs):
        """Bookkeeping by the sender and scripts. A change to the text or
        the mailbox without a status of its own is a new revision and drops
        any approval; send_at and status changes here are the sender's own
        timing and transitions, and keep it."""
        fields = {k: v for k, v in kwargs.items() if k in self._OUTBOX_COLUMNS}
        if not fields:
            return
        async with self._connect() as db:
            fields = await self._with_draft_checks(db, item_id, fields)
            sets = ", ".join(f"{k} = ?" for k in fields)
            if "status" not in fields and fields.keys() & {"subject", "body", "mailbox"}:
                sets += f", revision = revision + 1, {_CLEAR_APPROVAL_SQL}"
            await db.execute(
                f"UPDATE outbox SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _utcnow().isoformat(), item_id),
            )
            await db.commit()

    _REVISABLE_COLUMNS = frozenset({"to_email", "subject", "body", "send_at", "mailbox",
                                    "manually_edited"})

    async def revise_outbox_item(self, item_id: str, expected_revision: int | None = None,
                                 word_limit: int | None = None, **kwargs) -> int | None:
        """An operator's change to a queued draft: text, recipient, sending
        mailbox or send time. It is a new revision, and an approved draft
        goes back to review. Applies only while the draft is pending or
        approved and, when ``expected_revision`` is given, still at that
        revision. Returns the new revision, or None when nothing changed.

        A new body is measured again: its word count and flags are replaced
        (against ``word_limit`` when given, else the limit the row was held
        to), and any acceptance of the old flags is dropped with the approval."""
        fields = {k: v for k, v in kwargs.items() if k in self._REVISABLE_COLUMNS}
        if not fields:
            return None
        for column in ("to_email", "mailbox"):
            if column in fields:
                fields[column] = _norm(fields[column])
        async with self._connect() as db:
            fields = await self._with_draft_checks(db, item_id, fields, word_limit)
            sets = ", ".join(f"{k} = ?" for k in fields)
            cursor = await db.execute(
                f"UPDATE outbox SET {sets}, revision = revision + 1, {_CLEAR_APPROVAL_SQL}, "
                "updated_at = ? WHERE id = ? AND status IN ('pending_review', 'approved') "
                "AND (? IS NULL OR revision = ?)",
                (*fields.values(), _utcnow().isoformat(), item_id,
                 expected_revision, expected_revision),
            )
            if not cursor.rowcount:
                return None
            async with db.execute("SELECT revision FROM outbox WHERE id = ?", (item_id,)) as c:
                (revision,) = await c.fetchone()
            await db.commit()
            return int(revision)

    async def edit_outbox_item(self, item_id: str, **kwargs) -> bool:
        """Apply reviewer changes only while the draft is still editable.
        Any change is a new revision and sends an approved draft back to review."""
        fields = {k: v for k, v in kwargs.items()
                  if k in {"subject", "body", "send_at", "manually_edited"}}
        return await self.revise_outbox_item(item_id, None, **fields) is not None

    async def claim_outbox_item(self, item: dict, mailbox: str) -> bool:
        """Freeze the validated snapshot before sending; reject stale or claimed rows.

        The row must still hold exactly what the due scan read, and its
        approval must be for this revision and this content: an approval
        for an earlier revision, or content changed behind it, never sends.
        The mailbox the sender resolved for it (a rotation pick, or the
        legacy inbox) is the sender's routing, not a review change, so the
        snapshot takes it over and a retry or recovered send still claims."""
        return (await self.claim_for_send(item, mailbox))[0] == "claimed"

    async def recover_stale_outbox(self, max_age_minutes: int = 30) -> int:
        """Put rows stuck in 'sending' back to 'approved'.

        A row is claimed ('sending') right before the provider call. If the
        process dies mid-send, nothing ever moves it on, and the email is
        neither sent nor retried. Anything still 'sending' after
        ``max_age_minutes`` is treated as interrupted and re-queued with its
        original send_at, so the next drain picks it up. Returns the count.
        """
        cutoff = (_utcnow() - timedelta(minutes=max(0, int(max_age_minutes)))).isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'approved', "
                "error = 'recovered: send interrupted', updated_at = ? "
                "WHERE status = 'sending' AND "
                "(updated_at IS NULL OR REPLACE(updated_at, ' ', 'T') <= ?)",
                (_utcnow().isoformat(), cutoff),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def approve_outbox(self, item_id: str, expected_revision: int | None = None,
                             approved_by: str = "", accept_flags: bool = False) -> int:
        """Approve one pending item and record the snapshot it approved: its
        current revision and content hash. With ``expected_revision`` the
        item must still be at that revision.

        A flagged draft (over its word limit, generic greeting) is approved
        only with ``accept_flags``: the reviewer's explicit choice, recorded
        in ``flags_accepted_by`` for the revision approved. Without it a
        flagged draft stays in review and this returns 0."""
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'approved', requires_manual_review = 0, "
                "approved_revision = revision, "
                f"approved_hash = {_OUTBOX_HASH_SQL}, approved_by = ?, approved_at = ?, "
                "flags_accepted_by = CASE WHEN flags != '' THEN ? ELSE '' END, "
                "updated_at = ? WHERE id = ? AND status = 'pending_review' "
                "AND (? IS NULL OR revision = ?) AND (flags = '' OR ? = 1)",
                (approved_by, now, approved_by or "reviewer", now, item_id,
                 expected_revision, expected_revision, 1 if accept_flags else 0),
            )
            await db.commit()
            return cursor.rowcount

    async def approve_ready_followups(self, campaign_id: str | None = None,
                                      prospect_id: str | None = None) -> int:
        """Approve pending follow-ups (sequence steps 2+) whose previous step
        is already approved or sent: the reviewer signed off on the opener,
        so the sequence it belongs to may run. A follow-up of a still-pending
        or rejected opener stays put. One step per pass, so a 3-step chain
        whose opener was approved is fully promoted within two cycles.
        Requeued exclusions always need another explicit approval."""
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                f"""UPDATE outbox SET status = 'approved', approved_revision = revision,
                       approved_hash = {_OUTBOX_HASH_SQL}, approved_by = 'auto_followups',
                       approved_at = ?, updated_at = ?
                   WHERE status = 'pending_review' AND kind = 'sequence'
                     AND requires_manual_review = 0 AND flags = ''
                     AND step > 1 AND campaign_id != ''
                     AND (
                       SELECT prev.status FROM outbox AS prev
                       WHERE prev.campaign_id = outbox.campaign_id
                         AND prev.prospect_id = outbox.prospect_id
                         AND prev.kind = 'sequence' AND prev.step < outbox.step
                       ORDER BY prev.step DESC LIMIT 1
                     ) IN ('approved', 'sent')"""
                + (" AND campaign_id = ? AND prospect_id = ?" if campaign_id else ""),
                (now, now) + ((campaign_id, prospect_id or "") if campaign_id else ()),
            )
            await db.commit()
            return cursor.rowcount

    async def get_sequence_delay_days(self, campaign_id: str, step: int) -> int | None:
        """delay_days of ``step`` in a campaign's sequence (days after the
        previous step), or None when the campaign or step is unknown."""
        if not campaign_id:
            return None
        async with self._connect() as db:
            async with db.execute(
                "SELECT sequence_json FROM campaigns WHERE id = ?", (campaign_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        for s in Campaign.sequence_from_json(row[0]):
            if int(s.step) == int(step):
                return max(0, int(s.delay_days))
        return None

    async def get_thread_mailboxes(self, campaign_ids: list[str]) -> dict[tuple[str, str], str]:
        """(campaign_id, prospect_id) -> mailbox of the thread's sent opener
        ('' = sent before mailbox tracking). One query for a whole page."""
        ids = sorted({c for c in campaign_ids if c})
        if not ids:
            return {}
        out: dict[tuple[str, str], str] = {}
        async with self._connect() as db:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ", ".join("?" for _ in chunk)
                async with db.execute(
                    "SELECT campaign_id, prospect_id, COALESCE(mailbox, '') FROM outbox "
                    "WHERE kind = 'sequence' AND step = 1 AND status = 'sent' "
                    f"AND campaign_id IN ({marks})",
                    chunk,
                ) as cursor:
                    for c, p, m in await cursor.fetchall():
                        out[(c, p)] = m
        return out

    async def get_previous_outbox_step(
        self, campaign_id: str, prospect_id: str, step: int
    ) -> dict | None:
        """Nearest earlier sequence step for the same (campaign, prospect),
        or None when there is none. The sender holds step N until this row
        is actually 'sent': a follow-up must never precede its opener."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE campaign_id = ? AND prospect_id = ? "
                "AND kind = 'sequence' AND step < ? "
                "ORDER BY step DESC LIMIT 1",
                (campaign_id, prospect_id, int(step)),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def thread_followups(self, campaign_id: str, prospect_id: str) -> int:
        """Copy the thread headers of the latest sent step of a sequence onto
        its later steps that are still queued, so they go out (and show in
        review) as replies in that thread. Only subject/body are ever edited
        or regenerated, so the headers survive both. A sequence whose opener
        never went out has no sent step and keeps empty headers.
        Returns the number of rows updated."""
        if not campaign_id:
            return 0
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE campaign_id = ? AND prospect_id = ? "
                "AND kind = 'sequence' AND status = 'sent' AND message_id != '' "
                "ORDER BY step DESC LIMIT 1",
                (campaign_id, prospect_id),
            ) as cursor:
                parent = await cursor.fetchone()
            headers = thread_headers(dict(parent) if parent else None)
            if not headers:
                return 0
            cursor = await db.execute(
                "UPDATE outbox SET in_reply_to = ?, thread_ref = ?, thread_references = ?, "
                "thread_subject = ? WHERE campaign_id = ? AND prospect_id = ? "
                "AND kind = 'sequence' AND step > ? "
                "AND status IN ('pending_review', 'approved', 'blocked')",
                (*(headers[f] for f in THREAD_FIELDS), campaign_id, prospect_id,
                 int(parent["step"])),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def reject_outbox_item(self, item_id: str, expected_revision: int | None = None) -> int:
        """Reject one queued item. For a sequence step, every LATER step of
        the same (campaign, prospect) that is still queued is rejected too:
        'closing the loop' on an email that never went out is nonsense.
        With ``expected_revision`` the item must still be at that revision,
        or nothing is rejected. Returns the number of rows rejected."""
        item = await self.get_outbox_item(item_id)
        if not item:
            return 0
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'rejected', updated_at = ? "
                "WHERE id = ? AND status IN ('pending_review', 'approved', 'blocked') "
                "AND (? IS NULL OR revision = ?)",
                (now, item_id, expected_revision, expected_revision),
            )
            n = cursor.rowcount
            if not n and expected_revision is not None:
                return 0  # changed since it was read: reject nothing
            if item["kind"] == "sequence" and item["campaign_id"]:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'rejected', error = ?, updated_at = ? "
                    "WHERE campaign_id = ? AND prospect_id = ? AND kind = 'sequence' "
                    "AND step > ? AND status IN ('pending_review', 'approved', 'blocked')",
                    (f"step {item['step']} rejected", now,
                     item["campaign_id"], item["prospect_id"], int(item["step"])),
                )
                n += cursor.rowcount
            await db.commit()
            return n

    async def cancel_pending_outbox_for_prospect(
        self, prospect_id: str, reason: str = "stop_on_reply", keep_answering: str = ""
    ) -> int:
        """Stop-on-reply: cancel everything queued for a prospect who replied.
        ``keep_answering`` spares the draft that answers that inbound message
        (a retried message must not cancel the answer its first try queued)."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE prospect_id = ? AND status IN ('pending_review', 'approved', 'blocked') "
                "AND (? = '' OR COALESCE(answers_inbound_id, '') != ?)",
                (reason, _utcnow().isoformat(), prospect_id, keep_answering, keep_answering),
            )
            await db.commit()
            return cursor.rowcount

    # Compatibility methods for integrations that address pauses by prospect.

    async def count_active_pauses(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute("SELECT state, COUNT(*) FROM sequence_pauses "
                                  "WHERE ended_at IS NULL GROUP BY state") as cur:
                return {row[0]: row[1] for row in await cur.fetchall()}

    async def record_ooo_pause(
        self, prospect_id: str, *, message_id: str, message_at: datetime,
        state: str, resume_at: datetime | None = None, confidence: float = 0.0,
        return_text: str = "", review_reason: str = "", reason: str = "ooo",
    ) -> dict:
        if state not in PAUSE_ACTIVE_STATES:
            raise ValueError(f"not an active pause state: {state!r}")
        if state == "paused" and resume_at is None:
            raise ValueError("a paused sequence needs a resume time")
        outcome, pause = await self.record_auto_reply(
            message_key=f"{prospect_id}:{message_id or _ts(message_at)}",
            kind="out_of_office", received_at=_ts(message_at), prospect_id=prospect_id,
            parsed={"source_message_key": message_id,
                    "resume_at": _ts(resume_at) if state == "paused" else None,
                    "confidence": confidence, "text": return_text,
                    "review_reason": review_reason},
        )
        if pause is None:
            pause = await self.get_pause(prospect_id)
        action = {"paused": "created", "updated": "updated"}.get(outcome, "ignored")
        why = "" if action != "ignored" else {
            "duplicate": "duplicate message", "ignored": "older than the end of the last pause",
            "kept": "keeps the return date already set or predates its latest correction",
        }.get(outcome, outcome)
        return {"action": action, "why": why, "pause": pause}

    async def override_pause(self, prospect_id: str, resume_at: datetime,
                             now: datetime | None = None) -> dict | None:
        now_s, when = _ts(now), _ts(resume_at)
        if when <= now_s:
            return await self.resume_pause(prospect_id, reason="operator", now=now)
        pause = await self.get_active_pause(prospect_id)
        if pause is None:
            return None
        changed = await self.set_pause_resume_at(pause["id"], when, now=now_s, return_date="")
        return changed[1] if changed else None

    async def resume_due_pauses(self, now: datetime | None = None) -> list[dict]:
        resumed = []
        for pause in await self.due_pauses(_ts(now)):
            result = await self.resume_pause(pause["prospect_id"], reason="return date",
                                             now=now, only_due=True)
            if result is not None:
                resumed.append(result)
        return resumed

    async def supersede_pause(self, prospect_id: str, reason: str,
                              now: datetime | None = None) -> bool:
        return bool(await self.end_pause(prospect_id, reason=reason, now=_ts(now)))

    @staticmethod
    async def _reschedule_after_pause(db, prospect_id: str, resume_at: str, now_s: str) -> int:
        """Move a resumed prospect's queued sequence steps to start at
        ``resume_at``. The next step goes out then (never earlier than it was
        planned), and each later step keeps its own gap after the one before,
        so overdue steps do not all fire at once. Statuses are not touched."""
        async with db.execute(
            "SELECT id, campaign_id, step, send_at FROM outbox "
            "WHERE prospect_id = ? AND kind = 'sequence' "
            "AND status IN ('pending_review', 'approved') "
            "ORDER BY campaign_id, step",
            (prospect_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        by_campaign: dict[str, list] = {}
        for row in rows:
            by_campaign.setdefault(row[1], []).append(row)

        def parse(value):
            try:
                return datetime.fromisoformat(str(value).replace(" ", "T"))
            except (TypeError, ValueError):
                return None

        anchor = parse(resume_at) or parse(now_s)
        moved = 0
        for campaign_id, steps in by_campaign.items():
            delays: dict[int, int] = {}
            if campaign_id:
                async with db.execute(
                    "SELECT sequence_json FROM campaigns WHERE id = ?", (campaign_id,)
                ) as cursor:
                    found = await cursor.fetchone()
                if found:
                    for s in Campaign.sequence_from_json(found[0]):
                        delays[int(s.step)] = max(0, int(s.delay_days))
            prev_new = prev_old = None
            for row_id, _cid, step, send_at in steps:
                old = parse(send_at) or anchor
                if prev_new is None:
                    new = max(old, anchor)
                else:
                    if int(step) in delays:
                        gap = timedelta(days=delays[int(step)])
                    else:
                        gap = max(timedelta(0), old - prev_old) if prev_old else timedelta(0)
                    new = max(old, prev_new + gap)
                if new != old or send_at is None:
                    await db.execute(
                        "UPDATE outbox SET send_at = ?, updated_at = ? WHERE id = ? "
                        "AND status IN ('pending_review', 'approved')",
                        (new.replace(microsecond=0).isoformat(), now_s, row_id),
                    )
                    moved += 1
                prev_new, prev_old = new, old
        return moved

    async def count_outbox_sent(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent'"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent_today(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                # A rolling 24-hour window: a calendar day in UTC let the cap
                # reset at 20:00 Santo Domingo, i.e. ten sends per local day.
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent' "
                "AND replace(sent_at, 'T', ' ') >= "
                "strftime('%Y-%m-%d %H:%M:%S', 'now', '-24 hours')"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent_today_by_mailbox(self) -> dict[str, int]:
        """Same rolling 24-hour window as count_outbox_sent_today, split by
        the mailbox each email went out from ('' = before tracking)."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COALESCE(mailbox, ''), COUNT(*) FROM outbox "
                "WHERE status = 'sent' AND replace(sent_at, 'T', ' ') >= "
                "strftime('%Y-%m-%d %H:%M:%S', 'now', '-24 hours') "
                "GROUP BY COALESCE(mailbox, '')"
            ) as cursor:
                return {row[0]: row[1] for row in await cursor.fetchall()}

    async def find_outbox_by_message_id(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    # ── Demos: what an offer's email says was already built for them ──

    DEMO_ARTIFACTS = ("demo_url", "recording_path", "agent_id", "notes", "built_by")
    # The offer a sequence row belongs to: its own key, else its campaign's.
    _OUTBOX_OFFER_SQL = "COALESCE(NULLIF(o.offer_key, ''), c.offer_key, '')"

    async def request_demo(self, prospect_id: str, offer_key: str,
                           kind: str = "") -> tuple[str, bool]:
        """Register a demo as 'requested'. Returns (demo id, created); a live
        demo for the same prospect and offer is returned as it is."""
        offer_key = _norm(offer_key)
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO demos (id, prospect_id, offer_key, kind) "
                "VALUES (?, ?, ?, ?)",
                (_new_id(), prospect_id, offer_key, _norm(kind)),
            )
            await db.commit()
            created = cursor.rowcount > 0
        demo = await self.find_live_demo(prospect_id, offer_key)
        return (demo["id"] if demo else ""), created

    async def get_demo(self, demo_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM demos WHERE id = ?", (demo_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_live_demo(self, prospect_id: str, offer_key: str) -> dict | None:
        """The requested or ready demo for this prospect and offer, if any."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM demos WHERE prospect_id = ? AND offer_key = ? "
                "AND status != 'retired'",
                (prospect_id, _norm(offer_key)),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_demos(self, status: str | None = None, prospect_id: str = "",
                         limit: int = 200) -> list[dict]:
        """Demo records, newest first, with the prospect's name and address."""
        where, params = [], []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if prospect_id:
            where.append("d.prospect_id = ?")
            params.append(prospect_id)
        sql = ("SELECT d.*, COALESCE(p.email, '') AS email, COALESCE(p.first_name, '') AS first_name, "
               "COALESCE(p.last_name, '') AS last_name, COALESCE(p.company, '') AS company "
               "FROM demos d LEFT JOIN prospects p ON p.id = d.prospect_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.updated_at DESC, d.created_at DESC LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def mark_demo_ready(self, demo_id: str, **artifacts) -> bool:
        """requested -> ready, or update a ready demo's artifacts. Only the
        artifact fields given (not None) change. A retired demo stays retired."""
        fields = {k: str(v).strip() for k, v in artifacts.items()
                  if k in self.DEMO_ARTIFACTS and v is not None}
        sets = "".join(f", {k} = ?" for k in fields)
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE demos SET status = 'ready', ready_at = COALESCE(ready_at, ?), "
                f"updated_at = ?{sets} WHERE id = ? AND status IN ('requested', 'ready')",
                (now, now, *fields.values(), demo_id),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def retire_demo(self, demo_id: str, reason: str = "") -> bool:
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE demos SET status = 'retired', retired_at = ?, retire_reason = ?, "
                "updated_at = ? WHERE id = ? AND status != 'retired'",
                (now, reason, now, demo_id),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def outbox_offer_key(self, item: dict) -> str:
        """The offer an outbox row was written for: its own key, else its
        campaign's (a row queued before the campaign carried one)."""
        own = _norm(item.get("offer_key"))
        if own or not item.get("campaign_id"):
            return own
        async with self._connect() as db:
            async with db.execute(
                "SELECT COALESCE(offer_key, '') FROM campaigns WHERE id = ?",
                (item["campaign_id"],),
            ) as cursor:
                row = await cursor.fetchone()
        return _norm(row[0]) if row else ""

    async def record_offer_routes(self, campaign_id: str, routes: list[dict]) -> None:
        """Keep why each prospect of a campaign got its offer:
        [{prospect_id, offer_key, reason, is_default}]."""
        if not campaign_id or not routes:
            return
        async with self._connect() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO offer_routes "
                "(campaign_id, prospect_id, offer_key, reason, is_default) VALUES (?, ?, ?, ?, ?)",
                [(campaign_id, r["prospect_id"], _norm(r.get("offer_key")),
                  r.get("reason") or "", int(bool(r.get("is_default")))) for r in routes],
            )
            await db.commit()

    async def outbox_offers(self, rows: list[dict]) -> dict[str, dict]:
        """Per outbox id: the offer it carries (its own key, else its
        campaign's) and the recorded routing reason, if any."""
        ids = [r["id"] for r in rows if r.get("id")]
        if not ids:
            return {}
        result: dict[str, dict] = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                async with db.execute(
                    f"SELECT o.id, {self._OUTBOX_OFFER_SQL} AS offer_key, "
                    "COALESCE(r.reason, '') AS reason, COALESCE(r.is_default, 0) AS is_default "
                    "FROM outbox o "
                    "LEFT JOIN campaigns c ON c.id = o.campaign_id AND o.campaign_id != '' "
                    "LEFT JOIN offer_routes r ON r.campaign_id = o.campaign_id "
                    "AND r.prospect_id = o.prospect_id AND o.campaign_id != '' "
                    f"WHERE o.id IN ({', '.join('?' for _ in chunk)})",
                    chunk,
                ) as cursor:
                    for row in await cursor.fetchall():
                        result[row["id"]] = dict(row)
        return result

    async def queued_offer_outbox(self, ids: list[str] | None = None) -> list[dict]:
        """Queued sequence rows that carry an offer, each with its resolved
        ``offer``, its prospect, and the live demo for that prospect and offer
        (``demo_id`` / ``demo_status``, None when no demo is registered).
        ``ids`` limits the scan to those outbox rows."""
        offer = self._OUTBOX_OFFER_SQL
        sql = (
            f"SELECT o.*, {offer} AS offer, d.id AS demo_id, d.status AS demo_status, "
            "d.kind AS demo_kind, COALESCE(p.first_name, '') AS first_name, "
            "COALESCE(p.last_name, '') AS last_name, COALESCE(p.company, '') AS company "
            "FROM outbox o "
            "LEFT JOIN campaigns c ON c.id = o.campaign_id AND o.campaign_id != '' "
            "LEFT JOIN prospects p ON p.id = o.prospect_id "
            f"LEFT JOIN demos d ON d.prospect_id = o.prospect_id AND d.offer_key = {offer} "
            "AND d.status != 'retired' "
            "WHERE o.kind = 'sequence' AND o.status IN ('pending_review', 'approved') "
            f"AND {offer} != ''"
        )
        params: list = []
        if ids is not None:
            ids = [i for i in ids if i]
            if not ids:
                return []
            sql += f" AND o.id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)
        sql += " ORDER BY o.send_at ASC, o.created_at ASC LIMIT 2000"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def demos_due_for_retirement(self, days: int) -> list[dict]:
        """Ready demos whose prospect never replied, with nothing queued for
        them, and no email to them (nor the demo itself) newer than ``days``."""
        cutoff = (_utcnow() - timedelta(days=max(0, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT d.* FROM demos d
                   LEFT JOIN prospects p ON p.id = d.prospect_id
                   WHERE d.status = 'ready'
                     AND COALESCE(p.status, '') NOT IN ('replied', 'meeting', 'closed')
                     AND NOT EXISTS (
                       SELECT 1 FROM outbox o WHERE o.prospect_id = d.prospect_id
                         AND o.kind = 'sequence'
                         AND o.status IN ('pending_review', 'approved', 'sending'))
                     AND MAX(
                       REPLACE(COALESCE((SELECT MAX(REPLACE(o.sent_at, 'T', ' ')) FROM outbox o
                                         WHERE o.prospect_id = d.prospect_id AND o.status = 'sent'),
                                        ''), 'T', ' '),
                       REPLACE(COALESCE(d.ready_at, d.created_at), 'T', ' ')) < ?""",
                (cutoff,),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── Exclusions (suppressions) ──

    async def find_suppressions(self, email: str) -> list[dict]:
        """Every active rule that covers this address, opt-outs first."""
        email = _norm(email)
        if "@" not in email:
            return []
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            return await _matching_rules(db, email)

    async def add_suppression(
        self, kind: str, value: str, *, source: str, reason: str = "",
        include_subdomains: bool = False, prospect_id: str = "", actor: str = "",
    ) -> tuple[dict, bool]:
        """Add a rule, or return the active one with the same kind, value and
        source (so a second opt-out from one person is one rule). Queued mail
        the rule covers is blocked in the same transaction: it leaves the
        approval queue and needs an explicit decision to come back.
        Returns (rule, created)."""
        subdomains = 1 if (kind == "domain" and include_subdomains) else 0
        now = _utcnow().isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM suppressions WHERE kind = ? AND value = ? AND source = ? "
                    "AND removed_at IS NULL", (kind, value, source),
                ) as cur:
                    found = await cur.fetchone()
                if found:
                    rule, created = dict(found), False
                    if subdomains and not rule["include_subdomains"]:
                        await db.execute(
                            "UPDATE suppressions SET include_subdomains = 1, updated_at = ? "
                            "WHERE id = ?", (now, rule["id"]))
                        await _rule_event(db, rule["id"], "widened", actor,
                                          "now also matches subdomains")
                        rule["include_subdomains"] = 1
                else:
                    rule = {
                        "id": _new_id(), "kind": kind, "value": value,
                        "include_subdomains": subdomains, "source": source,
                        "reason": reason, "prospect_id": prospect_id,
                        "created_by": actor, "created_at": now, "updated_at": now,
                        "removed_at": None, "removed_by": "", "removed_note": "",
                    }
                    await db.execute(
                        """INSERT INTO suppressions (id, kind, value, include_subdomains, source,
                           reason, prospect_id, created_by, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (rule["id"], kind, value, subdomains, source, reason, prospect_id,
                         actor, now, now))
                    await _rule_event(db, rule["id"], "added", actor, reason)
                    created = True
                rule["blocked"] = await _block_queued(db, rule, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return rule, created

    async def remove_suppression(self, rule_id: str, *, actor: str = "",
                                 note: str = "") -> dict | None:
        """Lift one rule. Other rules for the same address stay, and mail it
        blocked stays blocked until someone sends it back for review."""
        now = _utcnow().isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE suppressions SET removed_at = ?, removed_by = ?, removed_note = ?, "
                "updated_at = ? WHERE id = ? AND removed_at IS NULL",
                (now, actor, note, now, rule_id))
            if not cursor.rowcount:
                return None
            await _rule_event(db, rule_id, "removed", actor, note)
            await db.commit()
            async with db.execute("SELECT * FROM suppressions WHERE id = ?", (rule_id,)) as cur:
                return dict(await cur.fetchone())

    async def get_suppression(self, rule_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM suppressions WHERE id = ?", (rule_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_suppressions(self, query: str = "", source: str = "",
                                removed: bool = False, limit: int = 500) -> list[dict]:
        where = ["removed_at IS NOT NULL" if removed else "removed_at IS NULL"]
        params: list = []
        if query:
            where.append("(value LIKE ? ESCAPE '\\' OR reason LIKE ? ESCAPE '\\')")
            like = "%" + _norm(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        if source:
            where.append("source = ?")
            params.append(source)
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM suppressions WHERE {' AND '.join(where)} "
                "ORDER BY COALESCE(removed_at, created_at) DESC LIMIT ?", params,
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def suppression_events(self, rule_id: str) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM suppression_events WHERE suppression_id = ? ORDER BY id",
                (rule_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def requeue_blocked_outbox(self, item_id: str) -> str:
        """Send a blocked email back to review: 'requeued', 'excluded' (a
        rule still covers it), or 'not_blocked'. Never approves it."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT to_email FROM outbox WHERE id = ? AND status = 'blocked'",
                    (item_id,),
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return "not_blocked"
                if await _matching_rules(db, row["to_email"]):
                    await db.rollback()
                    return "excluded"
                await db.execute(
                    "UPDATE outbox SET status = 'pending_review', requires_manual_review = 1, "
                    "error = '', updated_at = ? "
                    "WHERE id = ?", (_utcnow().isoformat(), item_id))
                await db.commit()
                return "requeued"
            except BaseException:
                await db.rollback()
                raise

    # ── Company holds and per-company sending capacity ──

    async def hold_company(self, company_id: str, *, reason: str, prospect_id: str = "",
                           note: str = "", actor: str = "") -> tuple[dict, bool]:
        """Pause cold mail to a company. One active hold per company; a second
        reason while it is held returns the existing hold. (hold, created)."""
        now = _utcnow().isoformat()
        hold_id = _new_id()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """INSERT OR IGNORE INTO company_holds
                   (id, company_id, reason, prospect_id, note, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (hold_id, company_id, reason, prospect_id, note, actor, now))
            created = bool(cursor.rowcount)
            await db.commit()
            async with db.execute(
                "SELECT * FROM company_holds WHERE company_id = ? AND released_at IS NULL",
                (company_id,),
            ) as cur:
                return dict(await cur.fetchone()), created

    async def release_company_hold(self, hold_id: str, *, actor: str = "",
                                   note: str = "") -> dict | None:
        now = _utcnow().isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE company_holds SET released_at = ?, released_by = ?, released_note = ? "
                "WHERE id = ? AND released_at IS NULL", (now, actor, note, hold_id))
            await db.commit()
            if not cursor.rowcount:
                return None
            async with db.execute("SELECT * FROM company_holds WHERE id = ?", (hold_id,)) as cur:
                return dict(await cur.fetchone())

    async def get_company_hold(self, company_id: str) -> dict | None:
        if not company_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM company_holds WHERE company_id = ? AND released_at IS NULL",
                (company_id,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_company_holds(self, released: bool = False, limit: int = 200) -> list[dict]:
        """Holds with the company name, who triggered them, and how much cold
        mail each is holding right now."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT h.*, COALESCE(c.name, '') AS company_name,
                       COALESCE(c.domain, '') AS company_domain,
                       COALESCE(p.email, '') AS prospect_email,
                       TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, ''))
                           AS prospect_name,
                       (SELECT COUNT(*) FROM outbox o
                          LEFT JOIN prospects op ON op.id = o.prospect_id
                          WHERE (o.company_id = h.company_id OR (o.company_id = ''
                                 AND (op.company_id = h.company_id
                                      OR (COALESCE(op.company_id, '') = '' AND c.domain != ''
                                          AND substr(o.to_email, instr(o.to_email, '@') + 1)
                                              = c.domain))))
                          AND o.kind = 'sequence'
                          AND o.status IN ('pending_review', 'approved')) AS queued
                    FROM company_holds h
                    LEFT JOIN companies c ON c.id = h.company_id
                    LEFT JOIN prospects p ON p.id = h.prospect_id
                    WHERE h.released_at IS {'NOT ' if released else ''}NULL
                    ORDER BY COALESCE(h.released_at, h.created_at) DESC LIMIT ?""",
                (int(limit),),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def company_contact_usage(self, company_id: str, *, exclude_campaign: str = "",
                                    exclude_prospect: str = "") -> dict:
        """(new contacts in the last 24 hours, contacts with an unfinished
        sequence) at one company, leaving out one person across all campaigns
        when asked. A contact occupies at most one slot."""
        async with self._connect() as db:
            return await _company_usage(db, company_id, exclude_campaign, exclude_prospect)

    async def claim_for_send(self, item: dict, mailbox: str, *, company_id: str = "",
                             max_new_per_day: int = 0, max_active: int = 0,
                             respect_holds: bool = True) -> tuple[str, dict]:
        """Claim one approved email for sending, re-checking every policy under
        the write lock. Returns ("claimed", {}) or (code, detail):

          suppressed            an exclusion covers the recipient; the row is
                                now 'blocked' (any kind of email)
          company_hold          cold mail to this company is paused
          company_daily_limit   the company had its new contacts for 24 hours
          company_active_limit  the company has its unfinished sequences
          stale                 the row changed since the due scan, or its
                                sequence was paused (out of office)

        A claimed first email is status 'sending' until the provider answers,
        so it already counts against its company: two senders cannot both see
        the same free slot. A send that fails goes back to 'approved' or to
        'failed' and frees the slot; a sent one counts once, as one contact.
        """
        columns = ("to_email", "subject", "body", "generation_id", "send_at", "mailbox",
                   "manually_edited", "revision")
        now = _utcnow().isoformat()
        async with aiosqlite.connect(
            self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
        ) as db:
            db.row_factory = aiosqlite.Row
            await _register_functions(db)
            await db.execute("BEGIN IMMEDIATE")
            try:
                verdict = await _claim_verdict(db, item, company_id, max_new_per_day,
                                               max_active, respect_holds)
                if verdict[0] == "suppressed":
                    await db.execute(
                        "UPDATE outbox SET status = 'blocked', requires_manual_review = 1, "
                        "error = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'approved'",
                        (verdict[1]["error"], now, item["id"]))
                if verdict[0] != "claimed":
                    await db.execute("COMMIT")
                    return verdict
                matches = " AND ".join(f"{column} = ?" for column in columns)
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'sending', mailbox = ?, company_id = ?, "
                    "approved_hash = outbox_hash(to_email, subject, body, ?, generation_id), "
                    f"updated_at = ? WHERE id = ? AND status = 'approved' AND {matches} "
                    "AND approved_revision = revision "
                    f"AND approved_hash = {_OUTBOX_HASH_SQL} "
                    # A flagged draft sends only once a person accepted its flags.
                    "AND (flags = '' OR flags_accepted_by != '') "
                    "AND NOT EXISTS (SELECT 1 FROM settings "
                    "WHERE key IN ('operator_pause', 'sending_paused') "
                    "AND COALESCE(value, '') != '') "
                    "AND (outbox.kind = 'reply' OR NOT EXISTS ("
                    "SELECT 1 FROM warmup_inboxes WHERE email = ? AND status = 'paused')) "
                    # A pause set after the due scan still holds this email back.
                    f"AND NOT {_PAUSED_OUTBOX_SQL}",
                    (mailbox, company_id, mailbox, now, item["id"],
                     *(item[c] for c in columns), _norm(mailbox)))
                if not cursor.rowcount:
                    await db.execute("ROLLBACK")
                    return "stale", {}
                if item.get("kind") == "sequence" and item.get("campaign_id"):
                    # The rest of the thread counts against the same company.
                    await db.execute(
                        "UPDATE outbox SET company_id = ? WHERE campaign_id = ? "
                        "AND prospect_id = ? AND kind = 'sequence' AND company_id != ?",
                        (company_id, item["campaign_id"], item["prospect_id"], company_id))
                await db.execute("COMMIT")
                return "claimed", {}
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    # ── Out-of-office pauses (see mercury/ooo.py) ──

    async def record_auto_reply(
        self, *, message_key: str, kind: str, received_at: str, prospect_id: str = "",
        from_email: str = "", mailbox: str = "", subject: str = "", excerpt: str = "",
        detected_by: str = "", parsed: dict | None = None, can_pause: bool = True,
        now: str | None = None,
    ) -> tuple[str, dict | None]:
        """Keep one automatic message, and pause the contact's sequence when it
        is a vacation reply. One transaction. Returns (outcome, active pause):

          duplicate  this message was recorded before; nothing changes
          recorded   not a vacation reply (receipt, acknowledgement)
          ignored    a vacation reply that must not pause: the contact is out
                     of the sequence, or the message is older than a pause
                     that already ended
          paused     a new pause
          updated    a newer vacation reply changed the active pause
          kept       the active pause stays as it is: the message is not newer
                     than the one that set it (or than a person's correction),
                     or it has no usable date while the pause already has one
        """
        now = now or _utcnow().isoformat()
        parsed = parsed or {}
        async with aiosqlite.connect(
            self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
        ) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO auto_replies
                       (message_key, prospect_id, from_email, mailbox, kind, detected_by,
                        subject, excerpt, received_at, return_text, parsed_resume_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (message_key, prospect_id, _norm(from_email), _norm(mailbox), kind,
                     detected_by, subject[:300], excerpt[:1000], received_at,
                     parsed.get("text", ""), parsed.get("resume_at"), now))
                if not cursor.rowcount:
                    pause = await _active_pause(db, prospect_id) if prospect_id else None
                    await db.execute("COMMIT")
                    return "duplicate", pause
                outcome, pause = "recorded", None
                if kind == "out_of_office" and prospect_id:
                    outcome, pause = await self._apply_pause(
                        db, prospect_id, message_key, received_at, parsed, can_pause, now)
                await db.execute(
                    "UPDATE auto_replies SET outcome = ?, pause_id = ? WHERE message_key = ?",
                    (outcome, (pause or {}).get("id", ""), message_key))
                await db.execute("COMMIT")
                return outcome, pause
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def _apply_pause(db, prospect_id: str, message_key: str, received_at: str,
                           parsed: dict, can_pause: bool, now: str):
        if not can_pause:
            return "ignored", None
        scheduled = bool(parsed.get("resume_at"))
        fields = {
            "review_state": "scheduled" if scheduled else "needs_review",
            "review_reason": "" if scheduled else (parsed.get("review_reason") or "no_date"),
            "message_key": parsed.get("source_message_key", message_key), "message_at": received_at,
            "confidence": float(parsed.get("confidence") or 0.0),
            "return_text": parsed.get("text", ""), "return_date": parsed.get("local_date", ""),
            "resume_at": parsed.get("resume_at"),
            "timezone": parsed.get("timezone", ""),
        }
        active = await _active_pause(db, prospect_id)
        if active is None:
            async with db.execute(
                "SELECT MAX(ended_at) FROM sequence_pauses "
                "WHERE prospect_id = ? AND ended_at IS NOT NULL", (prospect_id,),
            ) as cur:
                (last_end,) = await cur.fetchone()
            if last_end and received_at <= str(last_end):
                return "ignored", None
            pause = {"id": _new_id(), "prospect_id": prospect_id, **fields}
            await db.execute(
                f"INSERT INTO sequence_pauses ({', '.join(pause)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in pause)}, ?, ?)",
                (*pause.values(), now, now))
            return "paused", await _active_pause(db, prospect_id)

        newer = received_at > str(active.get("message_at") or "")
        if active["manual_override"] and received_at <= str(active.get("override_at") or ""):
            newer = False
        if not newer or (not scheduled and active["review_state"] == "scheduled"):
            return "kept", active
        await db.execute(
            f"UPDATE sequence_pauses SET {', '.join(f'{k} = ?' for k in fields)}, "
            "manual_override = 0, updated_at = ? WHERE id = ?",
            (*fields.values(), now, active["id"]))
        return "updated", await _active_pause(db, prospect_id)

    async def get_active_pause(self, prospect_id: str) -> dict | None:
        if not prospect_id:
            return None
        async with self._connect() as db:
            return await _active_pause(db, prospect_id)

    async def get_pause(self, pause_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sequence_pauses WHERE id = ? OR prospect_id = ? "
                "ORDER BY ended_at IS NOT NULL, created_at DESC LIMIT 1", (pause_id, pause_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def paused_prospect_ids(self) -> set[str]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT prospect_id FROM sequence_pauses WHERE ended_at IS NULL"
            ) as cur:
                return {row[0] for row in await cur.fetchall()}

    async def due_pauses(self, now: str) -> list[dict]:
        """Active pauses whose return time has come."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sequence_pauses WHERE ended_at IS NULL "
                "AND review_state = 'scheduled' AND resume_at IS NOT NULL AND resume_at <= ? "
                "ORDER BY resume_at", (now,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def list_pauses(self, ended: bool = False, limit: int = 200, *,
                          active_only: bool | None = None) -> list[dict]:
        """Pauses with who they are, and the cold mail each is holding."""
        held = ", ".join(f"'{s}'" for s in _PAUSE_HELD)
        where = "" if active_only is False else f"WHERE s.ended_at IS {'NOT ' if ended else ''}NULL"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT s.*, p.first_name, p.last_name, p.email, p.title,
                       COALESCE(NULLIF(c.name, ''), p.company, '') AS company_name,
                       COALESCE(p.email, '') AS prospect_email,
                       TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, ''))
                           AS prospect_name,
                       COALESCE(p.company, '') AS company,
                       COALESCE(p.status, '') AS prospect_status,
                       COALESCE(p.email_status, '') AS prospect_email_status,
                       (SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = s.prospect_id
                          AND o.kind = 'sequence' AND o.status IN ({held})) AS queued,
                       (SELECT MIN(o.send_at) FROM outbox o WHERE o.prospect_id = s.prospect_id
                          AND o.kind = 'sequence' AND o.status IN ({held})) AS next_send_at
                    FROM sequence_pauses s
                    LEFT JOIN prospects p ON p.id = s.prospect_id
                    LEFT JOIN companies c ON c.id = p.company_id
                    {where}
                    ORDER BY {'s.ended_at DESC' if ended else
                              "s.review_state = 'scheduled', COALESCE(s.resume_at, s.created_at)"}
                    LIMIT ?""",
                (int(limit),),
            ) as cur:
                return [{**dict(r), "queued_count": r["queued"]} for r in await cur.fetchall()]

    async def auto_replies_for(self, prospect_id: str, limit: int = 20) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM auto_replies WHERE prospect_id = ? "
                "ORDER BY received_at DESC LIMIT ?", (prospect_id, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def set_pause_resume_at(self, pause_id: str, resume_at: str, *, actor: str = "",
                                  now: str | None = None,
                                  return_date: str | None = None) -> tuple[dict, dict] | None:
        """A person sets (or corrects) the return time. Returns (before, after),
        or None when the pause is not active. Approval status is untouched."""
        now = now or _utcnow().isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            before = await self.get_pause(pause_id)
            if not before or before["ended_at"]:
                return None
            cursor = await db.execute(
                "UPDATE sequence_pauses SET resume_at = ?, return_date = COALESCE(?, return_date), "
                "review_state = 'scheduled', "
                "review_reason = '', manual_override = 1, override_at = ?, override_by = ?, "
                "updated_at = ? WHERE id = ? AND ended_at IS NULL",
                (resume_at, return_date, now, actor, now, before["id"]))
            await db.commit()
            if not cursor.rowcount:
                return None
        return before, await self.get_pause(pause_id)

    async def resume_pause(self, pause_id: str, reason: str = "operator",
                           now: datetime | str | None = None, *, start_at: str | None = None,
                           actor: str = "", due_at: str | None = None, only_due: bool = False):
        """Resume by pause ID (tuple result) or prospect ID (legacy dict result).

        The current return date is checked after acquiring the write lock, so
        an operator correction or newer reply wins over a stale due scan.
        """
        by_pause = start_at is not None
        now_s = now if isinstance(now, str) else _ts(now)
        if only_due:
            due_at = now_s
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM sequence_pauses WHERE (id = ? OR prospect_id = ?) "
                    "AND ended_at IS NULL", (pause_id, pause_id),
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return (None, 0) if by_pause else None
                pause = dict(row)
                resume_at = _parse_ts(pause["resume_at"])
                if due_at is not None and (pause["review_state"] != "scheduled"
                        or resume_at is None or resume_at > _parse_ts(due_at)):
                    await db.rollback()
                    return (None, 0) if by_pause else None
                anchor = start_at or (pause["resume_at"] if resume_at is not None
                                      and resume_at <= _parse_ts(now_s) else now_s)
                moved = await self._reschedule_after_pause(db, pause["prospect_id"], anchor, now_s)
                await db.execute(
                    "UPDATE sequence_pauses SET status = 'resumed', ended_at = ?, ended_by = ?, "
                    "ended_reason = ?, updated_at = ? WHERE id = ?",
                    (now_s, actor, reason, now_s, pause["id"]))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        ended = {**pause, "status": "resumed", "state": "resumed", "ended_at": now_s,
                 "ended_by": actor, "ended_reason": reason, "rescheduled": moved}
        return (ended, moved) if by_pause else ended

    async def end_pause(self, prospect_id: str, *, reason: str, actor: str = "",
                        now: str | None = None) -> dict | None:
        """Supersede a contact's pause: they replied, opted out, bounced or
        were closed, so the sequence must not come back. Its queued mail is
        left to the rules that apply to it (stop-on-reply, exclusions)."""
        now = now or _utcnow().isoformat()
        async with self._connect() as db:
            pause = await _active_pause(db, prospect_id)
            if not pause:
                return None
            await db.execute(
                "UPDATE sequence_pauses SET status = 'superseded', ended_at = ?, ended_by = ?, "
                "ended_reason = ?, updated_at = ? WHERE id = ? AND ended_at IS NULL",
                (now, actor, reason, now, pause["id"]))
            await db.commit()
        return {**pause, "status": "superseded", "ended_at": now, "ended_reason": reason}

    # ── Signal vocabulary (governed; user-confirmed before collection) ──

    async def upsert_signal_code(
        self,
        code: str,
        *,
        label: str = "",
        description: str = "",
        category: str = "",
        value_type: str = "text",
        collector: str = "",
        cost_note: str = "",
        confidence_floor: float = 0.0,
        status: str | None = None,
    ):
        """Register a signal in the vocabulary. Never downgrades a user's
        decision: an existing row's ``status`` is preserved unless explicitly
        passed, so re-seeding can't silently re-enable a rejected signal."""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO signal_codes
                       (code, label, description, category, value_type,
                        collector, cost_note, confidence_floor, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'proposed'))
                   ON CONFLICT(code) DO UPDATE SET
                       label = excluded.label,
                       description = excluded.description,
                       category = excluded.category,
                       value_type = excluded.value_type,
                       collector = excluded.collector,
                       cost_note = excluded.cost_note,
                       confidence_floor = excluded.confidence_floor,
                       status = COALESCE(?, signal_codes.status),
                       updated_at = CURRENT_TIMESTAMP""",
                (code, label, description, category, value_type, collector,
                 cost_note, float(confidence_floor), status, status),
            )
            await db.commit()

    async def get_signal_codes(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signal_codes"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY category, code"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def set_signal_status(self, code: str, status: str) -> bool:
        """Confirm / reject a proposed signal. Only confirmed signals are
        collected — Mercury proposes, the user decides."""
        if status not in ("proposed", "confirmed", "rejected"):
            raise ValueError(f"invalid signal status: {status}")
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE signal_codes SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE code = ?",
                (status, code),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def confirmed_signal_codes(self) -> set[str]:
        return {r["code"] for r in await self.get_signal_codes(status="confirmed")}

    # ── Pains (governed like signals: proposed -> confirmed / rejected) ──

    PAIN_STATUSES = ("proposed", "confirmed", "rejected")
    # What an edit may change. status is not here: only set_pain_status
    # moves it, and it records who.
    _PAIN_EDITABLE = frozenset({
        "label", "market", "sector", "owner_words", "scene", "cost",
        "signal_codes", "offer_key", "evidence", "avoid_terms",
    })
    _PAIN_LISTS = {"signal_codes": "signal_codes_json", "evidence": "evidence_json",
                   "avoid_terms": "avoid_terms_json"}

    @classmethod
    def _decode_pain(cls, row) -> dict:
        pain = dict(row)
        for name, column in cls._PAIN_LISTS.items():
            try:
                value = json.loads(pain.pop(column, "[]") or "[]")
            except ValueError:
                value = []
            pain[name] = value if isinstance(value, list) else []
        return pain

    async def add_pain(
        self,
        code: str,
        *,
        label: str = "",
        market: str = "",
        sector: str = "",
        owner_words: str = "",
        scene: str = "",
        cost: str = "",
        signal_codes: list[str] | None = None,
        offer_key: str = "",
        evidence: list[str] | None = None,
        avoid_terms: list[str] | None = None,
        origin_text: str = "",
        source: str = "manual",
        status: str = "proposed",
        status_by: str = "",
        status_note: str = "",
    ) -> bool:
        """Insert one pain. False when the code already exists (nothing is
        overwritten: an existing row keeps its status and its edits). A
        status other than 'proposed' needs ``status_by``, the person who
        decided it; the database refuses it otherwise."""
        if status not in self.PAIN_STATUSES:
            raise ValueError(f"invalid pain status: {status}")
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO pains
                       (code, label, market, sector, owner_words, scene, cost,
                        signal_codes_json, offer_key, evidence_json, avoid_terms_json,
                        origin_text, source, status, status_by, status_at, status_note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code, label, _norm(market), sector, owner_words, scene, cost,
                 json.dumps(list(signal_codes or [])), _norm(offer_key),
                 json.dumps(list(evidence or [])), json.dumps(list(avoid_terms or [])),
                 origin_text or label or owner_words, source, status, status_by,
                 now if status != "proposed" else None, status_note),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_pain(self, code: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM pains WHERE code = ?",
                                  ((code or "").strip().upper(),)) as cursor:
                row = await cursor.fetchone()
                return self._decode_pain(row) if row else None

    async def list_pains(self, status: str | None = None, market: str | None = None,
                         offer_key: str | None = None) -> list[dict]:
        """Pains in a stable order (code). ``market`` and ``offer_key`` match
        exactly (case-insensitive); '' matches the pains that name none."""
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if market is not None:
            where.append("market = ?")
            params.append(_norm(market))
        if offer_key is not None:
            where.append("offer_key = ?")
            params.append(_norm(offer_key))
        sql = "SELECT * FROM pains" + (" WHERE " + " AND ".join(where) if where else "")
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql + " ORDER BY code", params) as cursor:
                return [self._decode_pain(r) for r in await cursor.fetchall()]

    async def update_pain(self, code: str, fields: dict,
                          expected_revision: int | None = None) -> int | None:
        """Edit a pain's content. Returns the new revision, or None when the
        pain does not exist or ``expected_revision`` is stale. Never touches
        the status."""
        fields = {k: v for k, v in fields.items() if k in self._PAIN_EDITABLE}
        if not fields:
            raise ValueError("nothing to change")
        sets, params = [], []
        for name, value in fields.items():
            if name in self._PAIN_LISTS:
                sets.append(f"{self._PAIN_LISTS[name]} = ?")
                params.append(json.dumps(list(value or [])))
            else:
                sets.append(f"{name} = ?")
                params.append(_norm(value) if name in ("market", "offer_key") else value)
        sql = ("UPDATE pains SET " + ", ".join(sets) +
               ", revision = revision + 1, updated_at = ? WHERE code = ?")
        params += [_utcnow().isoformat(), (code or "").strip().upper()]
        if expected_revision is not None:
            sql += " AND revision = ?"
            params.append(int(expected_revision))
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            if cursor.rowcount == 0:
                await db.commit()
                return None
            async with db.execute("SELECT revision FROM pains WHERE code = ?",
                                  ((code or "").strip().upper(),)) as cur:
                (revision,) = await cur.fetchone()
            await db.commit()
            return revision

    async def set_pain_status(self, code: str, status: str, actor: str, note: str = "",
                              expected_revision: int | None = None) -> bool:
        """Confirm, reject or reopen a pain. ``actor`` is the person (or the
        client acting for them), recorded with the time; the database refuses
        a blank actor and the trainer/system names. False when the pain does
        not exist or ``expected_revision`` is stale."""
        if status not in self.PAIN_STATUSES:
            raise ValueError(f"invalid pain status: {status}")
        now = _utcnow().isoformat()
        sql = ("UPDATE pains SET status = ?, status_by = ?, status_at = ?, status_note = ?, "
               "revision = revision + 1, updated_at = ? WHERE code = ?")
        params: list = [status, actor, now, note, now, (code or "").strip().upper()]
        if expected_revision is not None:
            sql += " AND revision = ?"
            params.append(int(expected_revision))
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount > 0

    async def pain_stats(self) -> dict[str, dict]:
        """Sends and replies attributable to each pain, for the learning loop.

        ``sends``: sent sequence emails that carry the pain's code.
        ``prospects``: distinct people those went to.
        ``replies``: distinct people among them who answered at or after a
        send that used the pain (out-of-office notices excluded).
        ``positive``: those whose reply was classified interested.

        Deliberately simple: a reply counts for every pain the person was
        sent before it, and it is not weighed by how long after.
        """
        from mercury.metrics import _REPLY_EVENTS

        sql = f"""
            SELECT o.pain_code AS code,
                   COUNT(DISTINCT o.id) AS sends,
                   COUNT(DISTINCT o.prospect_id) AS prospects,
                   COUNT(DISTINCT CASE WHEN r.k IS NOT NULL AND COALESCE(r.intent, '') != 'ooo'
                                       THEN o.prospect_id END) AS replies,
                   COUNT(DISTINCT CASE WHEN r.intent = 'interested'
                                       THEN o.prospect_id END) AS positive
            FROM outbox o
            LEFT JOIN ({_REPLY_EVENTS}) r
                   ON r.k = o.prospect_id AND datetime(r.ts) >= datetime(o.sent_at)
            WHERE o.pain_code != '' AND o.status = 'sent' AND o.kind = 'sequence'
            GROUP BY o.pain_code"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cursor:
                return {r["code"]: {"sends": r["sends"], "prospects": r["prospects"],
                                    "replies": r["replies"], "positive": r["positive"]}
                        for r in await cursor.fetchall()}

    async def company_signal_codes(self, company_id: str, confirmed_only: bool = True) -> set[str]:
        """The signals a company currently carries (newest observation of
        each, and true), by default only those a person has confirmed."""
        if not company_id:
            return set()
        sql = self._CURRENT_CTE + "SELECT DISTINCT signal_code FROM positive WHERE company_id = ?"
        async with self._connect() as db:
            async with db.execute(sql, (company_id,)) as cursor:
                codes = {row[0] for row in await cursor.fetchall()}
        if confirmed_only:
            codes &= await self.confirmed_signal_codes()
        return codes

    # ── Observations (every fact is a row, never a column) ──

    async def add_observation(
        self,
        signal_code: str,
        *,
        company_id: str = "",
        prospect_id: str = "",
        collector: str = "",
        value_num: float | None = None,
        value_text: str = "",
        confidence: float = 1.0,
        evidence_url: str = "",
        run_id: str = "",
        observed_at: str | None = None,
        detail: dict | None = None,
    ) -> str:
        """Record one fact. Raises if signal_code isn't in the vocabulary —
        a typo must fail loudly rather than create a junk signal."""
        obs_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?, ?)""",
                (obs_id, company_id, prospect_id, signal_code, collector,
                 value_num, value_text, float(confidence), evidence_url,
                 observed_at, run_id, json.dumps(detail) if detail else ""),
            )
            await db.commit()
        return obs_id

    async def add_observations(self, rows: list[dict], run_id: str = "") -> int:
        """Batch insert. Flushed per batch by callers — a long run that dies
        must not lose everything it observed."""
        if not rows:
            return 0
        payload = [
            (
                _new_id(), r.get("company_id", ""), r.get("prospect_id", ""),
                r["signal_code"], r.get("collector", ""), r.get("value_num"),
                r.get("value_text", ""), float(r.get("confidence", 1.0)),
                r.get("evidence_url", ""), r.get("observed_at"),
                r.get("run_id", run_id),
                json.dumps(r["detail"]) if r.get("detail") else "",
            )
            for r in rows
        ]
        async with self._connect() as db:
            await db.executemany(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?, ?)""",
                payload,
            )
            await db.commit()
        return len(payload)

    async def get_observations(
        self,
        company_id: str = "",
        signal_code: str = "",
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        where, params = [], []
        if company_id:
            where.append("company_id = ?")
            params.append(company_id)
        if signal_code:
            where.append("signal_code = ?")
            params.append(signal_code)
        sql = "SELECT * FROM observations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if latest_only:
            # Newest observation per (company, signal) — the current view of
            # the world, with history still on disk underneath.
            sql = (
                "SELECT * FROM (" + sql + " ORDER BY observed_at DESC) "
                "GROUP BY company_id, signal_code"
            )
        else:
            sql += " ORDER BY observed_at DESC"
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # A boolean signal is recorded either way: "checked, not running ads" is a
    # real finding, and a later flip from 0 to 1 is a real event. But a COHORT
    # asks who *has* the signal, so it must read the value, not merely the
    # presence of a row. Without this, "companies running Google Ads" silently
    # means "companies we checked for Google Ads" — which is everyone.
    #
    # `latest` is the current view of the world (newest observation per company
    # and signal, with history still on disk underneath). `positive` is the
    # subset where the finding is actually true — value_num IS NULL covers text
    # signals like INCUMBENT_AGENCY, where the row's existence IS the finding.
    _CURRENT_CTE = """
    WITH latest AS (
        SELECT o.* FROM observations o
        JOIN (SELECT company_id, signal_code, MAX(observed_at) AS t
              FROM observations GROUP BY company_id, signal_code) newest
          ON newest.company_id = o.company_id
         AND newest.signal_code = o.signal_code
         AND newest.t = o.observed_at
    ),
    positive AS (
        SELECT * FROM latest WHERE value_num IS NULL OR value_num != 0
    )
    """

    async def signal_counts(self) -> list[dict]:
        """How many entities actually carry each signal — the cohort sizes."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                self._CURRENT_CTE + """
                SELECT p.signal_code,
                       COALESCE(sc.label, p.signal_code) AS label,
                       sc.status AS status,
                       COUNT(DISTINCT p.company_id) AS companies,
                       COUNT(*) AS observations
                FROM positive p
                LEFT JOIN signal_codes sc ON sc.code = p.signal_code
                GROUP BY p.signal_code
                ORDER BY companies DESC"""
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def cohort(
        self,
        require: list[str],
        exclude: list[str] | None = None,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[str]:
        """Company IDs carrying ALL required signals and none excluded.

        Set intersection happens in SQL — doing it in application code over a
        capped SELECT silently returns the wrong cohort.
        """
        if not require:
            return []
        req_ph = ",".join("?" for _ in require)
        sql = self._CURRENT_CTE + (
            f"SELECT company_id FROM positive "
            f"WHERE signal_code IN ({req_ph}) AND company_id != '' "
            "AND confidence >= ? "
            "GROUP BY company_id HAVING COUNT(DISTINCT signal_code) = ?"
        )
        params: list = [*require, float(min_confidence), len(set(require))]
        if exclude:
            exc_ph = ",".join("?" for _ in exclude)
            sql += (
                " AND company_id NOT IN ("
                f"SELECT company_id FROM positive WHERE signal_code IN ({exc_ph})"
                ")"
            )
            params.extend(exclude)
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def company_signals(self, company_id: str) -> dict[str, dict]:
        """The newest observation of each signal for one company, with the
        signal's label and value type: what offer routing and the brief read."""
        if not company_id:
            return {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                self._CURRENT_CTE + """
                SELECT l.*, COALESCE(sc.label, '') AS label,
                       COALESCE(sc.value_type, 'text') AS value_type
                FROM latest l LEFT JOIN signal_codes sc ON sc.code = l.signal_code
                WHERE l.company_id = ?""",
                (company_id,),
            ) as cursor:
                return {r["signal_code"]: dict(r) for r in await cursor.fetchall()}

    async def cohort_share(
        self, require: list[str], exclude: list[str] | None = None,
        segments: list[str] | None = None,
    ) -> tuple[int, int]:
        """(matched, checked) for an aggregate-evidence cohort.

        ``checked`` is every company with a current observation of each
        required signal, whatever its value: the companies Mercury actually
        looked at. ``matched`` is the subset carrying them all and none of
        ``exclude``, the same reading ``cohort()`` uses. ``segments`` keeps
        only companies whose industry, or one of whose prospects' industry,
        is one of them.
        """
        if not require:
            return 0, 0
        require = list(dict.fromkeys(require))
        req_ph = ",".join("?" for _ in require)
        params: list = []

        def having(source: str) -> str:
            params.extend([*require, len(require)])
            return (f"SELECT company_id FROM {source} WHERE signal_code IN ({req_ph}) "
                    "AND company_id != '' GROUP BY company_id "
                    "HAVING COUNT(DISTINCT signal_code) = ?")

        segment_sql = ""
        segments = [s.strip().lower() for s in segments or [] if s and s.strip()]
        if segments:
            seg_ph = ",".join("?" for _ in segments)
            segment_sql = (
                " AND company_id IN (SELECT id FROM companies "
                f"WHERE lower(trim(industry)) IN ({seg_ph}) "
                "UNION SELECT company_id FROM prospects "
                f"WHERE company_id != '' AND lower(trim(industry)) IN ({seg_ph}))"
            )
        checked_sql = f"SELECT COUNT(*) FROM ({having('latest')}) WHERE 1 {segment_sql}"
        checked_params = list(params) + segments * 2
        params.clear()
        matched_sql = f"SELECT COUNT(*) FROM ({having('positive')}) WHERE 1 {segment_sql}"
        matched_params = list(params) + segments * 2
        if exclude:
            exc_ph = ",".join("?" for _ in exclude)
            matched_sql += (" AND company_id NOT IN (SELECT company_id FROM positive "
                            f"WHERE signal_code IN ({exc_ph}))")
            matched_params.extend(exclude)
        async with self._connect() as db:
            async with db.execute(self._CURRENT_CTE + checked_sql, checked_params) as cursor:
                checked = (await cursor.fetchone())[0]
            async with db.execute(self._CURRENT_CTE + matched_sql, matched_params) as cursor:
                matched = (await cursor.fetchone())[0]
        return int(matched), int(checked)

    async def companies_needing_profile(
        self, limit: int = 100, stale_days: int = 90
    ) -> list[dict]:
        """Companies a profile run should visit next.

        Never profiled, or last profiled longer ago than ``stale_days``. The
        staleness window is what makes re-observation a time series rather
        than a duplicate: "they dropped their agency last quarter" is only
        visible if you look again.

        Businesses with no domain are excluded — there is no site to read.
        They are not a failure, they are the NO_WEBSITE cohort.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.domain
                   FROM companies c
                   LEFT JOIN (
                       SELECT company_id, MAX(observed_at) AS last_seen
                       FROM observations WHERE collector = 'profile'
                       GROUP BY company_id
                   ) p ON p.company_id = c.id
                   WHERE c.domain != ''
                     AND (p.last_seen IS NULL
                          OR p.last_seen < datetime('now', ?))
                   ORDER BY c.created_at DESC
                   LIMIT ?""",
                (f"-{int(stale_days)} days", int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def count_companies_needing_profile(self, stale_days: int = 90) -> int:
        async with self._connect() as db:
            async with db.execute(
                """SELECT COUNT(*) FROM companies c
                   LEFT JOIN (
                       SELECT company_id, MAX(observed_at) AS last_seen
                       FROM observations WHERE collector = 'profile'
                       GROUP BY company_id
                   ) p ON p.company_id = c.id
                   WHERE c.domain != ''
                     AND (p.last_seen IS NULL OR p.last_seen < datetime('now', ?))""",
                (f"-{int(stale_days)} days",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Public-registry lookups (one cached answer per company) ──

    @staticmethod
    def _registry_row(row) -> dict:
        out = dict(row)
        for column, key in (("candidates_json", "candidates"), ("people_json", "people")):
            try:
                out[key] = json.loads(out.pop(column) or "[]")
            except (json.JSONDecodeError, TypeError):
                out[key] = []
        return out

    async def get_registry_lookup(self, company_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM registry_lookups WHERE company_id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._registry_row(row) if row else None

    async def get_registry_lookups(self, company_ids: list[str]) -> dict[str, dict]:
        ids = [c for c in dict.fromkeys(company_ids) if c]
        if not ids:
            return {}
        out: dict[str, dict] = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT * FROM registry_lookups WHERE company_id IN ({marks})", chunk
                ) as cursor:
                    for row in await cursor.fetchall():
                        out[row["company_id"]] = self._registry_row(row)
        return out

    async def save_registry_lookup(self, company_id: str, record: dict) -> None:
        """Replace the company's cached answer. ``record`` carries the same
        keys ``get_registry_lookup`` returns."""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO registry_lookups
                       (company_id, provider, status, reason, entity_name,
                        document_number, source_url, confidence, searched_name,
                        searched_city, candidates_json, people_json, looked_up_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                   ON CONFLICT(company_id) DO UPDATE SET
                       provider = excluded.provider, status = excluded.status,
                       reason = excluded.reason, entity_name = excluded.entity_name,
                       document_number = excluded.document_number,
                       source_url = excluded.source_url,
                       confidence = excluded.confidence,
                       searched_name = excluded.searched_name,
                       searched_city = excluded.searched_city,
                       candidates_json = excluded.candidates_json,
                       people_json = excluded.people_json,
                       looked_up_at = excluded.looked_up_at""",
                (company_id, record.get("provider", ""), record["status"],
                 record.get("reason", ""), record.get("entity_name", ""),
                 record.get("document_number", ""), record.get("source_url", ""),
                 float(record.get("confidence") or 0), record.get("searched_name", ""),
                 record.get("searched_city", ""),
                 json.dumps(record.get("candidates") or []),
                 json.dumps(record.get("people") or []),
                 record.get("looked_up_at")),
            )
            await db.commit()

    async def get_registry_reviews(self, prospect_ids: list[str]) -> dict[str, dict]:
        ids = [p for p in dict.fromkeys(prospect_ids) if p]
        out: dict[str, dict] = {}
        if not ids:
            return out
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT * FROM registry_name_reviews WHERE prospect_id IN ({marks})",
                    chunk,
                ) as cursor:
                    for row in await cursor.fetchall():
                        out[row["prospect_id"]] = dict(row)
        return out

    async def set_registry_review(self, prospect_id: str, company_id: str,
                                  person_name: str, decision: str,
                                  decided_by: str = "") -> None:
        if decision not in ("accepted", "dismissed"):
            raise ValueError(f"invalid registry review decision: {decision}")
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO registry_name_reviews
                       (prospect_id, company_id, person_name, decision, decided_by)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(prospect_id) DO UPDATE SET
                       company_id = excluded.company_id,
                       person_name = excluded.person_name,
                       decision = excluded.decision,
                       decided_by = excluded.decided_by,
                       decided_at = CURRENT_TIMESTAMP""",
                (prospect_id, company_id, person_name, decision, decided_by),
            )
            await db.commit()

    async def clear_registry_review(self, prospect_id: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM registry_name_reviews WHERE prospect_id = ?", (prospect_id,))
            await db.commit()
            return cursor.rowcount > 0

    # ── Run log (what ran, when, what it produced and cost) ──

    async def start_run(
        self, stage: str, provider: str = "", params: dict | None = None
    ) -> str:
        run_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO runs (id, stage, provider, params_json, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (run_id, stage, provider, json.dumps(params or {})),
            )
            await db.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        status: str = "completed",
        records: int = 0,
        cost_usd: float = 0.0,
        error: str = "",
    ):
        async with self._connect() as db:
            await db.execute(
                "UPDATE runs SET status = ?, records = ?, cost_usd = ?, "
                "error = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, int(records), float(cost_usd), error[:500], run_id),
            )
            await db.commit()

    async def sweep_stale_runs(self, older_than_hours: int = 6) -> int:
        """A killed collector can't close its own run — never trust it to."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE runs SET status = 'stale', ended_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running' "
                "AND started_at < datetime('now', ?)",
                (f"-{int(older_than_hours)} hours",),
            )
            await db.commit()
            return cursor.rowcount

    async def get_runs(self, limit: int = 25) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── Settings (operational flags: kill switch, counters) ──

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (key, str(value)),
            )
            await db.commit()

    async def increment_setting(self, key: str, by: int = 1) -> int:
        current = await self.get_setting(key, "0")
        try:
            value = int(current) + by
        except ValueError:
            value = by
        await self.set_setting(key, str(value))
        return value

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
        async with self._connect() as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (_norm(email),)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?",
                    (linkedin_url.strip(),),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            # Name + company dedup (case-insensitive, expression-indexed)
            if first_name and last_name and company:
                async with db.execute(
                    """SELECT 1 FROM prospects
                       WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                         AND LOWER(company) = ?""",
                    (first_name.lower(), last_name.lower(), company.lower()),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        if not reply_id:
            return
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

    # ── Inbound mail (stored before it is handled) ──

    async def record_inbound(
        self, *, provider: str, mailbox: str, external_id: str, rfc_message_id: str = "",
        in_reply_to: str = "", thread_references: str = "", thread_ref: str = "",
        from_email: str = "", subject: str = "", body: str = "", headers: dict | None = None,
        date_header: str = "", kind: str = "message", legacy_key: str = "",
    ) -> tuple[dict, bool]:
        """Store one message as read from one mailbox. Returns (row, created).

        The key is (provider, mailbox, external_id): the same id in another
        inbox is another message. A new row starts 'received', except:
        'skipped' when ``legacy_key`` was handled before this table existed
        (processed_replies), and 'duplicate' when another inbox already holds
        the same RFC Message-ID. It is linked to the sent email it answers."""
        now = _ts()
        mailbox, provider = _norm(mailbox), (provider or "").strip().lower()
        async with aiosqlite.connect(
            self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
        ) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                new_id = _new_id()
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO inbound_messages
                       (id, provider, mailbox, external_id, rfc_message_id, in_reply_to,
                        thread_references, thread_ref, from_email, subject, body, headers_json,
                        date_header, received_at, kind, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (new_id, provider, mailbox, external_id, (rfc_message_id or "").strip(),
                     (in_reply_to or "").strip(), (thread_references or "").strip(),
                     thread_ref or "", _norm(from_email), subject or "", body or "",
                     json.dumps(headers or {}, default=str), date_header or "",
                     header_time(date_header), kind, now))
                created = cursor.rowcount > 0
                if created:
                    await self._classify_new_inbound(db, new_id, legacy_key)
                async with db.execute(
                    "SELECT * FROM inbound_messages WHERE provider = ? AND mailbox = ? "
                    "AND external_id = ?", (provider, mailbox, external_id),
                ) as cur:
                    row = dict(await cur.fetchone())
                await db.execute("COMMIT")
                return row, created
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def _classify_new_inbound(db, row_id: str, legacy_key: str) -> None:
        async with db.execute("SELECT * FROM inbound_messages WHERE id = ?", (row_id,)) as cur:
            row = dict(await cur.fetchone())
        status, duplicate_of, note = "received", "", ""
        if legacy_key:
            async with db.execute("SELECT 1 FROM processed_replies WHERE reply_id = ?",
                                  (legacy_key,)) as cur:
                if await cur.fetchone():
                    status, note = "skipped", "handled before inbound messages were stored"
        if status == "received" and row["rfc_message_id"]:
            async with db.execute(
                "SELECT id FROM inbound_messages WHERE rfc_message_id = ? AND id != ? "
                "AND status NOT IN ('duplicate', 'skipped') ORDER BY created_at, rowid LIMIT 1",
                (row["rfc_message_id"], row_id),
            ) as cur:
                first = await cur.fetchone()
            if first:
                status, duplicate_of = "duplicate", first[0]
        # The sent email it answers: In-Reply-To first, then the References
        # chain from the newest id back.
        outbox_id = ""
        for ref in [row["in_reply_to"], *reversed(row["thread_references"].split())]:
            if not ref:
                continue
            async with db.execute("SELECT id FROM outbox WHERE message_id = ? LIMIT 1",
                                  (ref,)) as cur:
                hit = await cur.fetchone()
            if hit:
                outbox_id = hit[0]
                break
        await db.execute(
            "UPDATE inbound_messages SET status = ?, duplicate_of = ?, last_error = ?, "
            "outbox_id = ?, processed_at = CASE WHEN ? = 'received' THEN NULL ELSE ? END "
            "WHERE id = ?",
            (status, duplicate_of, note, outbox_id, status, _ts(), row_id))

    async def get_inbound(self, inbound_id: str) -> dict | None:
        if not inbound_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM inbound_messages WHERE id = ?",
                                  (inbound_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def pending_inbound(self, limit: int = 200, exclude_provider: str = "") -> list[dict]:
        """Stored messages still to handle: new ones and retries, oldest first."""
        marks = ", ".join("?" for _ in INBOUND_PENDING)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM inbound_messages WHERE status IN ({marks}) "
                "AND attempts < ? AND provider != ? ORDER BY created_at, rowid LIMIT ?",
                (*INBOUND_PENDING, INBOUND_MAX_ATTEMPTS, exclude_provider, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def start_inbound_attempt(self, inbound_id: str) -> int:
        """Count an attempt before handling, so a crash mid-way still counts."""
        async with self._connect() as db:
            await db.execute("UPDATE inbound_messages SET attempts = attempts + 1 WHERE id = ?",
                             (inbound_id,))
            async with db.execute("SELECT attempts FROM inbound_messages WHERE id = ?",
                                  (inbound_id,)) as cur:
                row = await cur.fetchone()
            await db.commit()
            return int(row[0]) if row else 0

    async def finish_inbound(self, inbound_id: str, status: str, error: str = "") -> None:
        """processed, retry (try again next cycle) or failed (gave up)."""
        if status not in ("processed", "retry", "failed"):
            raise ValueError(f"unknown inbound status {status!r}")
        async with self._connect() as db:
            await db.execute(
                "UPDATE inbound_messages SET status = ?, last_error = ?, processed_at = ? "
                "WHERE id = ?", (status, (error or "")[:500], _ts(), inbound_id))
            await db.commit()

    _INBOUND_LINKS = frozenset({"prospect_id", "conversation_id", "outbox_id", "intent",
                                "kind", "auto_kind"})

    async def link_inbound(self, inbound_id: str, **fields) -> None:
        """What handling learned about a message: its contact, conversation,
        the email it answers, its intent and kind. Empty values are ignored."""
        fields = {k: v for k, v in fields.items() if k in self._INBOUND_LINKS and v}
        if not inbound_id or not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(f"UPDATE inbound_messages SET {sets} WHERE id = ?",
                             (*fields.values(), inbound_id))
            await db.commit()

    async def attach_inbound_to_conversation(
        self, inbound_id: str, *, prospect_id: str, campaign_id: str, message: Message,
        intent: str,
    ) -> tuple[Conversation, bool]:
        """Put a reply in its contact's open conversation (opening one if
        none is open) and link the stored message to it, in one transaction.
        Returns (conversation, attached_now). A message already attached by
        an earlier attempt is not appended again: attached_now is False."""
        now = _utcnow()
        async with aiosqlite.connect(
            self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
        ) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                done = ""
                if inbound_id:
                    async with db.execute(
                        "SELECT conversation_id FROM inbound_messages WHERE id = ?",
                        (inbound_id,)) as cur:
                        row = await cur.fetchone()
                    done = (row[0] if row else "") or ""
                if done:
                    # An earlier attempt got this far. If that conversation
                    # was deleted since, attach again as if for the first time.
                    async with db.execute("SELECT * FROM conversations WHERE id = ?",
                                          (done,)) as cur:
                        found = await cur.fetchone()
                    if found:
                        await db.execute(
                            "UPDATE conversations SET intent = ?, updated_at = ? WHERE id = ?",
                            (intent, now.isoformat(), done))
                        await db.execute("COMMIT")
                        d = dict(found)
                        d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                        d["intent"] = intent
                        return Conversation(**d), False
                async with db.execute(
                    "SELECT * FROM conversations WHERE prospect_id = ? AND status = 'open' "
                    "ORDER BY rowid LIMIT 1", (prospect_id,)) as cur:
                    found = await cur.fetchone()
                if found:
                    d = dict(found)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convo = Conversation(**d)
                    convo.thread.append(message)
                    convo.intent = intent
                    await db.execute(
                        "UPDATE conversations SET thread_json = ?, intent = ?, updated_at = ? "
                        "WHERE id = ?", (convo.thread_json(), intent, now.isoformat(), convo.id))
                else:
                    convo = Conversation(id=_new_id(), prospect_id=prospect_id,
                                         campaign_id=campaign_id, channel="email",
                                         thread=[message], intent=intent, status="open")
                    await db.execute(
                        """INSERT INTO conversations
                           (id, prospect_id, campaign_id, channel, thread_json,
                            intent, stage, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (convo.id, prospect_id, campaign_id, "email", convo.thread_json(),
                         intent, convo.stage, convo.status, convo.created_at.isoformat(),
                         convo.updated_at.isoformat()))
                if inbound_id:
                    await db.execute(
                        "UPDATE inbound_messages SET conversation_id = ?, prospect_id = ?, "
                        "intent = ? WHERE id = ?", (convo.id, prospect_id, intent, inbound_id))
                await db.execute("COMMIT")
                return convo, True
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def outbox_answering(self, inbound_id: str) -> dict | None:
        """The reply draft (in any state) queued to answer this message."""
        if not inbound_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE answers_inbound_id = ? ORDER BY created_at LIMIT 1",
                (inbound_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    # ── Inbox: local read state, snoozes, notes and reminders ──
    #
    # Local to Mercury. None of it touches the provider's read flags.

    async def set_conversations_read(self, conversation_ids: list[str], read: bool,
                                     now: str | None = None) -> int:
        ids = [c for c in dict.fromkeys(conversation_ids) if c]
        if not ids:
            return 0
        now = now or _ts()
        async with self._connect() as db:
            for cid in ids:
                await db.execute(
                    "INSERT INTO inbox_state (conversation_id, read_at, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                    "read_at = excluded.read_at, updated_at = excluded.updated_at",
                    (cid, now if read else None, now))
            await db.commit()
        return len(ids)

    async def snooze_conversation(self, conversation_id: str, until: str | None,
                                  now: str | None = None) -> None:
        """Snooze until a naive-UTC time, or clear it with None."""
        now = now or _ts()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO inbox_state (conversation_id, snoozed_until, snoozed_at, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "snoozed_until = excluded.snoozed_until, snoozed_at = excluded.snoozed_at, "
                "updated_at = excluded.updated_at",
                (conversation_id, until, now if until else None, now))
            await db.commit()

    async def get_inbox_state(self, conversation_id: str) -> dict:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM inbox_state WHERE conversation_id = ?",
                                  (conversation_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else {"conversation_id": conversation_id, "read_at": None,
                                      "snoozed_until": None, "snoozed_at": None,
                                      "updated_at": None}

    async def add_contact_note(self, prospect_id: str, body: str, created_by: str = "") -> dict:
        now = _ts()
        note = {"id": _new_id(), "prospect_id": prospect_id, "body": body,
                "created_by": created_by, "created_at": now, "updated_at": now,
                "deleted_at": None}
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO contact_notes (id, prospect_id, body, created_by, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (note["id"], prospect_id, body, created_by, now, now))
            await db.commit()
        return note

    async def get_contact_note(self, note_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM contact_notes WHERE id = ? "
                                  "AND deleted_at IS NULL", (note_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def update_contact_note(self, note_id: str, body: str) -> dict | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE contact_notes SET body = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL", (body, _ts(), note_id))
            await db.commit()
            if not cursor.rowcount:
                return None
        return await self.get_contact_note(note_id)

    async def delete_contact_note(self, note_id: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE contact_notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (_ts(), note_id))
            await db.commit()
            return cursor.rowcount > 0

    async def list_contact_notes(self, prospect_id: str) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM contact_notes WHERE prospect_id = ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC, id", (prospect_id,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def add_reminder(self, conversation_id: str, due_at: str, *, prospect_id: str = "",
                           note: str = "", created_by: str = "") -> dict:
        reminder = {"id": _new_id(), "conversation_id": conversation_id,
                    "prospect_id": prospect_id, "due_at": due_at, "note": note,
                    "created_by": created_by, "created_at": _ts(), "done_at": None,
                    "done_by": ""}
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO inbox_reminders (id, conversation_id, prospect_id, due_at, note, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(reminder[k] for k in ("id", "conversation_id", "prospect_id", "due_at",
                                            "note", "created_by", "created_at")))
            await db.commit()
        return reminder

    async def get_reminder(self, reminder_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM inbox_reminders WHERE id = ?",
                                  (reminder_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def complete_reminder(self, reminder_id: str, done_by: str = "") -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE inbox_reminders SET done_at = ?, done_by = ? "
                "WHERE id = ? AND done_at IS NULL", (_ts(), done_by, reminder_id))
            await db.commit()
            return cursor.rowcount > 0

    async def delete_reminder(self, reminder_id: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM inbox_reminders WHERE id = ?",
                                      (reminder_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_reminders(self, *, conversation_id: str = "", due_before: str | None = None,
                             include_done: bool = False, limit: int = 200) -> list[dict]:
        where, params = [], []
        if conversation_id:
            where.append("r.conversation_id = ?")
            params.append(conversation_id)
        if not include_done:
            where.append("r.done_at IS NULL")
        if due_before:
            where.append("r.due_at <= ?")
            params.append(due_before)
        sql = ("SELECT r.*, p.email AS prospect_email, p.first_name, p.last_name, "
               "p.company FROM inbox_reminders r "
               "LEFT JOIN conversations c ON c.id = r.conversation_id "
               "LEFT JOIN prospects p ON p.id = COALESCE(NULLIF(r.prospect_id, ''), c.prospect_id)")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.due_at, r.id LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at, mailbox, offer_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    campaign.instantly_campaign_id, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(), campaign.mailbox,
                    _norm(campaign.offer_key),
                ),
            )
            await db.commit()
        return campaign.id

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                campaigns = []
                for r in rows:
                    d = dict(r)
                    d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                    d["prospect_ids"] = json.loads(d.pop("prospect_ids_json") or "[]")
                    campaigns.append(Campaign(**d))
                return campaigns

    async def update_campaign(self, campaign_id: str, **kwargs):
        """Update whitelisted campaign columns in a single atomic statement."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CAMPAIGN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid campaign column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*kwargs.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversation(self, convo_id: str) -> Conversation | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE id = ?", (convo_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                return Conversation(**d)

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        """Update whitelisted conversation columns atomically (bumps updated_at)."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CONVERSATION_COLUMNS
        if invalid:
            raise ValueError(f"Invalid conversation column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*kwargs.values(), _utcnow().isoformat(), convo_id),
            )
            await db.commit()

    # ── Actions Log ──

    async def log_action(
        self,
        action_type: str,
        agent: str,
        details: dict | None = None,
        created_at: str | None = None,
    ):
        """Append to the actions log. ``created_at`` is for backfills/seeding
        only; normal callers let the DB stamp the time."""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json, created_at) "
                "VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (_new_id(), action_type, agent, json.dumps(details or {}), created_at),
            )
            await db.commit()

    # ── Command requests and audit (see mercury/control/audit.py) ──

    # A first attempt still 'running' after this long died mid-command; its
    # key may be used again. Finished records are kept for a week.
    COMMAND_STALE_MINUTES = 15
    COMMAND_KEEP_DAYS = 7

    async def begin_command(self, client: str, operator: str, key: str, action: str,
                            fingerprint: str) -> dict | None:
        """Claim a request key. None: claimed, run the command. Otherwise
        the existing record (running, or done with its result)."""
        now = _utcnow()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM command_requests WHERE created_at < ? OR "
                "(state = 'running' AND created_at < ?)",
                ((now - timedelta(days=self.COMMAND_KEEP_DAYS)).isoformat(),
                 (now - timedelta(minutes=self.COMMAND_STALE_MINUTES)).isoformat()),
            )
            cursor = await db.execute(
                "INSERT OR IGNORE INTO command_requests "
                "(client, operator, request_key, action, fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (client, operator, key, action, fingerprint, now.isoformat()),
            )
            if cursor.rowcount:
                await db.commit()
                return None
            async with db.execute(
                "SELECT * FROM command_requests WHERE client = ? AND operator = ? "
                "AND request_key = ?", (client, operator, key),
            ) as c:
                row = await c.fetchone()
            await db.commit()
            return dict(row)

    async def finish_command(self, client: str, operator: str, key: str,
                             outcome: str, result_json: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE command_requests SET state = 'done', outcome = ?, result_json = ?, "
                "finished_at = ? WHERE client = ? AND operator = ? AND request_key = ?",
                (outcome, result_json, _utcnow().isoformat(), client, operator, key),
            )
            await db.commit()

    async def release_command(self, client: str, operator: str, key: str) -> None:
        """Forget a claim whose command failed unexpectedly, so a retry runs."""
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM command_requests WHERE client = ? AND operator = ? "
                "AND request_key = ? AND state = 'running'", (client, operator, key),
            )
            await db.commit()

    _AUDIT_COLUMNS = ("client", "operator", "request_key", "batch_id", "action", "object_type",
                      "object_id", "revision_before", "revision_after", "outcome", "message",
                      "detail_json")

    async def add_audit(self, entries: list[dict]) -> None:
        if not entries:
            return
        now = _utcnow().isoformat()
        marks = ", ".join("?" for _ in self._AUDIT_COLUMNS)
        async with self._connect() as db:
            await db.executemany(
                f"INSERT INTO audit_log ({', '.join(self._AUDIT_COLUMNS)}, at) VALUES ({marks}, ?)",
                [(*("" if e.get(c) is None else e.get(c) for c in self._AUDIT_COLUMNS), now)
                 for e in entries],
            )
            await db.commit()

    async def get_audit(self, object_type: str = "", object_id: str = "",
                        limit: int = 100) -> list[dict]:
        """Newest first, optionally for one object."""
        where, params = [], []
        if object_type:
            where.append("object_type = ?")
            params.append(object_type)
        if object_id:
            where.append("object_id = ?")
            params.append(object_id)
        sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql + " ORDER BY id DESC LIMIT ?",
                                  (*params, max(1, min(int(limit), 1000)))) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return rows

    # ── Warm-up overlay (see mercury/warmup.py) ──

    _WARMUP_COLUMNS = frozenset({
        "status", "notes", "tasks_json", "paused_at", "pause_reason", "resumed_at",
    })

    async def add_warmup_inbox(self, email: str, *, status: str = "active",
                               notes: str = "") -> bool:
        """Insert an overlay row. Returns False when it already exists."""
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO warmup_inboxes (email, status, notes, tasks_json)
                   VALUES (?, ?, ?, '{}')""",
                (_norm(email), status, notes),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_warmup_inbox(self, email: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM warmup_inboxes WHERE email = ?", (_norm(email),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_warmup_inboxes(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM warmup_inboxes ORDER BY created_at ASC, email ASC"
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def update_warmup_inbox(self, email: str, **kwargs) -> bool:
        invalid = set(kwargs) - self._WARMUP_COLUMNS
        if invalid:
            raise ValueError(f"Invalid warm-up column(s): {sorted(invalid)}")
        if not kwargs:
            return False
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE warmup_inboxes SET {sets}, updated_at = ? WHERE email = ?",
                (*kwargs.values(), _utcnow().isoformat(), _norm(email)),
            )
            await db.commit()
            return cursor.rowcount > 0

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign (one indexed pass over conversations)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          COUNT(v.id) as reply_count,
                          SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) as interested_count,
                          SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) as objection_count,
                          SUM(CASE WHEN v.intent = 'not_interested' THEN 1 ELSE 0 END) as not_interested_count
                   FROM campaigns c
                   LEFT JOIN conversations v ON v.campaign_id = c.id
                   WHERE c.status IN ('active', 'completed')
                   GROUP BY c.id
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    for key in ("interested_count", "objection_count", "not_interested_count"):
                        d[key] = d[key] or 0
                    prospect_ids = json.loads(d.get("prospect_ids_json") or "[]")
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Usage Tracking ──

    async def get_usage_today(self) -> int:
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                "SELECT claude_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def increment_usage(self):
        today = date.today().isoformat()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO usage_log (id, date, claude_calls)
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET claude_calls = claude_calls + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Per-call usage accounting (usage_events) ──

    async def record_usage_event(
        self,
        *,
        agent: str = "",
        task: str = "",
        session_id: str = "",
        request_key: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        num_turns: int = 0,
        is_error: bool = False,
        source: str = "result_json",
        created_at: str | None = None,
    ) -> bool:
        """Insert one usage row (one model within one Claude call).

        Returns False when the row was skipped as a duplicate (request_key
        uniqueness makes transcript reconciliation idempotent).
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO usage_events
                   (id, created_at, agent, task, session_id, request_key, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, duration_ms, num_turns,
                    is_error, source)
                   VALUES (?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(), created_at, agent, task, session_id, request_key,
                    model, int(input_tokens or 0), int(output_tokens or 0),
                    int(cache_read_tokens or 0), int(cache_creation_tokens or 0),
                    float(cost_usd or 0.0), int(duration_ms or 0),
                    int(num_turns or 0), 1 if is_error else 0, source,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    _USAGE_SUM = (
        "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
        "SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(cost_usd) AS cost_usd"
    )

    @staticmethod
    def _usage_row_to_dict(row: aiosqlite.Row) -> dict:
        d = dict(row)
        for key, value in d.items():
            if value is None and key != "period":
                d[key] = 0
        if "cost_usd" in d:
            d["cost_usd"] = round(float(d["cost_usd"] or 0.0), 6)
        return d

    async def _usage_grouped(self, group_expr: str, alias: str, days: int) -> list[dict]:
        sql = (
            f"SELECT {group_expr} AS {alias}, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY {alias} ORDER BY cost_usd DESC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_by_agent(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent", days
        )

    async def usage_by_task(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN task = '' THEN 'other' ELSE task END", "task", days
        )

    async def usage_by_model(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN model = '' THEN 'unknown' ELSE model END", "model", days
        )

    async def usage_by_day(self, days: int = 30) -> list[dict]:
        sql = (
            f"SELECT date(created_at) AS day, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY day ORDER BY day ASC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_totals(self) -> dict:
        """Rollups for today / last 7 days / last 30 days (UTC)."""
        totals = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for label, where in (
                ("today", "date(created_at) = date('now')"),
                ("week", "created_at >= datetime('now', '-7 days')"),
                ("month", "created_at >= datetime('now', '-30 days')"),
            ):
                async with db.execute(
                    f"SELECT {self._USAGE_SUM} FROM usage_events WHERE {where}"
                ) as cursor:
                    row = await cursor.fetchone()
                    totals[label] = self._usage_row_to_dict(row) if row else {}
        return totals

    # ── Summary for Decision Making ──

    async def get_state_summary(self) -> dict:
        prospect_counts = await self.count_prospects_by_status()
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                """SELECT
                     SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)
                   FROM campaigns"""
            ) as cursor:
                row = await cursor.fetchone()
                draft_campaigns = row[0] or 0
                active_campaigns = row[1] or 0
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT claude_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                usage_row = await cursor.fetchone()
                usage_today = usage_row[0] if usage_row else 0

        return {
            "prospects": prospect_counts,
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "open_conversations": open_conversations,
            "usage_today": usage_today,
            # Companies discovered but not yet read. The heartbeat profiles
            # these every cycle — it is free, so it never competes with the
            # Claude budget the rest of the loop is rationing.
            "unprofiled": await self.count_companies_needing_profile(),
        }
