"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import logging
import math
import random
import time
import re
from datetime import date, datetime, timedelta, timezone

from mercury.brain import Brain
from mercury.config import MercuryConfig, EnvConfig
from mercury.gate import pre_send_check
from mercury.integrations.instantly import InstantlyClient
from mercury.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from mercury.integrations.mailboxes import (
    MailboxPool,
    build_rotation_pool,
    local_today,
    rotation_configured,
)
from mercury.state import StateManager

logger = logging.getLogger("mercury.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
# Due approved rows examined per cycle (ordering + pinning happen in Python).
DUE_SCAN_LIMIT = 2000
SEND_JITTER_SECONDS = (4, 15)

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}


def _smtp_error_is_transient(err: str) -> bool:
    """Connection trouble and 4xx deferrals deserve a retry; 5xx verdicts do not."""
    e = (err or "").lower()
    if any(m in e for m in ("timed out", "timeout", "connect", "disconnected",
                            "connection", "temporarily", "try again", "greylist",
                            "too many", "rate limit", "throttl")):
        return True
    import re
    m = re.search(r"\b([245]\d\d)\b", e)
    return bool(m) and m.group(1).startswith("4")


def _retry_attempt(prev_error: str) -> int:
    import re
    m = re.match(r"retry (\d)/3", prev_error or "")
    return int(m.group(1)) + 1 if m else 1


