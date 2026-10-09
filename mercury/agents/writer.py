"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.

With ``offers:`` configured, each prospect is routed to one offer first
(mercury/offers.py) and campaigns are grouped so one campaign carries one
offer. The prompt then gets that offer's brief and nothing about any other
offer. A brief with a ``content.summary`` is authoritative: it replaces the
product description and the trainer's product_knowledge skill, which
describe the whole business and could name offers this prospect was not
routed to.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mercury import experiments
from mercury.brain import AGENT_SKILLS, Brain
from mercury.config import MercuryConfig
from mercury.integrations.mail_provider import NATIVE_PROVIDERS
from mercury.draft_rules import (
    REVIEW_SIGNAL_CODES,
    apply_short_name,
    count_words,
    draft_flags,
    strip_generic_greeting,
    strip_review_claims,
    word_limit,
    word_limits,
)
from mercury.greeting import (
    NAMED,
    ROUTING,
    GreetingPlan,
    business_names,
    plan_greeting,
    prompt_lines as greeting_lines,
    sequence_lines,
)
from mercury.greeting import fact_line as greeting_fact_line
from mercury.models.campaign import Campaign, EmailStep
from mercury.offers import (
    ConfirmedPain,
    OfferBrief,
    RouteDecision,
    blocked_terms,
    build_brief,
    case_study_hits,
    decision_for_key,
    has_case_studies,
    offer_problems,
    route_prospect,
    routing_enabled,
)
from mercury.pains import (
    PainSelection,
    find_rejected_pain_hits,
    never_use_block,
    pain_block,
    select_pain_for_prospect,
)
from mercury.state import StateManager
from mercury.personas import PersonaStore, voice_instructions
from mercury.registry.base import name_variants, short_business_name
from mercury.voices import MailboxVoices

