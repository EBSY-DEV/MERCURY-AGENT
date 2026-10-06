"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
from datetime import datetime, timezone

from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 20
LEGACY_BATCH_CAP = 50


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        # Loaded here too so drafts requested outside run() (the review
        # desk's regenerate, one-off scripts) get the product knowledge.
        self.skills = self.brain.load_skills_for_agent("writer")
        self.config = config
        self.env = env

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

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        drafted = 0
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
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
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _write_personal_email(self, prospect, instruction: str = "") -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
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
        recent_line = ""
        try:
            import aiosqlite

            async with aiosqlite.connect(self.state.db_path) as db:
                async with db.execute(
                    "SELECT subject FROM outbox WHERE step = 1 "
                    "AND status IN ('approved', 'pending_review', 'sent') "
                    "ORDER BY created_at DESC LIMIT 25"
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

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- 50-90 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-4 words, reads like an internal note.
- Language and register: {lang_line}
- Follow every STRICT EMAIL RULE and the EVIDENCE-BACKED RULES above.{instruction_line}{recent_line}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    async def regenerate_email(self, item: dict, prospect, instruction: str = "") -> dict | None:
        """Rewrite one outbox draft from the review desk.

        Step 1 is drafted again from the facts; a follow-up is rewritten in
        the context of the first email of its thread. The reviewer's
        instruction ("más corto", "menciona la constructora") is binding.
        """
        step = int(item.get("step") or 1)
        if step == 1:
            return await self._write_personal_email(prospect, instruction=instruction)

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
        role = (
            "a FOLLOW-UP sent 3 days after the first email: 60-110 words, at least "
            "four sentences, stands on its own, a different angle with one concrete "
            "proof point from the product knowledge, and an interest-based question"
            if step == 2 else
            "the BREAK-UP email, the last one: 30-50 words, gives permission to say "
            "no, leaves the door open, no guilt"
        )
        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
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
            prompt, session_id="harvey-writer",
            agent="writer", task="regenerate_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

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

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        lang_line = await self._market_lang(prospects)
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold observation + one question. 50-90 words. No pitch.
- Email 2: Follow-up 3 days later. It must stand on its own (the reader does
  not remember email 1): 60-110 words, at least four sentences, a different
  angle, one concrete proof point from the product knowledge, and an
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
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        return self._parse_sequence(result)

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

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
