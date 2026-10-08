"""Handler — monitors replies and manages conversations.

Works against Instantly (legacy) or a native mail provider (Gmail/SMTP).
The native path also owns bounce handling (see mercury/bounces.py): each
bounce is bucketed by its DSN status code. A dead address is marked invalid
and its queued sends cancelled; a sender or reputation block pauses the
mailbox or domain; and past the kill-switch thresholds the global switch
flips, so a bad list can't torch the sending domain while nobody's watching.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from mercury import bounces as bounce_policy
from mercury.brain import Brain
from mercury.config import MercuryConfig, EnvConfig
from mercury.integrations.instantly import InstantlyClient
from mercury.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from mercury.integrations.mailboxes import (
    MailboxPool,
    build_rotation_pool,
    rotation_configured,
)
from mercury.models.conversation import Conversation, Message
from mercury.ooo import (
    classify_auto_reply,
    extract_return_date,
    is_receipt,
    operator_clock,
    resume_time,
)
from mercury.policy import ContactPolicy
from mercury.state import PAUSE_ENDING_STATUSES, StateManager
from mercury.personas import PersonaStore, voice_instructions

logger = logging.getLogger("mercury.handler")

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"
# The bounce-rate kill switch only engages after this many sends (a tiny
# sample lies). SENDER/BURNED bounces do not wait for it.
MIN_SENDS_FOR_KILL_SWITCH = bounce_policy.MIN_SENDS_FOR_RATE
DEFAULT_MAX_BOUNCE_RATE = 0.02

INTENT_LABELS = {
    "interested",
    "objection",
    "not_interested",
    "ooo",
    "wrong_person",
    "question",
    "unsubscribe",
    "escalate",
}

# Intents that get an automated reply. Everything else is closed,
# deferred, or escalated to a human.
AUTO_REPLY_INTENTS = {"interested", "objection", "question", "wrong_person"}

# Hard opt-out phrases. If any of these appear, we ALWAYS honor the
# opt-out regardless of what the LLM classifier says.
OPT_OUT_PATTERNS = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "take me off",
    "stop emailing",
    "stop contacting",
    "stop sending",
    "do not contact",
    "do not email",
    "don't contact",
    "don't email",
    "never contact",
    "delete my info",
    "delete my data",
)

# Spanish opt-outs. Word-boundary regexes so "baja" never fires on
# "trabaja", "rebaja" or "está de baja" (on leave).
OPT_OUT_REGEXES = tuple(re.compile(p) for p in (
    r"\b(dar|darme|darnos|dame|deme|denme|dénme|solicito la|solicito|quiero darme) (de )?baja\b",
    r"\bdesuscrib",
    r"\bcancel(a|e|en|ar) (la |mi )?suscripción\b",
    r"\bno (me|nos) (escrib(a|as|an)|contact(e|es|en)|env[ií](e|es|en)|mand(e|es|en))\b",
    r"\bno (me|nos) vuelv\w* a (escribir|contactar|enviar|mandar|molestar)\b",
    r"\bno (quiero|queremos|deseo|deseamos) (recibir|seguir recibiendo)\b",
    r"\bdej(a|e|en|ad|ar) de (escribir|enviar|mandar|contactar)",
    r"\b(elim[ií]n|qu[ií]t|borr|sa[cq]u)\w* ?(me |nos )?de (la |su |tu |esta |sus )?lista\b",
    r"\bborr\w* (mis|nuestros) datos\b",
))
# A reply that is nothing but the opt-out word we asked for.
OPT_OUT_EXACT = {"baja", "stop", "unsubscribe", "remove", "opt out", "no mas", "no más"}

# Everything from the first of these markers on is quoted text (our own
# email, or a forwarded thread), never the prospect's words. Our own footer
# contains the very words we ask people to reply with, so this cut is what
# keeps a normal reply from being read as an opt-out.
_QUOTE_MARKERS = (
    re.compile(r"^\s*>", re.M),
    re.compile(r"^\s*On .{0,200}?wrote:\s*$", re.M | re.S),
    re.compile(r"^\s*El .{0,200}?escribió:\s*$", re.M | re.S),
    re.compile(r"^\s*-{2,}\s*(Original Message|Mensaje original|Forwarded message|Mensaje reenviado)", re.M | re.I),
    re.compile(r"^\s*(From|De):\s.+\n\s*(Sent|Enviado el|Date|Fecha):\s", re.M | re.I),
)


def strip_quoted(text: str, extra_markers: tuple[str, ...] = ()) -> str:
    """Return only the prospect's own words from an inbound reply."""
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    lowered = text.lower()
    for marker in extra_markers:
        marker = (marker or "").strip().lower()
        if not marker:
            continue
        i = lowered.find(marker)
        if i != -1 and i < cut:
            cut = i
    return text[:cut].strip()