logger = logging.getLogger("mercury.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 20
LEGACY_BATCH_CAP = 50

# Skills an authoritative offer brief replaces. product_knowledge is the
# trainer's description of everything the business sells.
BRIEF_REPLACES_SKILLS = ("product_knowledge",)


@dataclass
class PainPlan:
    """The pain decision behind one prompt (one email, or one shared sequence).

    ``pain`` is the one confirmed pain the email may raise (None: none fits,
    and the prompt says so). ``never_use`` is the rejected pains the model is
    shown; ``rejected`` is all of them, which the Writer and the send gate
    check a draft against, and ``confirmed`` is the vocabulary a draft may
    share with them."""
    pain: ConfirmedPain | None = None
    never_use: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    confirmed: list = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.pain.code if self.pain is not None else ""


@dataclass
class DraftContext:
    """What a draft is checked against once the model has answered."""
    step: int
    limit: int
    decision: RouteDecision
    brief: OfferBrief | None
    plan: PainPlan
    greeting: GreetingPlan
    full_name: str = ""
    short_name: str = ""
    variants: list = field(default_factory=list)
    business_names: list = field(default_factory=list)
    who: str = ""


def _selection(plan: PainPlan) -> PainSelection:
    """The plan's pain in the shape pains.pain_block renders."""
    pain = plan.pain
    return PainSelection(pain=None if pain is None else {
        "code": pain.code, "owner_words": pain.words, "scene": pain.scene, "cost": pain.cost,
    }, reason="")


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: MercuryConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        # Loaded here too so drafts requested outside run() (the review
        # desk's regenerate, one-off scripts) get the product knowledge.
        self.skills = self.brain.load_skills_for_agent("writer")
        self.config = config
        self.env = env
        self.personas = PersonaStore(state)
        self.voices = MailboxVoices(state, config, env)
        # The confirmed-pain hook: an async callable
        # (prospect, offer_key, step) -> ConfirmedPain | None. By default it
        # selects the one confirmed pain from the governed pain library that
        # fits the prospect and offer. Set to None, every prompt says no pain
        # was supplied; the Writer never makes one up either way.
        self.pain_source = self._confirmed_pain

    def _base_prompt(self, profile: dict, brief: OfferBrief | None = None,
                     plan: PainPlan | None = None) -> str:
        return "".join(text for _key, _label, text in self._base_sections(profile, brief, plan))

    def _sign_off_rule(self, profile: dict) -> str:
        name = profile.get("signer") or self.config.persona.name
        return (f"Sign off with this name and no other: {name}. "
                "Never sign with a persona, team or company name instead.")

    def _base_sections(self, profile: dict, brief: OfferBrief | None = None,
                       plan: PainPlan | None = None) -> list[tuple[str, str, str]]:
        """Template, knowledge, offer brief, pain and voice, as labelled pieces that join into the base prompt."""
        product = self.config.product
        description, pricing = product.description, product.pricing
        benefits = "\n".join(f"- {b}" for b in product.key_benefits)
        if brief is not None and brief.authoritative:
            # The brief is the only offer description: the product block
            # (which may describe every offer) points at it instead.
            description = "See the OFFER BRIEF below. It is the only offer you may describe."
            benefits = "- Only the approved claims in the OFFER BRIEF below."
            pricing = "Only what the OFFER BRIEF says, if anything."
        template = self.brain.load_prompt(
            "writer",
            product_name=product.name,
            product_description=description,
            product_benefits=benefits,
            product_pricing=pricing,
            persona_name=profile.get("signer") or self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=profile["tone"],
            **{f"word_limit_{n}": limit for n, limit in word_limits(self.config).items()},
        ) or (
            f"You are {profile.get('signer') or self.config.persona.name}, {self.config.persona.role} "
            f"at {self.config.persona.company}.\nProduct: {product.name}\n"
            f"Description: {description}\nPricing: {pricing}\n"
            "Benefits:\n" + benefits
        )
        sections = [("template", "Writer template", template)]
        if brief is not None and brief.authoritative:
            self.skills = self.brain.load_skills(
                [s for s in AGENT_SKILLS["writer"] if s not in BRIEF_REPLACES_SKILLS])
        else:
            self.skills = self.brain.load_skills_for_agent("writer")
        if self.skills:
            sections.append(("knowledge", "Knowledge", "\n\n" + self.skills))
        if brief is not None:
            sections.append(("offer", "Offer brief", brief.render()))
        if plan is not None:
            # With a brief the pain rides inside it. Without offers it is its
            # own block, once the pain library is in use (an install with no
            # confirmed pain keeps the prompt it always had).
            if brief is None and (plan.pain is not None or plan.confirmed):
                sections.append(("pain", "Confirmed pain", "\n\n" + pain_block(_selection(plan))))
            never = never_use_block(plan.never_use)
            if never:
                sections.append(("never_use", "Rejected pains", "\n\n" + never))
        sections.append(("persona", "Writing persona", voice_instructions(profile)))
        return sections

    # ── Offers ──

    async def _route(self, prospect) -> RouteDecision:
        return await route_prospect(self.state, self.config, prospect)

    async def _confirmed_pain(self, prospect, offer_key: str, step: int) -> ConfirmedPain | None:
        """The default pain source: the one confirmed pain from the pain
        library that fits this prospect and offer (mercury/pains.py), or
        None. Every step of a thread gets the same pick, so a follow-up
        never raises a different problem than its opener."""
        found = await select_pain_for_prospect(
            self.state, prospect, config=self.config, offer_key=offer_key)
        pain = found.pain
        if pain is None:
            return None
        return ConfirmedPain(code=pain["code"], words=(pain["owner_words"] or pain["label"]).strip(),
                             scene=pain["scene"], cost=pain["cost"])

    async def _pain(self, prospect, decision: RouteDecision, step: int) -> ConfirmedPain | None:
        if self.pain_source is None:
            return None
        try:
            return await self.pain_source(prospect, decision.key, step)
        except Exception as e:
            logger.warning(f"Writer: pain lookup failed for {getattr(prospect, 'email', '')}: {e}")
            return None

    async def _pain_plan(self, prospects: list, decision: RouteDecision, steps: list[int]) -> PainPlan:
        """The pain for one prompt. A shared sequence template has no single
        reader, so it carries a pain only when every prospect in the group
        was given the same one."""
        pains = [await self._pain(p, decision, steps[0]) for p in prospects]
        pain = pains[0] if pains and pains[0] is not None and all(
            x is not None and x.code == pains[0].code for x in pains) else None
        try:
            library = await self.state.list_pains()
        except Exception as e:
            logger.warning(f"Writer: could not read the pain library: {e}")
            library = []
        rejected = [p for p in library if p["status"] == "rejected"]
        # The prompt must not carry another offer's content: a rejected pain
        # written for a different offer stays out of it (the gate still
        # checks drafts against it).
        shown = [p for p in rejected if not p["offer_key"] or p["offer_key"] == decision.key]
        return PainPlan(pain=pain, never_use=shown, rejected=rejected,
                        confirmed=[p for p in library if p["status"] == "confirmed"])

    def _raises_rejected_pain(self, plan: PainPlan, *texts: str) -> list[str]:
        """Codes of the rejected pains a draft raises: the same check the
        send gate runs, applied before the draft is ever staged."""
        return [h.code for h in find_rejected_pain_hits("\n".join(texts), plan.rejected, plan.confirmed)]

    def _blocked(self, decision: RouteDecision, brief: OfferBrief | None, contexts: list) -> list[str]:
        """Case-study names a draft must not contain."""
        if brief is not None:
            return brief.blocked
        if not has_case_studies(self.config):
            return []
        return blocked_terms(self.config, decision.offer, [c for c in contexts if c is not None])

    @staticmethod
    def _out_of_scope(blocked: list[str], *texts: str) -> list[str]:
        return case_study_hits("\n".join(texts), blocked) if blocked else []

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        deliverable = {"verified"}
        if getattr(self.config.channels.email, "send_to_risky", False):
            deliverable.add("risky")
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in deliverable
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Route each prospect to one offer (none without offers: configured),
        # then batch by offer and industry so one campaign carries one offer.
        # A rule over a signal nobody collects never matches: say so.
        if routing_enabled(self.config):
            for problem in await offer_problems(self.state, self.config):
                logger.warning(f"Writer: offers: {problem}")
        decisions = {p.id: await self._route(p) for p in prospects_with_email}
        batches = self._group_prospects(prospects_with_email, decisions)

        # Each mailbox can write in its own voice and sign with its own name.
        # When they differ, every new thread gets its mailbox now, so the
        # draft, its follow-ups and the From address all agree.
        plan = await self.voices.plan() if self.is_native else {"pinned": False, "profile": None}
        groups = []
        for batch_name, prospects in batches.items():
            if not prospects:
                continue
            if not plan["pinned"]:
                groups.append((batch_name, prospects, "", plan["profile"]))
                continue
            picks = await self.voices.spread(len(prospects), plan["mailboxes"])
            for mailbox in dict.fromkeys(picks):
                group = [p for p, pick in zip(prospects, picks) if pick == mailbox]
                groups.append((f"{batch_name} · {mailbox}", group, mailbox, await self.voices.profile_for(mailbox)))
        if self.is_native:
            # Running experiments assign their prospects now, before any
            # draft, and each arm's share is written with the arm's profile.
            groups = await experiments.writer_groups(self.state, self.config, groups)

        for batch_name, prospects, mailbox, profile in groups:
            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects, profile, decisions)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            offer_key = decisions[prospects[0].id].key
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
                mailbox=mailbox,
                offer_key=offer_key,
            )
            campaign_id = await self.state.add_campaign(campaign)
            if offer_key:
                await self.state.record_offer_routes(campaign_id, [
                    {"prospect_id": p.id, "offer_key": decisions[p.id].key,
                     "reason": decisions[p.id].reason, "is_default": decisions[p.id].is_default}
                    for p in prospects
                ])

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                profile = await self.personas.for_generation(self.config, sequence[0].generation_id)
                await self._personalize_first_emails(campaign_id, prospects, profile, mailbox, decisions)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                    "offer_key": offer_key,
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list, profile=None,
                                        mailbox: str = "", decisions: dict | None = None):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        ``decisions`` are the offer routes the campaign was grouped by;
        without them each draft keeps the campaign's offer.
        """
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        campaign_offer = ""
        if decisions is None:
            campaign_offer = await self.state.outbox_offer_key({"campaign_id": campaign_id})
        drafted = 0
        for prospect in prospects:
            try:
                decision = (decisions or {}).get(prospect.id) or await decision_for_key(
                    self.state, self.config, prospect, campaign_offer)
                draft = await self._write_personal_email(prospect, profile=profile, decision=decision)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if not draft:
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                generation_id=draft.get("generation_id", ""),
                mailbox=mailbox,
                offer_key=decision.key,
                pain_code=draft.get("pain_code", ""),
                # A draft over its limit is queued flagged, and a flagged draft
                # waits for a person even when approval is otherwise automatic.
                word_limit=draft.get("word_limit", 0),
                flags=draft.get("flags"),
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    # ── Rules every draft is held to ──

    def _limit(self, step: int) -> int:
        return word_limit(self.config, step)

    async def _company_of(self, prospect):
        if not getattr(prospect, "company_id", ""):
            return None
        try:
            return await self.state.get_company(prospect.company_id)
        except Exception:
            return None

    async def _greeting(self, prospect, company, brief) -> GreetingPlan:
        """Who the email greets, from the registry resolver. If it cannot be
        worked out the reader is treated as unnamed: no greeting is safer
        than a wrong one."""
        try:
            return await plan_greeting(self.state, self.config, prospect, company, brief)
        except Exception as e:
            logger.warning(f"Writer: could not resolve a greeting for {getattr(prospect, 'email', '')}: {e}")
            return GreetingPlan()

    def _business(self, prospect, company) -> tuple[str, str, list[str]]:
        """(full name, short name, other spellings of the full name)."""
        full = (getattr(prospect, "company", "") or getattr(company, "name", "") or "").strip()
        return full, short_business_name(full), name_variants(full)

    def _name_rule(self, full: str, short: str) -> str:
        if not full or short.strip().lower() == full.strip().lower():
            return ""
        return (f"\n- Business name: use \"{full}\" in full at most once in the whole email. After that, and "
                f"in the subject line, say \"{short}\". Never put a legal suffix or a location after a dash "
                "in the subject or the call to action.")

    def _facts(self, prospect, company, greeting: GreetingPlan, names: tuple[str, str, list[str]]) -> list[str]:
        """What the Writer knows about the reader. Review counts and ratings
        are left out on purpose: they stay in the scoring notes."""
        full, short, _variants = names
        facts = [greeting_fact_line(greeting, prospect)]
        if prospect.title and greeting.mode != ROUTING:
            facts.append(f"- Title: {prospect.title}")
        if full and short.strip().lower() != full.strip().lower():
            facts.append(f"- Company (full legal name, use it at most once): {full}")
            facts.append(f"- Short business name (use it after the first mention and in subject lines): {short}")
        else:
            facts.append(f"- Company: {full}")
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")
        if company:
            if company.location:
                facts.append(f"- Location: {company.location}")
            description = strip_review_claims(company.description)
            if description:
                facts.append(f"- What the company says about itself: {description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                if str(signal.get("type", "")).upper() in REVIEW_SIGNAL_CODES:
                    continue
                detail = strip_review_claims(str(signal.get("detail", "")))
                if detail:
                    facts.append(f"- Signal ({signal.get('type', 'signal')}): {detail}")
        notes = strip_review_claims(prospect.personalization_notes)
        if notes:
            facts.append(f"- Research notes: {notes}")
        return facts

    async def _voice_for(self, item: dict, earlier: list[dict]) -> dict:
        """The persona an existing email is written in: its own generation's,
        else its opener's, else the voice of the mailbox it goes out from,
        else the default. A follow-up never drifts to another persona."""
        for gid in [item.get("generation_id") or "", *(row.get("generation_id") or "" for row in earlier)]:
            snapshot = await self.personas.snapshot(gid)
            if snapshot:
                return snapshot
        mailbox = (item.get("mailbox") or next((r["mailbox"] for r in earlier if r.get("mailbox")), "")).strip()
        if mailbox:
            return await self.voices.profile_for(mailbox)
        return await self.personas.resolve(self.config)

    async def _earlier_emails(self, item: dict) -> list[dict]:
        """The emails of this thread before ``item``, oldest first."""
        step = int(item.get("step") or 1)
        if step <= 1 or not item.get("campaign_id"):
            return []
        try:
            import aiosqlite

            async with aiosqlite.connect(self.state.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT step, subject, body, generation_id, mailbox FROM outbox "
                    "WHERE campaign_id = ? AND prospect_id = ? AND kind = 'sequence' AND step < ? "
                    "ORDER BY step ASC",
                    (item.get("campaign_id"), item.get("prospect_id"), step),
                ) as cursor:
                    return [dict(r) for r in await cursor.fetchall()]
        except Exception:
            return []

    # ── Checking and retrying a draft ──

    def _attempt_draft(self, result, ctx: DraftContext, verb: str) -> dict | None:
        """A model answer, if it is usable: both fields present, no case study
        outside its scope, no rejected pain. A generic greeting is stripped
        from a reader with no name, and the business name is held to the
        full-once rule."""
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        hits = self._out_of_scope(self._blocked(ctx.decision, ctx.brief, [ctx.decision.context]), subject, body)
        if hits:
            logger.warning(f"Writer: discarded {verb} for {ctx.who}: it names a case "
                           f"study outside its scope ({', '.join(hits)}).")
            return None
        rejected = self._raises_rejected_pain(ctx.plan, subject, body)
        if rejected:
            logger.warning(f"Writer: discarded {verb} for {ctx.who}: it raises a "
                           f"rejected pain ({', '.join(rejected)}).")
            return None
        if ctx.greeting.mode != NAMED:
            body, removed = strip_generic_greeting(body, ctx.business_names)
            if removed:
                logger.info(f"Writer: removed the generic greeting {removed!r} from {verb} for {ctx.who}.")
        subject, body = apply_short_name(subject, body, ctx.full_name, ctx.short_name, ctx.variants)
        return {"subject": subject[:120], "body": body[:2000]}

    def _shorter(self, draft: dict, ctx: DraftContext) -> str:
        return (
            f"\n\nYOUR PREVIOUS DRAFT IS TOO LONG: {count_words(draft['body'])} words. This email may have at "
            f"most {ctx.limit} words, counted over the whole body including the greeting and the sign-off. "
            f"Write it again, shorter: at most {ctx.limit} words including greeting and sign-off. Keep the "
            "facts, the one ask and every rule above; cut everything else.\n"
            f'Previous draft:\n"""\n{draft["body"]}\n"""\n\n'
            'Return ONLY JSON: {"subject": "...", "body": "..."}'
        )

    async def _draft(self, prompt: str, ctx: DraftContext, profile, task: str, instruction: str = "",
                     verb: str = "the draft") -> dict | None:
        """One email from the model, held to its step's word limit before it
        can be staged. A draft over the limit is written again once, with an
        explicit shorter-than-N instruction; if the better of the two is still
        over, it is returned FLAGGED (``flags``, ``word_limit``) so the Outbox
        shows it and a person has to choose to approve it."""
        result = await self.brain.think_json(
            prompt, session_id="mercury-writer", agent="writer", task=task)
        draft = self._attempt_draft(result, ctx, verb)
        if draft is None:
            return None
        used = prompt
        if count_words(draft["body"]) > ctx.limit:
            retry_prompt = prompt + self._shorter(draft, ctx)
            again = self._attempt_draft(await self.brain.think_json(
                retry_prompt, session_id="mercury-writer", agent="writer", task=task), ctx, verb)
            if again is not None and count_words(again["body"]) < count_words(draft["body"]):
                draft, used = again, retry_prompt
        record = dict(draft)
        draft["generation_id"] = await self.personas.record(profile, self.config, used, record, task, instruction)
        draft["pain_code"] = ctx.plan.code
        draft["word_limit"] = ctx.limit
        draft["flags"] = draft_flags(draft["body"], ctx.limit, check_greeting=True,
                                     business_names=ctx.business_names)
        if draft["flags"]:
            logger.warning(f"Writer: {verb} for {ctx.who} is flagged ({', '.join(draft['flags'])}; "
                           f"{count_words(draft['body'])} / {ctx.limit} words). It is staged for review, "
                           "not for automatic approval.")
        return draft

    # ── Email 1 ──

    async def build_personal_prompt(self, prospect, instruction: str = "", profile=None, decision=None):
        """The same assembled inputs for prompt inspection, preview and drafting."""
        sections, profile = await self.personal_prompt_sections(prospect, instruction, profile, decision)
        return "".join(text for _key, _label, text in sections), profile

    async def personal_prompt_sections(self, prospect, instruction: str = "", profile=None, decision=None):
        """The first-email prompt as labelled pieces, in the order the writer receives them.
        ``decision`` None routes the prospect now (prompt inspection, preview)."""
        sections, profile, _ctx = await self._personal_inputs(prospect, instruction, profile, decision)
        return sections, profile

    async def _personal_inputs(self, prospect, instruction: str = "", profile=None,
                               decision: RouteDecision | None = None):
        profile = profile or await self.personas.resolve(self.config)
        if decision is None:
            decision = await self._route(prospect)
        plan = await self._pain_plan([prospect], decision, [1])
        brief = None
        if decision.offer is not None:
            brief = await build_brief(self.state, self.config, decision, [1], [decision.context],
                                      plan.pain)
        company = await self._company_of(prospect)
        greeting = await self._greeting(prospect, company, brief)
        names = self._business(prospect, company)
        facts = self._facts(prospect, company, greeting, names)
        lang_line = await self._market_lang([prospect])
        # Half of the first 24 subjects sent were a variant of "quote form":
        # the model converges on the strongest fact. Show it what is already
        # in the queue so each email finds its own angle and words.
        # With an offer, only that offer's subjects: another offer's wording
        # must not reach this prompt.
        recent_line = ""
        try:
            import aiosqlite

            async with aiosqlite.connect(self.state.db_path) as db:
                async with db.execute(
                    "SELECT subject FROM outbox WHERE step = 1 "
                    "AND status IN ('approved', 'pending_review', 'sent') "
                    + ("AND offer_key = ? " if brief is not None else "")
                    + "ORDER BY created_at DESC LIMIT 25",
                    (decision.key,) if brief is not None else (),
                ) as cursor:
                    recent = [r[0] for r in await cursor.fetchall() if r[0]]
            if recent:
                recent_line = (
                    "\n- Subjects already in the queue (do not reuse or paraphrase any, "
                    "and vary the opening angle too): " + "; ".join(sorted(set(recent))[:25])
                )
        except Exception:
            recent_line = ""
        instruction_line = (
            f"\n- The reviewer asked for this change; it is binding: {instruction.strip()}"
            if instruction and instruction.strip() else ""
        )

        limit = self._limit(1)
        cta = brief.step(1) if brief is not None else None
        # What the email's one ask is. A shared inbox with no known name is
        # asked to route the message, and that request is the ask.
        routing = greeting.mode == ROUTING
        ask = ("the routing request from the Greeting requirement below, as its one ask (the OFFER BRIEF's "
               "call to action is not used in this email)" if routing else
               "the call to action the OFFER BRIEF gives for this email as its one question")
        if brief is not None and brief.offer_sentence(1):
            shape = (
                "One specific observation from the FACTS above, then the offer in one plain\n"
                "  sentence, as the OFFER BRIEF gives it for this email (the brief asks this email\n"
                f"  to state the offer, so that one sentence is allowed), then {ask}.\n"
                "  Say nothing else about the offer."
            )
        elif routing:
            shape = (
                "One specific observation from the FACTS above, then\n"
                f"  {ask}.\n"
                + ("  Say about the offer only what the OFFER BRIEF allows." if brief is not None
                   else "  No pitch, no product name.")
            )
        elif cta is not None and cta.cta:
            shape = (
                "One specific observation from the FACTS above, then the\n"
                "  call to action the OFFER BRIEF gives for this email as its one\n"
                "  question. Say about the offer only what the OFFER BRIEF allows."
            )
        else:
            shape = (
                "One specific observation from the FACTS above, one\n"
                "  question. No pitch, no product name."
            )
        task = f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- At most {limit} words in the body, counting the greeting and the sign-off. {shape}
- {greeting_lines(greeting, 1)}{self._name_rule(names[0], names[1])}
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-4 words, reads like an internal note.
- Language and register: {lang_line}
- {self._sign_off_rule(profile)}
- Follow every STRICT EMAIL RULE and the EVIDENCE-BACKED RULES above.{instruction_line}{recent_line}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        sections = self._base_sections(profile, brief, plan) + [("email", "This email", task)]
        ctx = DraftContext(
            step=1, limit=limit, decision=decision, brief=brief, plan=plan, greeting=greeting,
            full_name=names[0], short_name=names[1], variants=names[2],
            business_names=business_names(prospect, company), who=getattr(prospect, "email", ""))
        return sections, profile, ctx

    async def _write_personal_email(self, prospect, instruction: str = "", profile=None,
                                    decision: RouteDecision | None = None) -> dict | None:
        sections, profile, ctx = await self._personal_inputs(prospect, instruction, profile, decision)
        prompt = "".join(text for _key, _label, text in sections)
        return await self._draft(prompt, ctx, profile, "personal_email", instruction, "the draft")

    # ── Follow-ups ──

    def _step_role(self, step: int, limit: int, brief: OfferBrief | None) -> str:
        """What this email is for. An offer brief that gives the step an angle
        or a call to action decides it; the fixed descriptions are the
        fallback for a config without one."""
        spec = brief.step(step) if brief is not None else None
        if spec is not None and (spec.cta or spec.angle or spec.state_offer):
            return (f"email {step} of the thread, at most {limit} words including greeting and sign-off. "
                    f"Use the angle and call to action the OFFER BRIEF gives for email {step}")
        proof = ("the approved claims or a case study in the OFFER BRIEF"
                 if brief is not None and brief.authoritative else "the product knowledge")
        if step == 2:
            return (f"a FOLLOW-UP sent 3 days after the first email: at most {limit} words including "
                    "greeting and sign-off, at least four sentences, stands on its own, a different "
                    f"angle with one concrete proof point from {proof}, and an interest-based question")
        return (f"the BREAK-UP email, the last one: at most {limit} words including greeting and "
                "sign-off, gives permission to say no, leaves the door open, no guilt")

    async def regenerate_email(self, item: dict, prospect, instruction: str = "") -> dict | None:
        """Rewrite one outbox draft from the review desk.

        Step 1 is drafted again from the facts; a follow-up is rewritten in
        the voice of the persona its thread started with, with its step's
        angle and call to action from the offer brief, and with the earlier
        emails of the thread in front of it so it adds something new instead
        of repeating them. The reviewer's instruction ("más corto",
        "menciona la constructora") is binding.
        """
        step = int(item.get("step") or 1)
        earlier = await self._earlier_emails(item)
        profile = await self._voice_for(item, earlier)
        # A rewrite keeps the offer the email was written for; it is never
        # routed again (a row without one stays without one).
        decision = await decision_for_key(self.state, self.config, prospect,
                                          await self.state.outbox_offer_key(item))
        if step == 1:
            return await self._write_personal_email(prospect, instruction=instruction,
                                                    profile=profile, decision=decision)

        lang_line = await self._market_lang([prospect])
        instruction_line = (
            f"\n- The reviewer asked for this change; it is binding: {instruction.strip()}"
            if instruction and instruction.strip() else ""
        )
        plan = await self._pain_plan([prospect], decision, [step])
        brief = None
        if decision.offer is not None:
            brief = await build_brief(self.state, self.config, decision, [step], [decision.context],
                                      plan.pain)
        company = await self._company_of(prospect)
        greeting = await self._greeting(prospect, company, brief)
        names = self._business(prospect, company)
        facts = self._facts(prospect, company, greeting, names)
        limit = self._limit(step)
        thread = "\n\n".join(
            f'Email {row["step"]} (subject: {row["subject"]}):\n"""\n{row["body"]}\n"""' for row in earlier
        ) or "(not available)"
        prompt = self._base_prompt(profile, brief, plan)
        prompt += f"""

