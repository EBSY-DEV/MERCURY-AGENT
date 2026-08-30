"""The signal vocabulary Harvey proposes, and the user confirms.

Harvey never decides on its own what makes a good prospect. It proposes a
catalog of signals it knows how to collect — each with a plain-language
description, what it costs, and why it might matter — and the user confirms,
edits, or rejects them in the dashboard before any collection runs.

Only ``confirmed`` signals get collected. That keeps spend intentional and
keeps the resulting cohorts explainable: a prospect list is a query over
signals a human chose, not a black box.

Design note: every signal is recorded as an OBSERVATION (a row), never a
column. A new signal needs no migration, history comes free, and confidence
and provenance travel with the fact.
"""

# category: discovery | profile | people | verification
# value_type: num | text | bool
# cost_note: what collecting it actually costs, in plain language
SIGNAL_CATALOG: list[dict] = [
    # ── Discovery: who exists, and how visible are they ──
    {
        "code": "SERP_RANK",
        "label": "Search rank position",
        "description": (
            "Where the business ranks for its core '<service> <city>' search. "
            "The most useful prospecting signal there is: positions 11-30 are "
            "the sweet spot — visible enough to be trying, not winning yet. "
            "Top 10 are already winning (harder sell); 31+ are cheaper but "
            "less motivated."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "~$0.0006 per search (paid SERP API, queued endpoint)",
        "confidence_floor": 0.0,
    },
    {
        "code": "FOUND_IN_SERP",
        "label": "Discovered via search",
        "description": "The business appeared in a target-market search — how Harvey found it at all.",
        "category": "discovery",
        "value_type": "text",
        "collector": "discover",
        "cost_note": "included in the search above",
        "confidence_floor": 0.0,
    },

    # ── Profile: what they are (free — HTML Harvey already fetches) ──
    {
        "code": "INCUMBENT_AGENCY",
        "label": "Has a marketing/web agency",
        "description": (
            "An agency credited in the site footer ('Website by X'). High-value: "
            "it tells you who you're displacing, and every detection builds a "
            "roster you can target as a whole book of business. Only recorded "
            "when the credit wording is explicit — a wrong incumbent is worse "
            "than none."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.75,
    },
    {
        "code": "RUNNING_GOOGLE_ADS",
        "label": "Running Google Ads",
        "description": "Google Ads tag on the site — they are spending on acquisition TODAY. Strong budget signal.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "RUNNING_META_ADS",
        "label": "Running Meta/Facebook Ads",
        "description": "Meta Pixel on the site — actively spending on paid social.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "TECH_STACK",
        "label": "Website platform / tools",
        "description": "What the site is built on and which tools it runs (WordPress, Shopify, HubSpot, chat widgets, booking tools).",
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_SCHEMA_MARKUP",
        "label": "Missing structured data",
        "description": "No JSON-LD schema markup — a concrete, checkable SEO gap you can name in an email.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_ONLINE_BOOKING",
        "label": "No online booking/scheduling",
        "description": "No booking or scheduling widget detected — leads have to phone in, so after-hours demand is lost.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "SITE_PAGE_COUNT",
        "label": "Site size",
        "description": "Page count from sitemap.xml. A very thin site suggests little investment; a large stale one suggests neglect.",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "free (sitemap.xml)",
        "confidence_floor": 0.0,
    },
    {
        "code": "BLOG_STALE",
        "label": "Content gone stale",
        "description": "No new content in a long time — content marketing started and abandoned.",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "free (sitemap.xml)",
        "confidence_floor": 0.0,
    },
    {
        "code": "BLOCKS_AI_CRAWLERS",
        "label": "Blocks AI crawlers",
        "description": "robots.txt blocks AI crawlers — they'll be invisible in AI search answers.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (robots.txt)",
        "confidence_floor": 0.0,
    },
    {
        "code": "HIRING_ROLE",
        "label": "Hiring for a relevant role",
        "description": "An open role on their careers page that implies they're buying in your category right now.",
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (careers page)",
        "confidence_floor": 0.0,
    },

    # ── People: who decides ──
    {
        "code": "CONTACT_FOUND",
        "label": "Named contact found",
        "description": "A named person scraped from the team/about page, with their title.",
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (team page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "DECISION_MAKER_TITLE",
        "label": "Decision-maker title",
        "description": "Title matches an owner/decision-maker pattern (Owner, President, Practice Manager, Marketing Director).",
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (team page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "REGISTRY_VERIFIED",
        "label": "Confirmed in a public registry",
        "description": (
            "The person was confirmed in an authoritative public registry "
            "(licensing board, professional registry). Also filters out "
            "scraping artifacts — a name that matches nothing usually wasn't a person."
        ),
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (public registry API)",
        "confidence_floor": 0.0,
    },
    {
        "code": "LIKELY_OWNER",
        "label": "Likely the owner",
        "description": "Registry indicators suggest this person owns the business rather than working there.",
        "category": "people",
        "value_type": "bool",
        "collector": "people",
        "cost_note": "free (public registry API)",
        "confidence_floor": 0.0,
    },

    # ── Verification ──
    {
        "code": "EMAIL_PATTERN",
        "label": "Company email pattern",
        "description": "The domain's address format (first.last@, flast@). One real address cracks the whole company.",
        "category": "verification",
        "value_type": "text",
        "collector": "verify",
        "cost_note": "free, or ~1 lookup credit",
        "confidence_floor": 0.0,
    },
    {
        "code": "EMAIL_STATUS",
        "label": "Email deliverability",
        "description": "Whether the address is verified, catch-all/risky, an unverified guess, or invalid.",
        "category": "verification",
        "value_type": "text",
        "collector": "verify",
        "cost_note": "1 verification credit (free tiers available)",
        "confidence_floor": 0.0,
    },
    {
        "code": "CONTACT_FORM_URL",
        "label": "Contact form available",
        "description": (
            "A contact form on the site. Worth knowing because it reaches the "
            "business without needing an email address at all — and roughly "
            "half of domains can't be email-verified."
        ),
        "category": "verification",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (site HTML)",
        "confidence_floor": 0.0,
    },
]


async def seed_signal_catalog(state) -> int:
    """Register the catalog as PROPOSED signals awaiting user confirmation.

    Idempotent, and never overrides a decision the user already made: an
    existing signal keeps its confirmed/rejected status.
    """
    for sig in SIGNAL_CATALOG:
        await state.upsert_signal_code(
            sig["code"],
            label=sig["label"],
            description=sig["description"],
            category=sig["category"],
            value_type=sig["value_type"],
            collector=sig["collector"],
            cost_note=sig["cost_note"],
            confidence_floor=sig.get("confidence_floor", 0.0),
        )
    return len(SIGNAL_CATALOG)