AUTO_REPLY_SUBJECTS = (
    "out of office", "out-of-office", "automatic reply", "auto-reply", "autoreply",
    "fuera de la oficina", "respuesta automática", "respuesta automatica",
    # Delivery and read acknowledgements are machines too.
    "read receipt", "delivery receipt", "return receipt",
    "we have received your", "we've received your", "acuse de recibo",
)
# Outlook-style receipts put the original subject after one of these.
AUTO_REPLY_PREFIXES = ("read:", "delivered:", "leído:", "leido:", "entregado:")


def _is_auto_reply(msg) -> bool:
    """Vacation and out-of-office responders, detected from headers first."""
    h = {k.lower(): (v or "").lower() for k, v in (getattr(msg, "headers", None) or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no"):
        return True
    if h.get("precedence") in ("bulk", "auto_reply", "junk"):
        return True
    if "x-autoreply" in h or "x-autorespond" in h:
        return True
    subject = (getattr(msg, "subject", "") or "").lower().strip()
    if any(m in subject for m in AUTO_REPLY_SUBJECTS) or subject.startswith(AUTO_REPLY_PREFIXES):
        return True
    # Read and delivery receipts are automatic too: not a person answering.
    return is_receipt(getattr(msg, "subject", "") or "", getattr(msg, "headers", None))


def received_at(date_header: str) -> datetime:
    """When an inbound message was sent, as naive UTC, from its Date header.

    Relative phrases in an out-of-office ("back tomorrow") are measured from
    this, and so is the check that a replayed old message must not move a
    pause. An unreadable header, or one dated in the future, falls back to now.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        when = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return now
    if when is None:
        return now
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return now if when > now + timedelta(days=1) else when


# Phrases that mean a human must take over. Never auto-reply to these.
ESCALATION_PATTERNS = (
    "lawyer",
    "attorney",
    "legal action",
    "lawsuit",
    "cease and desist",
    "harassment",
    "harassing",
    "report you",
    "reporting you",
    "spam complaint",
    "ftc",
    "gdpr",
    "can-spam",
    "blacklist",
)


class Handler:
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
        self.personas = PersonaStore(state)
        self.policy = ContactPolicy(state, config)
        self._response_generation_id = ""
        self.instantly = InstantlyClient(env.instantly_api_key)
        # Every configured mailbox is polled; see Sender for the pool.
        self.mailboxes: MailboxPool | None = build_rotation_pool(config, env)
        self.provider = (
            self.mailboxes.primary.provider if self.mailboxes
            else get_mail_provider(config, env)
        )
        self.skills = ""

    def _pool(self) -> MailboxPool | None:
        if self.mailboxes is not None:
            return self.mailboxes
        if rotation_configured(self.config) or self.provider is None:
            return None
        return MailboxPool.single(
            self.provider,
            getattr(self.config.channels.email, "max_daily_sends", 0) or 0,
            getattr(self.config.persona, "email", "") or "",
        )

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Check for new replies and handle them."""
        logger.info("Handler: Checking for replies...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("handler")

        if not self.config.channels.email.enabled:
            return

        if self.is_native:
            await self._run_native()
            return

        # 1. Fetch active campaigns
        active_campaigns = await self.state.get_campaigns_by_status("active")
        if not active_campaigns:
            logger.info("Handler: No active campaigns to monitor.")
            return

        total_handled = 0

        for campaign in active_campaigns:
            if not campaign.instantly_campaign_id:
                continue

            # 2. Get replies from Instantly
            try:
                replies = await self.instantly.get_replies(campaign.instantly_campaign_id)
            except Exception as e:
                logger.error(f"Handler: Failed to fetch replies for {campaign.name}: {e}")
                continue
            if not replies:
                continue

            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                try:
                    handled = await self._handle_reply(reply, campaign)
                    if handled:
                        total_handled += 1
                except Exception as e:
                    logger.error(f"Handler: Error processing reply: {e}")

        if total_handled:
            logger.info(f"Handler: Processed {total_handled} replies.")
        else:
            logger.info("Handler: No new replies.")

    async def _handle_reply(self, reply: dict, campaign) -> bool:
        """Process a single reply. Returns True if a new reply was handled."""
        lead_email = (reply.get("lead_email") or reply.get("from_email") or "").strip().lower()
        reply_text = (reply.get("body") or reply.get("text") or "").strip()
        reply_uuid = str(reply.get("uuid") or reply.get("id") or "")

        if not lead_email or not reply_text:
            return False

        # Dedup: skip if we already processed this reply
        if reply_uuid and await self.state.is_reply_processed(reply_uuid):
            logger.debug(f"Handler: Reply {reply_uuid} already processed. Skipping.")
            return False

        logger.info(f"Handler: Reply from {lead_email}")

        try:
            await self._process_reply(lead_email, reply_text, reply_uuid, campaign.id)
        finally:
            # ALWAYS mark processed — even on early exits (opt-out, OOO,
            # unknown prospect) — so the same reply is never re-handled
            # or double-replied on the next cycle.
            if reply_uuid:
                await self.state.mark_reply_processed(reply_uuid)
        return True

    async def _process_reply(
        self,
        lead_email: str,
        reply_text: str,
        reply_uuid: str,
        campaign_id: str = "",
        reply_meta: dict | None = None,
    ):
        """Classify, record, and respond to a single reply."""
        # Find the prospect by email (indexed lookup)
        prospect = await self.state.get_prospect_by_email(lead_email)
        if not prospect:
            logger.warning(f"Handler: No prospect found for {lead_email}")
            return

        # 1. Classify intent. Hard keyword checks run FIRST and override
        # the LLM — opt-outs and legal threats must never be missed.
        compliance = getattr(self.config, "compliance", None)
        own_words = strip_quoted(
            reply_text,
            (compliance.opt_out_line_en, compliance.opt_out_line_es) if compliance else (),
        )
        text_lower = own_words.lower()
        first_line = re.sub(r"[^\w ]", " ", text_lower.split("\n", 1)[0]).strip()
        if (
            any(p in text_lower for p in OPT_OUT_PATTERNS)
            or any(rx.search(text_lower) for rx in OPT_OUT_REGEXES)
            or first_line in OPT_OUT_EXACT
        ):
            intent = "unsubscribe"
        elif any(p in text_lower for p in ESCALATION_PATTERNS):
            intent = "escalate"
        else:
            intent = await self._classify_intent(reply_text, prospect)
        logger.info(f"Handler: Intent for {lead_email}: {intent}")

        if intent == "ooo":
            # Not a human answer: no conversation, no reply metrics. The
            # sequence is paused until they are back, not cancelled.
            meta = reply_meta or {}
            if self.is_native:
                await self._pause_for_ooo(
                    prospect, "", reply_text,
                    message_id=meta.get("message_id") or meta.get("dedup_key") or "",
                    received=received_at(meta.get("date", "")),
                )
            else:
                # Instantly owns sequencing there and Mercury has no pause
                # for it: say so rather than claim a pause that did not happen.
                logger.warning(
                    f"Handler: out-of-office from {lead_email}, but the Instantly "
                    "path cannot pause a remote sequence; it keeps sending."
                )
                await self.state.log_action(
                    action_type="sequence_pause_unavailable",
                    agent="handler",
                    details={"prospect_id": prospect.id, "prospect_email": lead_email,
                             "reason": "legacy Instantly sequences cannot be paused from Mercury"},
                )
            return

        # A human answered, so every queued sequence email for them is now
        # wrong to send. This used to run before classification, so an
        # auto-responder cancelled the whole sequence.
        # A temporary out-of-office pause is superseded: the person is here.
        await self.state.supersede_pause(prospect.id, "human reply")
        # A prospect a human already advanced (meeting booked / deal closed)
        # is never pulled back to 'replied' by a later message.
        if prospect.status not in ("meeting", "closed"):
            await self.state.update_prospect_status(prospect.id, "replied")
        try:
            cancelled = await self.state.cancel_pending_outbox_for_prospect(prospect.id)
            if cancelled:
                logger.info(
                    f"Handler: cancelled {cancelled} queued email(s) for "
                    f"{lead_email} (they replied)."
                )
        except Exception as e:
            logger.debug(f"Handler: outbox cancel failed: {e}")
        await self._hold_company_on_reply(prospect, intent)

        # The timestamped event the trends chart counts (replies / positive).
        try:
            await self.state.log_action(
                action_type="reply_received",
                agent="handler",
                details={"prospect_id": prospect.id, "prospect_email": lead_email,
                         "intent": intent, "campaign_id": campaign_id,
                         "mailbox": ((reply_meta or {}).get("mailbox") or "").lower()},
            )
        except Exception as e:
            logger.debug(f"Handler: reply_received log failed: {e}")

        # 2. Get or create conversation
        existing_convos = await self.state.get_conversations_by_status("open")
        convo = next(
            (c for c in existing_convos if c.prospect_id == prospect.id), None
        )

        if not convo:
            convo = Conversation(
                id="",
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                channel="email",
                thread=[
                    Message(sender="prospect", content=reply_text),
                ],
                intent=intent,
                status="open",
            )
            convo.id = await self.state.add_conversation(convo)
        else:
            convo.thread.append(Message(sender="prospect", content=reply_text))
            convo.intent = intent
            await self.state.update_conversation(
                convo.id,
                thread_json=convo.thread_json(),
                intent=intent,
            )

        # 2b. Advance conversation stage based on intent
        new_stage = self._determine_stage(intent, convo.stage, reply_text)
        if new_stage != convo.stage:
            logger.info(f"Handler: Stage for {lead_email}: {convo.stage} -> {new_stage}")
            await self.state.update_conversation(convo.id, stage=new_stage)
            convo.stage = new_stage

        # 3. Route based on intent
        if intent == "unsubscribe":
            # Honor opt-outs ALWAYS. No reply, no future contact.
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await self.state.update_prospect_status(prospect.id, "opted_out")
            # The opt-out is a rule about the address, kept apart from the
            # prospect row, so deleting or re-importing them never lifts it.
            await self.state.add_suppression(
                "email", prospect.email, source="opt_out", prospect_id=prospect.id,
                reason="asked not to be contacted", actor="handler",
            )
            await self.state.log_action(
                action_type="opt_out",
                agent="handler",
                details={"prospect_email": lead_email},
            )
            logger.info(f"Handler: {lead_email} opted out. Suppressed permanently. No reply sent.")
            return

        if intent == "escalate":
            # Angry / legal / compliance replies go to a human. Never auto-reply.
            await self.state.update_conversation(convo.id, status="needs_human")
            await self.state.log_action(
                action_type="escalation",
                agent="handler",
                details={
                    "prospect_email": lead_email,
                    "reply_preview": reply_text[:200],
                },
            )
            logger.warning(
                f"Handler: ESCALATED reply from {lead_email} — needs human review. No auto-reply sent."
            )
            return

        if intent == "not_interested":
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await self.state.update_prospect_status(prospect.id, "lost")
            logger.info(f"Handler: {lead_email} not interested. Closing. One no is enough.")
            return

        if intent == "ooo":
            logger.info(f"Handler: {lead_email} is OOO. Will follow up later.")
            return

        if intent not in AUTO_REPLY_INTENTS:
            logger.warning(f"Handler: No auto-reply policy for intent '{intent}'. Skipping reply.")
            return

        # For interested, objection, question, wrong_person — generate a reply
        response = await self._generate_response(intent, reply_text, prospect, convo)
        if not response:
            logger.warning(f"Handler: Could not generate response for {lead_email}")
            return

        # Send the response — natively through the outbox approval ladder,
        # or immediately via Instantly (legacy).
        if self.is_native:
            await self._queue_native_reply(
                response, prospect, convo, reply_meta or {}, intent
            )
        elif reply_uuid:
            result = await self.instantly.send_reply(reply_uuid, response)
            if result is not None:
                # Add our response to conversation
                convo.thread.append(Message(sender="mercury", content=response))
                await self.state.update_conversation(
                    convo.id,
                    thread_json=convo.thread_json(),
                )
                logger.info(f"Handler: Replied to {lead_email}")

                await self.state.log_action(
                    action_type="reply",
                    agent="handler",
                    details={
                        "prospect_email": lead_email,
                        "intent": intent,
                        "response_preview": response[:100],
                    },
                )

    # ── Native provider path (Gmail / SMTP) ──

    async def _run_native(self):
        """Poll every mailbox, split bounces from human replies, handle both."""
        pool = self._pool()
        if pool is None:
            logger.error("Handler: no mailbox could be built; not reading replies.")
            return
        mailboxes = pool.configured()
        if not mailboxes:
            logger.debug(
                f"Handler: provider '{self.provider.name if self.provider else '?'}' "
                "not configured yet."
            )
            return

        # One unreachable inbox must not hide the replies sitting in the rest.
        inbound = []
        for mailbox in mailboxes:
            try:
                fetched = await mailbox.provider.get_replies()
            except Exception as e:
                logger.error(
                    f"Handler: fetching replies from "
                    f"{mailbox.email or mailbox.provider.name} failed: {e}"
                )
                continue
            for msg in fetched:
                msg.mailbox = mailbox.email
            inbound.extend(fetched)

        handled = 0
        for msg in inbound:
            dedup_key = msg.provider_id or msg.message_id
            if not dedup_key or await self.state.is_reply_processed(dedup_key):
                continue
            keep_for_retry = False
            try:
                if msg.is_bounce:
                    await self._handle_bounce(msg)
                elif _is_auto_reply(msg):
                    # Not a human answer, and no Claude call is spent
                    # classifying it. A vacation notice pauses the sequence;
                    # every other automatic message is only recorded.
                    await self._handle_auto_reply(msg, dedup_key)
                elif msg.body.strip():
                    await self._process_reply(
                        msg.from_email,
                        msg.body.strip(),
                        reply_uuid="",
                        reply_meta={
                            "thread_ref": msg.thread_ref,
                            "message_id": msg.message_id,
                            "subject": msg.subject,
                            "mailbox": getattr(msg, "mailbox", ""),
                            "date": getattr(msg, "date", ""),
                            "dedup_key": dedup_key,
                        },
                    )
                handled += 1
            except Exception as e:
                logger.error(f"Handler: error processing {msg.from_email}: {e}")
                if msg.is_bounce:
                    # A bounce we could not account for is a reputation signal
                    # we never saw. Stop sending until someone looks; if even
                    # that cannot be saved, leave the bounce unread so the next
                    # heartbeat tries again instead of dropping it.
                    keep_for_retry = not await bounce_policy.engage_kill_switch(
                        self.state,
                        f"a bounce could not be processed ({e}) — review the "
                        "log before resuming",
                    )
            finally:
                if not keep_for_retry:
                    await self.state.mark_reply_processed(dedup_key)

        if handled:
            logger.info(f"Handler: processed {handled} inbound message(s).")
        else:
            logger.info("Handler: no new replies.")

    async def _handle_auto_reply(self, msg, dedup_key: str = ""):
        """An automatic message from a prospect's mailbox.

        It is kept as an audit record (action ``auto_reply``) and never opens
        a conversation or counts as a reply. Only a vacation notice pauses the
        sequence; read receipts and acknowledgements change nothing.
        """
        kind = classify_auto_reply(msg.subject, msg.body, getattr(msg, "headers", None))
        prospect = None
        outbox_item = await self.state.find_outbox_by_message_id(msg.in_reply_to)
        if outbox_item:
            prospect = await self.state.get_prospect(outbox_item["prospect_id"])
        if prospect is None:
            prospect = await self.state.get_prospect_by_email(msg.from_email)
        await self.state.log_action(
            action_type="auto_reply",
            agent="handler",
            details={"kind": kind, "from": msg.from_email,
                     "prospect_id": prospect.id if prospect else "",
                     "subject": (msg.subject or "")[:120],
                     "message_id": msg.message_id or dedup_key,
                     "mailbox": (getattr(msg, "mailbox", "") or "").lower()},
        )
        if kind != "ooo":
            logger.info(
                f"Handler: {kind} from {msg.from_email} recorded; sequence continues."
            )
            return
        if prospect is None:
            logger.info(f"Handler: out-of-office from {msg.from_email} matches no prospect.")
            return
        await self._pause_for_ooo(
            prospect, msg.subject, msg.body,
            message_id=msg.message_id or dedup_key,
            received=received_at(getattr(msg, "date", "")),
        )

    async def _pause_for_ooo(
        self, prospect, subject: str, body: str, *, message_id: str, received: datetime,
    ) -> dict | None:
        """Pause a prospect's cold sequence until they are back.

        A clear return date pauses until the first sending time after it. No
        date, an ambiguous one, an impossible one or one already past still
        pauses, but into the "return date needs review" state: nothing is
        guessed and nothing resumes until an operator decides.
        """
        if prospect.status in PAUSE_ENDING_STATUSES:
            logger.info(
                f"Handler: out-of-office from {prospect.email}, but they are already "
                f"'{prospect.status}'; nothing to pause."
            )
            return None
        compliance = getattr(self.config, "compliance", None)
        own = strip_quoted(
            body, (compliance.opt_out_line_en, compliance.opt_out_line_es) if compliance else ()
        )
        tz_name, quiet_end = operator_clock(self.config)
        found = extract_return_date(f"{subject}. {own}" if subject else own, received, tz_name)
        if found.ok:
            buffer = getattr(getattr(self.config.channels, "email", None), "ooo_resume_buffer_days", 0)
            state, resume = "paused", resume_time(found.date, tz_name, quiet_end, buffer)
        else:
            state, resume = "needs_review", None
        result = await self.state.record_ooo_pause(
            prospect.id,
            message_id=message_id,
            message_at=received,
            state=state,
            resume_at=resume,
            confidence=found.confidence if found.ok else 0.0,
            return_text=found.text,
            review_reason="" if found.ok else found.status,
        )
        if result["action"] == "ignored":
            logger.info(
                f"Handler: out-of-office from {prospect.email} left the pause as it was "
                f"({result['why']})."
            )
            return result
        pause = result["pause"]
        await self.state.log_action(
            action_type="sequence_paused",
            agent="handler",
            details={
                "prospect_id": prospect.id, "prospect_email": prospect.email,
                "change": result["action"], "state": pause["state"],
                "resume_at": pause["resume_at"] or "", "return_text": pause["return_text"],
                "confidence": pause["confidence"], "review_reason": pause["review_reason"],
                "message_id": message_id,
            },
        )
        if state == "paused":
            logger.info(
                f"Handler: {prospect.email} is out of office; sequence paused until "
                f"{pause['resume_at']} UTC ({found.text!r})."
            )
        else:
            logger.info(
                f"Handler: {prospect.email} is out of office; no usable return date "
                f"({found.status}). Sequence paused, return date needs review."
            )
        return result

    async def _hold_company_on_reply(self, prospect, intent: str):
        """A person at a company answered: hold cold mail to their colleagues
        so the company is not approached from two sides mid-conversation.
        Auto-replies and bounces never get here. Only native providers can
        hold a send, so the Instantly path records nothing it cannot enforce."""
        if not (self.is_native and self.policy.limits.pause_on_reply):
            return
        company_id = await self.policy.company_for(prospect)
        if not company_id:
            return
        hold, created = await self.state.hold_company(
            company_id, reason="reply", prospect_id=prospect.id,
            note=f"{prospect.email} replied ({intent})", actor="handler",
        )
        if created:
            logger.info(
                f"Handler: {prospect.email} replied; cold mail to their company is held "
                "until you resume it."
            )
            await self.state.log_action(
                action_type="company_hold", agent="handler",
                details={"company_id": company_id, "hold_id": hold["id"],
                         "prospect_email": prospect.email, "intent": intent},
            )

    async def _handle_bounce(self, msg):
        """A bounce is a data bug AND a reputation threat. Fix both."""
        headers = getattr(msg, "headers", None) or {}
        outbox_item = await self.state.find_outbox_by_message_id(msg.in_reply_to)
        if not outbox_item and headers.get("original_message_id"):
            outbox_item = await self.state.find_outbox_by_message_id(
                headers["original_message_id"])
        prospect = None
        if outbox_item:
            prospect = await self.state.get_prospect(outbox_item["prospect_id"])
        if prospect is None and headers.get("bounced_recipient"):
            prospect = await self.state.get_prospect_by_email(headers["bounced_recipient"])
        if prospect is None:
            # Last resort: any address in the DSN text that we have written to.
            own = {(self.config.persona.email or "").split("@")[-1].lower()}
            pool = self._pool()
            if pool is not None:
                own |= pool.domains()
            own.discard("")
            for cand in set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", msg.body or "")):
                cand = cand.lower().rstrip(".")
                if cand.split("@")[-1] in own:
                    continue
                if cand.split("@")[0] in ("mailer-daemon", "postmaster"):
                    continue
                prospect = await self.state.get_prospect_by_email(cand)
                if prospect is not None:
                    break

        # What the bounce says (RFC 3463 code) decides what it costs: a dead
        # address is one prospect, a blocked sender is the mailbox or domain.
        dsn_code, bucket = bounce_policy.classify_bounce(headers, msg.body or "")

        # Attribute the bounce to the mailbox that sent the email, so the
        # warm-up health gate can pause just that inbox. The DSN normally
        # comes back to the sending inbox, so that is the fallback.
        bounced_from = ""
        if outbox_item:
            bounced_from = outbox_item.get("mailbox") or ""
        if not bounced_from:
            bounced_from = getattr(msg, "mailbox", "") or ""
        bounced_from = bounced_from.strip().lower()

        # Count and log first, the address second: whatever happens to the
        # prospect, the reputation signal must not be lost.
        bounces = await self.state.increment_setting(BOUNCE_COUNT_KEY)
        await bounce_policy.record_bucket(self.state, bucket)
        total_sent = await self.state.count_outbox_sent()
        await self.state.log_action(
            action_type="bounce",
            agent="handler",
            details={"prospect": prospect.email if prospect else "unknown",
                     "prospect_id": prospect.id if prospect else "",
                     "mailbox": bounced_from,
                     "dsn_code": dsn_code, "bucket": bucket,
                     "bounces": bounces, "total_sent": total_sent},
        )

        note = await bounce_policy.apply_bucket(
            self.state, self._pool(), bucket=bucket, code=dsn_code, mailbox=bounced_from)
        logger.warning(
            f"Handler: bounce {dsn_code or 'with no status code'} → {bucket}"
            + (f" ({note})" if note else "") + "."
        )

        address_error = None
        if prospect and bucket in bounce_policy.ADDRESS_BUCKETS:
            try:
                await self.state.update_prospect_email(
                    prospect.id, prospect.email, "invalid"
                )
                cancelled = await self.state.cancel_pending_outbox_for_prospect(
                    prospect.id, reason="bounced"
                )
                await self.state.add_suppression(
                    "email", prospect.email, source="bounce", prospect_id=prospect.id,
                    reason="hard bounce", actor="handler",
                )
                await self.state.supersede_pause(prospect.id, "bounced")
                logger.warning(
                    f"Handler: BOUNCE for {prospect.email} — marked invalid, "
                    f"cancelled {cancelled} queued email(s)."
                )
            except Exception as e:
                # Still run the kill-switch check below, then fail the bounce:
                # a dead address left sendable is not something to log and move on from.
                address_error = e
                logger.error(f"Handler: could not mark {prospect.email} invalid: {e}")
        elif prospect:
            logger.warning(
                f"Handler: {bucket} bounce for {prospect.email} — the address is "
                "not the problem, so it stays as it is."
            )
        else:
            logger.warning(
                f"Handler: bounce received ({msg.subject[:60]}) but couldn't "
                "match it to a sent email."
            )

        max_rate = getattr(self.config.channels.email, "max_bounce_rate",
                           DEFAULT_MAX_BOUNCE_RATE)
        reason = bounce_policy.kill_switch_reason(
            await bounce_policy.load_counts(self.state), bounces, total_sent, max_rate)
        if reason:
            await bounce_policy.engage_kill_switch(self.state, reason)
        if address_error is not None:
            raise address_error

    async def _queue_native_reply(
        self, response: str, prospect, convo, reply_meta: dict, intent: str
    ):
        """Route Mercury's reply through the outbox (approval ladder applies)."""
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        subject = reply_meta.get("subject", "")
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        item_id = await self.state.add_outbox_item(
            prospect_id=prospect.id,
            conversation_id=convo.id,
            kind="reply",
            to_email=prospect.email,
            subject=subject or "Re: your note",
            body=response,
            generation_id=self._response_generation_id,
            send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="pending_review" if require_approval else "approved",
            provider=self.provider.name if self.provider else "",
            thread_ref=reply_meta.get("thread_ref", ""),
            in_reply_to=reply_meta.get("message_id", ""),
            # Answer from the inbox the message arrived in.
            mailbox=reply_meta.get("mailbox", ""),
        )
        if item_id:
            mode = "queued for your approval" if require_approval else "queued to send"
            logger.info(f"Handler: reply to {prospect.email} {mode} ({intent}).")
            await self.state.log_action(
                action_type="reply_queued",
                agent="handler",
                details={"prospect_email": prospect.email, "intent": intent,
                         "response_preview": response[:100]},
            )

    async def _classify_intent(self, reply_text: str, prospect) -> str:
        """Ask the brain to classify the reply's intent."""
        prompt = f"""Classify this email reply into exactly ONE category:
- "interested" — wants to learn more, open to a meeting, positive response
- "objection" — has concerns but hasn't said no (price, timing, competition)
- "not_interested" — a clear no, not now, or "we're all set"
- "unsubscribe" — asks to stop being contacted, remove from list, opt out
- "escalate" — angry, hostile, threatens legal action, or mentions spam/compliance
- "ooo" — out of office / auto-reply
- "wrong_person" — not the right contact, suggests someone else
- "question" — asking for more information before deciding

When in doubt between "not_interested" and "unsubscribe", choose "unsubscribe".
When in doubt between anything and "escalate", choose "escalate".

Reply from {prospect.full_name()} ({prospect.title} at {prospect.company}):
\"\"\"{reply_text}\"\"\"

Respond with ONLY the category label, nothing else."""

        result = await self.brain.think(
            prompt, session_id="mercury-handler",
            agent="handler", task="classify_intent",
        )
        if not result:
            # Classifier failed. Do NOT auto-reply blind — flag for a human.
            logger.warning("Handler: Intent classifier returned nothing. Escalating.")
            return "escalate"

        # Robust extraction: take the first recognized label anywhere in
        # the response (models sometimes add explanation despite instructions).
        cleaned = result.strip().strip('"').strip("'").lower()
        if cleaned in INTENT_LABELS:
            return cleaned
        for label in sorted(INTENT_LABELS, key=len, reverse=True):
            if label in cleaned:
                return label

        logger.warning(f"Handler: Unknown intent '{cleaned[:80]}'. Defaulting to 'question'.")
        return "question"

    def _determine_stage(self, intent: str, current_stage: str, reply_text: str) -> str:
        """Advance the conversation stage based on intent and context."""
        text_lower = reply_text.lower()

        # Terminal states
        if intent in ("not_interested", "unsubscribe"):
            return "closed_lost"

        if intent == "escalate":
            return current_stage  # Human decides what happens next

        # Stage advancement rules
        if intent == "interested":
            # Meeting/call signals from an interested prospect → closing
            if any(w in text_lower for w in ["let's meet", "schedule", "calendar", "book a call", "free on", "available"]):
                return "closing"
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                # Check if they're asking about specifics → presenting
                if any(w in text_lower for w in ["price", "cost", "how much", "pricing", "demo", "trial"]):
                    return "presenting"
                return "qualifying"
            if current_stage in ("qualifying", "presenting"):
                return "negotiating"
            if current_stage == "negotiating":
                return "closing"

        if intent == "objection":
            # Objections typically happen during presenting or negotiating
            if current_stage in ("initial_outreach", "engaged"):
                return "qualifying"
            # Stay in current stage during objection handling

        if intent == "question":
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                return "qualifying"

        return current_stage

    def _offer_brief(self) -> str:
        """The configured offer, rendered for a prompt.

        Returns "" when nothing is configured, so an untrained deployment
        carries on without an offer section rather than emitting empty
        labels the model would feel obliged to fill in.
        """
        offer = self.config.product.offer
        lines = []
        if offer.primary:
            lines.append(f"- What we sell: {offer.primary}")
        if offer.entry:
            lines.append(f"- Low-commitment first step: {offer.entry}")
        if offer.goal:
            lines.append(f"- Goal of this conversation: {offer.goal}")
        if offer.meeting_duration:
            lines.append(f"- Meeting length: {offer.meeting_duration}")
        if offer.meeting_owner:
            lines.append(f"- Who takes the meeting: {offer.meeting_owner}")

        if offer.booking_method == "calendar_link" and offer.booking_url:
            lines.append(
                f"- Booking link: {offer.booking_url} -- share it exactly as "
                f"written once they show interest. Never invent a link."
            )
        elif offer.booking_method == "suggest_times":
            lines.append("- Booking: suggest two or three concrete times, do not send a link")
        elif offer.booking_method == "ask_preference":
            lines.append("- Booking: ask which times suit them, do not send a link")

        if not lines:
            return ""
        return "\n\nTHE OFFER:\n" + "\n".join(lines)

    async def _generate_response(
        self, intent: str, reply_text: str, prospect, convo: Conversation,
        instruction: str = "", profile=None,
    ) -> str:
        """Generate an appropriate response based on intent."""
        self._response_generation_id = ""
        # Keep replies in the voice of the existing thread, even after a
        # different persona becomes the workspace default.
        async with self.state._connect() as db:
            cursor = await db.execute(
                "SELECT generation_id FROM outbox WHERE prospect_id = ? AND status = 'sent' "
                "AND (? = '' OR campaign_id = ? OR conversation_id = ?) "
                "ORDER BY sent_at DESC LIMIT 1",
                (prospect.id, convo.campaign_id, convo.campaign_id, convo.id),
            )
            row = await cursor.fetchone()
        profile = profile or await self.personas.for_generation(self.config, row[0] if row else "")
        # Build conversation history for context
        history = "\n".join(
            f"{'Mercury' if m.is_ours else prospect.full_name()}: {m.content}"
            for m in convo.thread[-6:]  # Last 6 messages for context
        )

        objection_context = ""
        if intent == "objection":
            # Check if we have a pre-configured response
            for trigger, response in self.config.product.objection_responses.items():
                if trigger.lower() in reply_text.lower():
                    objection_context = f"\nSuggested approach for this objection: {response}"
                    break

        prompt = self.brain.load_prompt("handler", stage=convo.stage)
        if not prompt:
            prompt = f"""You are {profile.get('signer') or self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {profile['tone']}
Product: {self.config.product.name} — {self.config.product.description}"""

        # Inject objection handling + sales methodology skills
        self.skills = self.brain.load_skills_for_agent("handler")
        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += voice_instructions(profile)

        # The offer is configuration, not knowledge, so it never arrives via
        # skills. Without it the reply agent knows to propose a call but not
        # what to propose or where to send them -- offer_strategy.md tells it
        # to use the booking_url, and nothing ever supplied one.
        prompt += self._offer_brief()

        prompt += f"""

Conversation so far:
{history}

The prospect's intent is: {intent}
{objection_context}

Write a reply that:"""

        if intent == "interested":
            prompt += """
- Acknowledges their interest warmly
- Suggests a specific next step (brief call or meeting)
- Keeps it short (under 80 words)
- Includes a clear CTA with flexibility on timing"""
        elif intent == "objection":
            prompt += """
- Addresses the concern directly and empathetically
- Provides evidence or a reframe
- Doesn't argue — redirect toward value
- Keeps it under 100 words"""
        elif intent == "question":
            prompt += """
- Answers their question clearly and concisely
- Ties the answer back to value for them
- Ends with a soft CTA
- Under 100 words"""
        elif intent == "wrong_person":
            prompt += """
- Thanks them politely
- Asks who the right person would be
- Makes it easy for them to refer (one-line ask)
- Under 50 words"""

        prompt += "\n\nWrite ONLY the email body. No subject line, no greeting label, no signature block, no markdown."
        if instruction:
            prompt += f"\n\nReviewer instruction (within the shared reply rules): {instruction}"

        response = await self.brain.think(
            prompt, session_id="mercury-handler",
            agent="handler", task="generate_response",
        )
        if not response:
            return ""
        response = response.strip()
        # Guard against the model returning meta-text instead of an email
        if response.lower().startswith(("i can't", "i cannot", "as an ai", "sorry,")):
            logger.warning("Handler: Model returned meta-text instead of an email. Discarding.")
            return ""
        self._response_generation_id = await self.personas.record(
            profile, self.config, prompt, {"body": response}, "generate_response",
            instruction=instruction, json_mode=False,
        )
        return response
