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

from mercury.brain import AGENT_SKILLS, Brain
from mercury.config import MercuryConfig
from mercury.integrations.mail_provider import NATIVE_PROVIDERS
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
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def build_personal_prompt(self, prospect, instruction: str = "", profile=None, decision=None):
        """The same assembled inputs for prompt inspection, preview and drafting."""
        sections, profile = await self.personal_prompt_sections(prospect, instruction, profile, decision)
        return "".join(text for _key, _label, text in sections), profile

    async def personal_prompt_sections(self, prospect, instruction: str = "", profile=None, decision=None):
        """The first-email prompt as labelled pieces, in the order the writer receives them.
        ``decision`` None routes the prospect now (prompt inspection, preview)."""
        sections, profile, _brief, _decision, _plan = await self._personal_inputs(
            prospect, instruction, profile, decision)
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
        facts = [
            f"- Name: {prospect.full_name()}",
            f"- Title: {prospect.title}",
            f"- Company: {prospect.company}",
        ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.location:
                facts.append(f"- Location: {company.location}")
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
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

        cta = brief.step(1) if brief is not None else None
        shape = (
            "One specific observation from the FACTS above, then the\n"
            "  call to action the OFFER BRIEF gives for this email as its one\n"
            "  question. Say about the offer only what the OFFER BRIEF allows."
            if cta is not None and cta.cta else
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
- 50-90 words. {shape}
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-4 words, reads like an internal note.
- Language and register: {lang_line}
- {self._sign_off_rule(profile)}
- Follow every STRICT EMAIL RULE and the EVIDENCE-BACKED RULES above.{instruction_line}{recent_line}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        sections = self._base_sections(profile, brief, plan) + [("email", "This email", task)]
        return sections, profile, brief, decision, plan

    async def _write_personal_email(self, prospect, instruction: str = "", profile=None,
                                    decision: RouteDecision | None = None) -> dict | None:
        sections, profile, brief, decision, plan = await self._personal_inputs(
            prospect, instruction, profile, decision)
        prompt = "".join(text for _key, _label, text in sections)
        result = await self.brain.think_json(
            prompt, session_id="mercury-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        hits = self._out_of_scope(self._blocked(decision, brief, [decision.context]), subject, body)
        if hits:
            logger.warning(f"Writer: discarded the draft for {prospect.email}: it names a case "
                           f"study outside its scope ({', '.join(hits)}).")
            return None
        rejected = self._raises_rejected_pain(plan, subject, body)
        if rejected:
            logger.warning(f"Writer: discarded the draft for {prospect.email}: it raises a "
                           f"rejected pain ({', '.join(rejected)}).")
            return None
        draft = {"subject": subject[:120], "body": body[:2000]}
        draft["generation_id"] = await self.personas.record(
            profile, self.config, prompt, draft, "personal_email", instruction,
        )
        draft["pain_code"] = plan.code
        return draft

    async def regenerate_email(self, item: dict, prospect, instruction: str = "") -> dict | None:
        """Rewrite one outbox draft from the review desk.

        Step 1 is drafted again from the facts; a follow-up is rewritten in
        the context of the first email of its thread. The reviewer's
        instruction ("más corto", "menciona la constructora") is binding.
        """
        profile = await self.personas.for_generation(self.config, item.get("generation_id", ""))
        step = int(item.get("step") or 1)
        # A rewrite keeps the offer the email was written for; it is never
        # routed again (a row without one stays without one).
        decision = await decision_for_key(self.state, self.config, prospect,
                                          await self.state.outbox_offer_key(item))
        if step == 1:
            return await self._write_personal_email(prospect, instruction=instruction,
                                                    profile=profile, decision=decision)

        first = ""
        try:
            import aiosqlite

            async with aiosqlite.connect(self.state.db_path) as db:
                async with db.execute(
                    "SELECT body FROM outbox WHERE campaign_id = ? AND prospect_id = ? "
                    "AND step = 1 ORDER BY created_at DESC LIMIT 1",
                    (item.get("campaign_id"), item.get("prospect_id")),
                ) as cursor:
                    row = await cursor.fetchone()
                    first = row[0] if row else ""
        except Exception:
            first = ""

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
        proof = ("the approved claims or a case study in the OFFER BRIEF"
                 if brief is not None and brief.authoritative else "the product knowledge")
        role = (
            "a FOLLOW-UP sent 3 days after the first email: 60-110 words, at least "
            "four sentences, stands on its own, a different angle with one concrete "
            f"proof point from {proof}, and an interest-based question"
            if step == 2 else
            "the BREAK-UP email, the last one: 30-50 words, gives permission to say "
            "no, leaves the door open, no guilt"
        )
        if brief is not None and brief.step(step) is not None:
            role += (f". Use the angle and call to action the OFFER BRIEF gives for email {step}; "
                     "they take precedence over this description")
        prompt = self._base_prompt(profile, brief, plan)
        prompt += f"""

Rewrite ONE email for this person: {prospect.full_name()}, {prospect.title} at {prospect.company}.
It is {role}.

The first email of the thread was:
\"\"\"
{first or "(not available)"}
\"\"\"

Requirements:
- Write the actual text (no merge variables).
- Subject: lowercase, 2-4 words, like an internal note.
- Language and register: {lang_line}
- Follow every STRICT EMAIL RULE and the EVIDENCE-BACKED RULES above.{instruction_line}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
        result = await self.brain.think_json(
            prompt, session_id="mercury-writer",
            agent="writer", task="regenerate_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        hits = self._out_of_scope(self._blocked(decision, brief, [decision.context]), subject, body)
        if hits:
            logger.warning(f"Writer: discarded a rewrite for {prospect.email}: it names a case "
                           f"study outside its scope ({', '.join(hits)}).")
            return None
        rejected = self._raises_rejected_pain(plan, subject, body)
        if rejected:
            logger.warning(f"Writer: discarded a rewrite for {prospect.email}: it raises a "
                           f"rejected pain ({', '.join(rejected)}).")
            return None
        draft = {"subject": subject[:120], "body": body[:2000]}
        draft["generation_id"] = await self.personas.record(
            profile, self.config, prompt, draft, "regenerate_email", instruction,
        )
        draft["pain_code"] = plan.code
        return draft

    async def _market_lang(self, prospects: list) -> str:
        """The language line for a batch, decided from the prospects' market.

        Matched against icp.markets by company location (or a .do domain),
        never left to the model: half the Dominican follow-ups came out in
        English when the sequence prompt did not say which language to use.
        """
        votes: dict[str, int] = {}
        markets = getattr(self.config.icp, "markets", None) or []
        for prospect in prospects[:8]:
            company = None
            if getattr(prospect, "company_id", ""):
                try:
                    company = await self.state.get_company(prospect.company_id)
                except Exception:
                    company = None
            loc = ((getattr(company, "location", "") or "") + " "
                   + (getattr(company, "domain", "") or "")).lower()
            lang = ""
            for market in markets:
                if any(place.lower() in loc for place in market.places):
                    lang = market.lang
                    break
            if not lang and (loc.rstrip().endswith(".do") or ".com.do" in loc
                             or "domin" in loc):
                lang = "es"
            if lang:
                votes[lang] = votes.get(lang, 0) + 1
        lang = max(votes, key=votes.get) if votes else "en"
        if lang == "es":
            return ("Spanish, for prospects in the Dominican Republic. The whole "
                    "sequence follows DOMINICAN REGISTER above — emails 2 and 3 "
                    "too. No English anywhere, not even a subject line.")
        return ("English, for prospects in the United States. Never mention the "
                "Dominican Republic or a Dominican client; say \"a local business "
                "like yours\".")

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
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
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

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold observation + one question. 50-90 words. No pitch.
- Email 2: Follow-up 3 days later. It must stand on its own (the reader does
  not remember email 1): 60-110 words, at least four sentences, a different
  angle, one concrete proof point from {proof}, and an
  interest-based question ("¿le interesa que le cuente cómo…?" / "worth
  hearing how…?"), never "thoughts?".
- Email 3: Break-up 4 days after that. 30-50 words. Gives permission to say
  no and leaves the door open; no guilt ("I never heard back" is banned).
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables — every email
  must use at least one, and email 1 must reference something specific to
  these prospects' industry or role (use the notes above).
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-4 words, like an internal note; no salesy words.
- Language and register: {lang_line}
- {self._sign_off_rule(profile)}
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.{brief_line}

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