Rewrite ONE email for this reader. It is {self._step_role(step, limit, brief)}.

FACTS — everything you know about them. Every claim about the prospect must come from
these facts; do not invent anything:
{chr(10).join(facts)}

The earlier emails of this thread, oldest first. The reader has already read them:
{thread}

This email must add something new. Do not repeat or paraphrase any sentence of the earlier
emails, and above all do not say again what the product or the offer is: an earlier email
has already said it, so assume the reader knows and bring one new piece of information.

Requirements:
- At most {limit} words in the body, counting the greeting and the sign-off.
- {greeting_lines(greeting, step)}{self._name_rule(names[0], names[1])}
- Write the actual text (no merge variables).
- Subject: lowercase, 2-4 words, like an internal note.
- Write in the WRITING VOICE above, the same voice as email 1.
- Language and register: {lang_line}
- {self._sign_off_rule(profile)}
- Follow every STRICT EMAIL RULE and the EVIDENCE-BACKED RULES above.{instruction_line}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
        ctx = DraftContext(
            step=step, limit=limit, decision=decision, brief=brief, plan=plan, greeting=greeting,
            full_name=names[0], short_name=names[1], variants=names[2],
            business_names=business_names(prospect, company), who=getattr(prospect, "email", ""))
        return await self._draft(prompt, ctx, profile, "regenerate_email", instruction, "a rewrite")

    # ── Language ──

    def _market_of(self, prospect, company):
        """(the icp.markets entry the company belongs to or None, its language)."""
        markets = getattr(self.config.icp, "markets", None) or []
        loc = ((getattr(company, "location", "") or "") + " "
               + (getattr(company, "domain", "") or "")).lower()
        for market in markets:
            if any(place.lower() in loc for place in market.places):
                return market, market.lang
        if loc.rstrip().endswith(".do") or ".com.do" in loc or "domin" in loc:
            return None, "es"
        return None, ""

    async def _market_lang(self, prospects: list) -> str:
        """The language line for a batch, decided from the prospects' market.

        Matched against icp.markets by company location (or a .do domain),
        never left to the model: half the Dominican follow-ups came out in
        English when the sequence prompt did not say which language to use.

        A market may carry its own ``language_line`` (replacing the built-in
        line), ``language_rules`` and ``terminology``; those come from private
        config and are added to every prompt for prospects in that market.

        Region-specific text still hard-coded, which that config can replace
        when it is moved out of the repository: the two built-in lines below,
        the ``.do`` / "domin" detection in ``_market_of``, and in
        prompts/writer.md the CASE STUDIES AND REGISTER section, the whole
        DOMINICAN REGISTER section, the Latin America bullet of the
        EVIDENCE-BACKED RULES, and the Spanish allowances in rule 16 and in
        the LANGUAGE section.
        """
        votes: dict[str, int] = {}
        winners: dict[str, object] = {}
        for prospect in prospects[:8]:
            company = await self._company_of(prospect)
            market, lang = self._market_of(prospect, company)
            if lang:
                votes[lang] = votes.get(lang, 0) + 1
                if market is not None:
                    winners.setdefault(lang, market)
        lang = max(votes, key=votes.get) if votes else "en"
        market = winners.get(lang)
        writer_cfg = getattr(self.config, "writer", None)
        custom = (market.language_line if market is not None
                  else getattr(writer_cfg, "default_language_line", "")) or ""
        if custom.strip():
            line = custom.strip()
        elif lang == "es":
            line = ("Spanish, for prospects in the Dominican Republic. The whole "
                    "sequence follows DOMINICAN REGISTER above — emails 2 and 3 "
                    "too. No English anywhere, not even a subject line.")
        else:
            line = ("English, for prospects in the United States. Never mention the "
                    "Dominican Republic or a Dominican client; say \"a local business "
                    "like yours\".")
        rules = list(market.language_rules if market is not None else getattr(writer_cfg, "language_rules", []))
        terms = dict(market.terminology) if market is not None else {}
        if terms:
            rules.append("Terminology: " + "; ".join(
                f'say "{wanted}", never "{avoid}"' for avoid, wanted in terms.items()) + ".")
        if rules:
            line += "\n  Market language rules (binding):\n" + "\n".join(f"  - {r}" for r in rules)
        return line

    # ── The shared sequence ──

    async def _write_sequence(self, prospects: list, profile=None,
                              decisions: dict | None = None) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence.

        ``decisions`` are the prospects' offer routes; the group shares the
        first one's offer (``run`` groups by offer). Without them the
        prospects are routed here.
        """
        if decisions is None:
            decisions = {p.id: await self._route(p) for p in prospects}
        decision = decisions[prospects[0].id]
        contexts = [decisions[p.id].context for p in prospects if p.id in decisions]
        # One pain for the shared template, and only when every prospect in
        # the group has that same one (see _pain_plan).
        plan = await self._pain_plan(prospects, decision, [1])
        brief = None
        if decision.offer is not None:
            brief = await build_brief(self.state, self.config, decision, [1, 2, 3], contexts, plan.pain)
        lang_line = await self._market_lang(prospects)
        greetings = [await self._greeting(p, await self._company_of(p), brief) for p in prospects]
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {strip_review_claims(p.personalization_notes)}"
               if strip_review_claims(p.personalization_notes) else "")
            for p in prospects[:5]  # Show sample for context
        )

        profile = profile or await self.personas.resolve(self.config)
        prompt = self._base_prompt(profile, brief, plan)
        proof = ("the approved claims or a case study in the OFFER BRIEF"
                 if brief is not None and brief.authoritative else "the product knowledge")
        brief_line = (
            "\n- Each email uses the angle and call to action the OFFER BRIEF gives for it;\n"
            "  they take precedence over the descriptions above."
            if brief is not None and any(brief.step(n) for n in (1, 2, 3)) else ""
        )
        l1, l2, l3 = (self._limit(n) for n in (1, 2, 3))
        offer_once = (
            "\n- The OFFER BRIEF asks for the offer to be stated in one plain sentence in some emails: say "
            "it only in the email(s) it names, never again in a later one. Each email adds new information "
            "and never repeats the product or offer sentence of an earlier one."
            if brief is not None else
            "\n- Each email adds new information and never repeats the product sentence of an earlier one."
        )

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Every email's word limit counts the whole body, greeting and sign-off included.
- Email 1: Personalized cold observation + one question. At most {l1} words. No pitch.
- Email 2: Follow-up 3 days later. It must stand on its own (the reader does
  not remember email 1): at most {l2} words, at least four sentences, a different
  angle, one concrete proof point from {proof}, and an
  interest-based question ("¿le interesa que le cuente cómo…?" / "worth
  hearing how…?"), never "thoughts?".
- Email 3: Break-up 4 days after that. At most {l3} words. Gives permission to say
  no and leaves the door open; no guilt ("I never heard back" is banned).
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables — every email
  must use at least one, and email 1 must reference something specific to
  these prospects' industry or role (use the notes above).
- {sequence_lines(greetings)}
- Write all three emails in the WRITING VOICE above.
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-4 words, like an internal note; no salesy words.
- Language and register: {lang_line}
- {self._sign_off_rule(profile)}
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.{brief_line}{offer_once}

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="mercury-writer",
            agent="writer", task="write_sequence",
        )
        steps = self._parse_sequence(result)
        # Over a step's limit: ask once for a shorter sequence. What is still
        # over afterwards is flagged when the Sender stages the rendered email.
        over = [(st.step, count_words(st.body), self._limit(st.step)) for st in steps
                if count_words(st.body) > self._limit(st.step)]
        if over:
            note = ("\n\nYOUR PREVIOUS SEQUENCE HAD EMAILS THAT ARE TOO LONG: "
                    + "; ".join(f"email {n} is {c} words, the limit is {lim}" for n, c, lim in over)
                    + ". Write the whole sequence again with every email within its limit, counted "
                      "including greeting and sign-off. Previous sequence:\n"
                    + json.dumps([{"step": st.step, "subject": st.subject, "body": st.body} for st in steps],
                                 ensure_ascii=False))
            retry = self._parse_sequence(await self.brain.think_json(
                prompt + note, session_id="mercury-writer", agent="writer", task="write_sequence"))
            excess = lambda seq: sum(max(0, count_words(st.body) - self._limit(st.step)) for st in seq)  # noqa: E731
            if len(retry) >= len(steps) and excess(retry) < excess(steps):
                steps, prompt = retry, prompt + note
        hits = self._out_of_scope(self._blocked(decision, brief, contexts),
                                  *(f"{st.subject}\n{st.body}" for st in steps))
        if hits:
            logger.warning(f"Writer: discarded a sequence: it names a case study outside its "
                           f"scope ({', '.join(hits)}). The prospects are tried again next cycle.")
            return []
        rejected = self._raises_rejected_pain(plan, *(f"{st.subject}\n{st.body}" for st in steps))
        if rejected:
            logger.warning(f"Writer: discarded a sequence: it raises a rejected pain "
                           f"({', '.join(rejected)}). The prospects are tried again next cycle.")
            return []
        if steps:
            generation_id = await self.personas.record(
                profile, self.config, prompt, [step.model_dump() for step in steps], "write_sequence",
            )
            for step in steps:
                step.generation_id = generation_id
                step.pain_code = plan.code
        return steps

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list, decisions: dict | None = None) -> dict[str, list]:
        """Group prospects into campaign batches by offer and industry."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category, and by offer: a
            # campaign carries exactly one offer_key.
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"
            decision = (decisions or {}).get(prospect.id)
            if decision is not None and decision.key:
                key += f" · {decision.key}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