def spread_budget(remaining: int, now_local: datetime, quiet_start: str,
                  interval_minutes: int) -> int:
    """Sends for this cycle so that ``remaining`` lasts until quiet hours:
    ceil(remaining / cycles left before quiet_start). The rounding front-loads
    the day; whatever MAX_SENDS_PER_CYCLE cuts off in the last cycles simply
    stays queued for the next morning.
    """
    if remaining <= 0:
        return 0
    try:
        hh, mm = (int(x) for x in (quiet_start or "22:00").split(":")[:2])
    except ValueError:
        hh, mm = 22, 0
    stop = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= now_local:
        stop += timedelta(days=1)
    minutes_left = (stop - now_local).total_seconds() / 60
    cycles_left = max(1, math.ceil(minutes_left / max(1, int(interval_minutes or 15))))
    return math.ceil(remaining / cycles_left)

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: MercuryConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)
        # Several mailboxes (channels.email.mailboxes) or None. Without a
        # rotation pool the drain wraps self.provider in a one-mailbox pool,
        # so tests and single-mailbox setups run the same code path.
        self.mailboxes: MailboxPool | None = build_rotation_pool(config, env)
        self.provider = (
            self.mailboxes.primary.provider if self.mailboxes
            else get_mail_provider(config, env)
        )
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        self._last_drain_at: float | None = None  # monotonic, for spread_sends

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": step.body.strip(),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `mercury sending resume` once the cause is fixed."
            )
            return

        for campaign in await self.state.get_campaigns_by_status("draft"):
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        if getattr(self.config.channels.email, "auto_approve_followups", False):
            await self._promote_followups()

        await self._drain_due()

    async def _promote_followups(self):
        """auto_approve_followups: a follow-up rides on its approved opener.
        Repeats so a whole chain is promoted in one cycle (bounded, since
        each pass only promotes steps whose previous step qualifies)."""
        promoted = 0
        for _ in range(10):
            n = await self.state.approve_ready_followups()
            if not n:
                break
            promoted += n
        if promoted:
            logger.info(f"Sender: auto-approved {promoted} follow-up(s) of approved openers.")

    def _pool(self) -> MailboxPool | None:
        if self.mailboxes is not None:
            return self.mailboxes
        if rotation_configured(self.config):
            return None  # mailboxes listed but the pool failed to build: hold
        if self.provider is None:
            return None
        return MailboxPool.single(
            self.provider,
            self.config.channels.email.max_daily_sends,
            getattr(self.config.persona, "email", "") or "",
        )

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            "first_name": prospect.first_name,
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    # Stopword counts decide the footer language; drafts are ES or EN.
    _ES_MARKERS = (" el ", " la ", " de ", " que ", " para ", " los ", " las ",
                   " una ", " con ", " por ", " tu ", " su ", " está ", " cómo ")
    _EN_MARKERS = (" the ", " and ", " you ", " your ", " with ", " for ",
                   " that ", " on ", " is ", " are ", " to ", " of ")

    def _with_legal_footer(self, body: str) -> str:
        """Append company + postal address + opt-out line (CAN-SPAM) in the
        language of the email. Kept out of the draft so the writer never
        rewrites or drops it."""
        c = self.config.compliance
        padded = f" {body.lower()} "
        es = sum(padded.count(m) for m in self._ES_MARKERS)
        en = sum(padded.count(m) for m in self._EN_MARKERS)
        opt_out = c.opt_out_line_es if es > en else c.opt_out_line_en
        company = self.config.persona.company
        return f"{body.rstrip()}\n\n{company} · {c.postal_address.strip()}\n{opt_out}"

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        skipped = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen_emails: set[str] = set()

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            seen_emails.add(email)

            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=self._render(step.body, prospect),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                    generation_id=step.generation_id,
                    mailbox=campaign.mailbox,
                )
                if item_id:
                    staged += 1
            await self.state.update_prospect_status(prospect.id, "queued")

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    async def _drain_due(self):
        """Send due, approved outbox items, rotating over the mailbox pool."""
        pool = self._pool()
        if pool is None:
            logger.error(
                "Sender: channels.email.mailboxes is set but no mailbox could be "
                "built. Outbox is holding; check the mailbox config."
            )
            return
        if not pool.configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name if self.provider else '?'}' "
                "is not configured yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        compliance = getattr(self.config, "compliance", None)
        if compliance is None or not compliance.postal_address.strip():
            logger.error(
                "Sender: compliance hold — compliance.postal_address is empty in "
                "mercury.local.yaml. CAN-SPAM requires a physical postal address and "
                "an opt-out line in every commercial email; the outbox is holding "
                "until it is set."
            )
            return

        email_cfg = self.config.channels.email
        max_daily = email_cfg.max_daily_sends
        today = local_today(self.config)
        sent_today = await self.state.count_outbox_sent_today()
        by_mailbox = await self.state.count_outbox_sent_today_by_mailbox()
        # Warm-up health gates (bounce spike / manual pause) lower the caps
        # the pool hands out below. Replies are not cap-bound, so a paused
        # mailbox still answers people who wrote back.
        try:
            from mercury.warmup import apply_health

            await apply_health(self.state, pool)
            for email, gate in pool.gates.items():
                logger.info(f"Sender: warm-up gate on {email or 'the mailbox'}: {gate}.")
        except Exception as e:
            logger.warning(f"Sender: warm-up health check failed ({e}); "
                           "using the configured caps.")
        remaining = pool.remaining(by_mailbox, today)
        global_left = max(0, max_daily - sent_today)
        # Cold mail (openers, follow-ups) is bound by the mailboxes' caps and
        # paced; a reply to a person only by max_daily_sends.
        cold_left = min(global_left, sum(max(0, n) for n in remaining.values()))
        cold_budget = min(MAX_SENDS_PER_CYCLE, cold_left)
        if cold_budget > 0 and getattr(email_cfg, "spread_sends", False):
            cold_budget = min(cold_budget, self._spread(cold_left))
        budget = min(MAX_SENDS_PER_CYCLE, global_left)
        if budget <= 0:
            capacity = min(max_daily, pool.capacity_on(today))
            logger.info(f"Sender: daily send cap reached ({sent_today}/{capacity}).")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # The whole due set, not a budget-sized slice: rows pinned to a
        # mailbox at its cap are skipped below, and a slice of the oldest
        # rows could be nothing but those while other mailboxes sat idle.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=DUE_SCAN_LIMIT
        )
        if not due:
            return
        # Replies first (a person is waiting), then follow-ups, then new
        # first touches. Follow-ups carry 55-65% of replies and lose their
        # "3 days later" meaning when they wait; ordered by send_at alone, a
        # backlog of older first emails starved every step 2.
        def _rank(i):
            if i.get("kind") == "reply":
                return 0
            return 1 if int(i.get("step") or 1) > 1 else 2
        due.sort(key=lambda i: (_rank(i), i.get("send_at") or ""))

        allow_risky = getattr(email_cfg, "send_to_risky", False)
        sent = cold_sent = 0
        sent_this_cycle: dict[str, int] = {}
        for item in due:
            if sent >= budget:
                break
            if item["kind"] != "reply" and cold_sent >= cold_budget:
                if cold_budget == 0 and not sent:
                    logger.info("Sender: mailbox caps reached for today; only replies may go.")
                break  # replies sort first, so nothing sendable is left

            prospect = await self.state.get_prospect(item["prospect_id"])
            if prospect is None:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error="prospect missing"
                )
                continue

            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if item["kind"] == "sequence" and prospect.status in STOP_STATUSES:
                await self.state.update_outbox_item(
                    item["id"], status="cancelled",
                    error=f"prospect status '{prospect.status}'",
                )
                continue

            # Sequence order: step N never leaves before step N-1 was sent.
            # If the earlier step is dead (rejected, cancelled, failed) this
            # one dies with it, so it cannot sit 'approved' forever and crowd
            # the due queue. If the earlier step is merely not sent yet, hold.
            prev = None
            if item["kind"] == "sequence" and item["step"] > 1:
                prev = await self.state.get_previous_outbox_step(
                    item["campaign_id"], item["prospect_id"], item["step"]
                )
                prev_status = prev["status"] if prev else "missing"
                if prev_status in ("rejected", "cancelled", "failed"):
                    await self.state.update_outbox_item(
                        item["id"], status="cancelled",
                        error=f"previous step is '{prev_status}'",
                    )
                    logger.info(
                        f"Sender: cancelled step {item['step']} to {item['to_email']}: "
                        f"previous step is '{prev_status}'."
                    )
                    continue
                if prev_status != "sent":
                    logger.info(
                        f"Sender: holding step {item['step']} to {item['to_email']}: "
                        f"previous step is '{prev_status}', not sent."
                    )
                    continue
                # "3 days later" means 3 days after the previous email really
                # went out. send_at was fixed when the sequence was staged, so
                # an opener approved a week late would otherwise drag its
                # follow-ups out right behind it.
                earliest = await self._followup_earliest(item, prev)
                if earliest and earliest > now:
                    await self.state.update_outbox_item(item["id"], send_at=earliest)
                    logger.info(
                        f"Sender: step {item['step']} to {item['to_email']} rescheduled "
                        f"to {earliest[:16]} (its delay after the previous step)."
                    )
                    continue

            mailbox, _verdict = self._mailbox_for(item, prev, pool, remaining,
                                                  sent_this_cycle, today)
            if mailbox is None:
                continue

            gate = pre_send_check(
                item["to_email"], item["subject"], item["body"],
                prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            )
            if not gate:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error="gate: " + "; ".join(gate.reasons)[:300],
                )
                logger.warning(
                    f"Sender: gate blocked email to {item['to_email']}: "
                    f"{gate.reasons}"
                )
                continue

            # The reviewed text and its generation must stay together during
            # the provider call. A stale due scan cannot send a replaced draft.
            if not await self.state.claim_outbox_item(item, mailbox.email):
                continue
            body_out = item["body"]
            if item["kind"] != "reply":
                body_out = self._with_legal_footer(body_out)
            result = await mailbox.provider.send_email(
                item["to_email"], item["subject"], body_out,
                thread_ref=item.get("thread_ref", ""),
                in_reply_to=item.get("in_reply_to", ""),
            )
            if not result.ok:
                err = result.error or ""
                attempt = _retry_attempt(item.get("error") or "")
                if attempt <= 3 and _smtp_error_is_transient(err):
                    # A connection hiccup or a 4xx deferral is not a verdict
                    # on the address: keep the row approved and try later.
                    retry_at = datetime.fromtimestamp(
                        datetime.now(timezone.utc).timestamp() + 1800 * attempt,
                        tz=timezone.utc,
                    ).replace(tzinfo=None).isoformat()
                    await self.state.update_outbox_item(
                        item["id"], status="approved", send_at=retry_at,
                        error=f"retry {attempt}/3: {err[:250]}",
                    )
                    logger.warning(
                        f"Sender: transient SMTP error to {item['to_email']} "
                        f"via {mailbox.email or mailbox.provider.name} "
                        f"(attempt {attempt}/3), retrying at {retry_at[:16]}: {err[:120]}"
                    )
                else:
                    await self.state.update_outbox_item(
                        item["id"], status="failed", error=err[:300]
                    )
                    logger.error(
                        f"Sender: giving up on {item['to_email']} "
                        f"(prospect {item['prospect_id']}): {err[:160]}"
                    )
                continue

            sent += 1
            if item["kind"] != "reply":
                cold_sent += 1
            remaining[mailbox.email] = remaining.get(mailbox.email, 0) - 1
            sent_this_cycle[mailbox.email] = sent_this_cycle.get(mailbox.email, 0) + 1
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body_out,
                message_id=result.message_id, thread_ref=result.thread_ref,
                mailbox=mailbox.email,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from mercury.models.conversation import Message
                        convo.thread.append(
                            Message(sender="mercury", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details={
                    "to": item["to_email"], "step": item["step"],
                    "kind": item["kind"], "provider": mailbox.provider.name,
                    "mailbox": mailbox.email,
                },
            )
            via = mailbox.email or mailbox.provider.name
            logger.info(f"Sender: sent step {item['step']} to {item['to_email']} via {via}.")
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if sent:
            capacity = min(max_daily, pool.capacity_on(today))
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{capacity} today).")

    def _mailbox_for(self, item, prev, pool, remaining, sent_this_cycle, today):
        """(mailbox, verdict): verdict is "send" or "hold" (try a later cycle).

        A thread keeps its mailbox: a reply answers from the inbox the message
        arrived in, and a follow-up comes from the address its opener used.
        Only a new thread (step 1) rotates, and only onto mailboxes that take
        new threads. A thread whose mailbox was removed from the config is
        held, never moved to another address.
        """
        pinned_value = None
        if item.get("mailbox"):
            pinned_value = item["mailbox"]
        elif item["kind"] == "reply":
            pinned_value = ""  # queued before tracking: the legacy mailbox
        elif prev is not None:
            pinned_value = prev.get("mailbox") or ""

        if pinned_value is None:
            mailbox = pool.pick(remaining, sent_this_cycle, today)
            return mailbox, ("send" if mailbox else "hold")

        mailbox = pool.resolve(pinned_value)
        if mailbox is None:
            # Never re-route: the old inbox is not read, so a reply or an
            # opt-out sent there would go unseen. Holding is reversible (put
            # the mailbox back, or reject the email in the dashboard).
            logger.warning(
                f"Sender: holding {item['kind']} to {item['to_email']}: its thread's "
                f"mailbox {pinned_value} is not in channels.email.mailboxes. Add it "
                "back (enabled: false is enough) or reject the email."
            )
            return None, "hold"
        if not mailbox.provider.is_configured():
            logger.warning(
                f"Sender: holding email to {item['to_email']}: its thread's mailbox "
                f"{mailbox.email or mailbox.provider.name} has no credentials."
            )
            return None, "hold"
        # A person who wrote back is not cold volume: a reply may go past its
        # mailbox's warm-up cap (the global max_daily_sends still applies).
        if item["kind"] != "reply" and remaining.get(mailbox.email, 0) <= 0:
            logger.debug(
                f"Sender: holding email to {item['to_email']}: "
                f"{mailbox.email or 'mailbox'} is at today's cap."
            )
            return None, "hold"
        return mailbox, "send"

    async def _followup_earliest(self, item, prev) -> str | None:
        """Earliest send time for a follow-up: the previous step's actual
        sent_at plus this step's delay_days. None when unknown."""
        sent_at = (prev or {}).get("sent_at")
        if not sent_at:
            return None
        try:
            prev_sent = datetime.fromisoformat(str(sent_at).replace(" ", "T"))
        except ValueError:
            return None
        delay = await self.state.get_sequence_delay_days(item.get("campaign_id") or "",
                                                         int(item["step"]))
        if delay is None:
            # No campaign row to read the delay from. send_at values get
            # rewritten (retries, this rescheduling), so a gap derived from
            # them drifts; keep the staged send_at instead.
            return None
        return (prev_sent + timedelta(days=delay)).isoformat()

    def _spread(self, left_today: int) -> int:
        usage = getattr(self.config, "usage", None)
        quiet = getattr(usage, "quiet_hours", None)
        tz_name = getattr(quiet, "timezone", "UTC") or "UTC"
        try:
            import pytz

            now_local = datetime.now(pytz.timezone(tz_name)).replace(tzinfo=None)
        except Exception:
            now_local = datetime.utcnow()
        # A cycle is its own run time plus the sleep, so the configured
        # interval overstates how many cycles are left. Use the gap actually
        # observed since the previous drain (bounded to 1-3 intervals).
        interval = max(1, int(getattr(usage, "heartbeat_interval_minutes", 15) or 15))
        observed = interval
        mono = time.monotonic()
        if self._last_drain_at is not None:
            gap = (mono - self._last_drain_at) / 60
            observed = min(max(gap, interval), 3 * interval)
        self._last_drain_at = mono
        return spread_budget(left_today, now_local, getattr(quiet, "start", "22:00"),
                             math.ceil(observed))
