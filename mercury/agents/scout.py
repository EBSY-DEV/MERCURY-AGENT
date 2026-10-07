"""Scout — DIY prospecting without expensive tools.

Architecture: Python does ALL web searching/scraping. Claude only
analyzes, scores, and personalizes data that Python already found.
This avoids model-level refusals when asking Claude to research
real people/companies.

Search backends (in priority order):
1. SERP API (Serper.dev) — if SERPER_API_KEY is set, reliable and fast
2. DuckDuckGo HTML — free, no rate limiting, good fallback
3. Bing — free, less aggressive than Google
4. Google — aggressive rate limiting, used last

Each cycle runs 2-3 queries max to avoid rate limits. Queries are
tracked in the DB so they spread across heartbeat cycles.

Hardening notes:
- All outbound HTTP goes through ``_fetch`` which adds timeouts, retries
  with exponential backoff + jitter, rotating user-agents, and detection
  of rate-limit / block / captcha responses.
- Every parse path is defensive: malformed HTML or JSON never crashes a
  cycle, it just yields zero results for that source.
- Prospects and companies are deduplicated both against the DB and
  in-memory across every strategy within a cycle.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from mercury.brain import Brain
from mercury.config import MercuryConfig, EnvConfig
from mercury.integrations.email_finder import find_email
from mercury.integrations.linkedin import LinkedInAutomation
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.state import StateManager

logger = logging.getLogger("mercury.scout")

# Max queries per cycle to avoid rate limits
MAX_QUERIES_PER_CYCLE = 10  # each query = 1 Serper credit
# Delay between search requests (seconds)
SEARCH_DELAY = (2, 5)

# HTTP hardening knobs
HTTP_TIMEOUT = 15.0
SCRAPE_TIMEOUT = 12.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # seconds; grows exponentially per attempt
MAX_RESULTS_PER_QUERY = 10  # Serper free tier rejects num>10 with HTTP 400
MAX_COMPANIES_PER_CYCLE = 30
MAX_PROSPECTS_PER_CYCLE = 25

# Rotate a small pool of realistic desktop user-agents. Scrapers that always
# send one UA are trivially fingerprinted and blocked.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) "
    "Gecko/20100101 Firefox/122.0",
]

# Signals that a response is a block / bot-challenge rather than real content.
_BLOCK_MARKERS = (
    "unusual traffic",
    "captcha",
    "are you a robot",
    "verify you are human",
    "detected unusual activity",
    "access denied",
    "/sorry/index",
)

# Seniority / title vocabulary reused for ICP heuristics.
_SENIOR_KEYWORDS = (
    "ceo", "cto", "cfo", "cmo", "coo", "cro", "chief", "founder",
    "co-founder", "president", "owner", "partner", "vp", "vice president",
    "svp", "evp", "head of", "director",
)


class Scout:
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
        self.env = env
        self.skills = ""
        self._queries_this_cycle = 0
        # In-memory dedup guards, reset each cycle.
        self._seen_prospect_keys: set[str] = set()
        self._seen_domains: set[str] = set()

    async def run(self):
        """Main prospecting flow: find leads matching ICP."""
        logger.info("Scout: Starting prospecting cycle...")

        self.skills = self.brain.load_skills_for_agent("scout")
        self._queries_this_cycle = 0
        self._seen_prospect_keys = set()
        self._seen_domains = set()

        prospects_found = 0

        # Each strategy is isolated so one failing does not abort the cycle.
        strategies = []
        if self.config.channels.linkedin.enabled and self.env.linkedin_email:
            strategies.append(("linkedin", self._prospect_via_linkedin))
        # Job boards first: hiring is the strongest intent signal, and the
        # strategy self-skips when python-jobspy isn't installed.
        strategies.append(("hiring_boards", self._prospect_via_hiring_boards))
        # Profile search returns names scraped off LinkedIn URLs with no
        # company and no domain, so nothing downstream can ever resolve an
        # address for them: one cycle produced 15 such prospects, all dead on
        # arrival, at one Serper credit per query. Local trades are found
        # company-first (discovery + inbox sweep), not person-first.
        if self.ENABLE_PROFILE_SEARCH:
            strategies.append(("profile_search", self._prospect_via_profile_search))
        strategies.append(("company_discovery", self._prospect_via_company_discovery))

        for name, strategy in strategies:
            try:
                prospects_found += await strategy()
            except Exception as e:
                logger.warning(f"Scout: strategy '{name}' failed: {e}")

        await self.state.log_action(
            action_type="prospect",
            agent="scout",
            details={"prospects_found": prospects_found},
        )
        logger.info(f"Scout: Found {prospects_found} new prospects this cycle.")

    # ── Prospect dedup helpers ──

    def _prospect_key(self, prospect: Prospect) -> str:
        """Stable identity key so we never emit the same person twice."""
        if prospect.email:
            return f"email:{prospect.email.lower().strip()}"
        if prospect.linkedin_url:
            # Normalise LinkedIn URLs (strip protocol/query/trailing slash).
            u = prospect.linkedin_url.lower().split("?")[0].rstrip("/")
            u = re.sub(r"^https?://(www\.)?", "", u)
            return f"li:{u}"
        return (
            f"name:{prospect.first_name.lower().strip()}"
            f"|{prospect.last_name.lower().strip()}"
            f"|{prospect.company.lower().strip()}"
        )

    async def _is_duplicate_prospect(self, prospect: Prospect) -> bool:
        """True if we've already seen this prospect this cycle or in the DB."""
        key = self._prospect_key(prospect)
        if key in self._seen_prospect_keys:
            return True
        try:
            if await self.state.prospect_exists(
                email=prospect.email,
                linkedin_url=prospect.linkedin_url,
                first_name=prospect.first_name,
                last_name=prospect.last_name,
                company=prospect.company,
            ):
                self._seen_prospect_keys.add(key)
                return True
        except Exception as e:
            logger.debug(f"prospect_exists check failed: {e}")
        return False

    def _remember_prospect(self, prospect: Prospect):
        self._seen_prospect_keys.add(self._prospect_key(prospect))

    # ── Central HTTP layer (timeouts, retries, backoff, UA rotation) ──

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        if not text:
            return False
        low = text[:4000].lower()
        return any(marker in low for marker in _BLOCK_MARKERS)

    async def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        headers: dict | None = None,
        timeout: float = HTTP_TIMEOUT,
        retries: int = MAX_RETRIES,
    ) -> httpx.Response | None:
        """Fetch a URL with retries, exponential backoff and jitter.

        Returns the Response on a usable 2xx, or None on exhausted retries,
        block detection, or any transport error. Never raises.
        """
        last_status = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    resp = await client.request(
                        method,
                        url,
                        json=json_body,
                        headers=self._headers(headers),
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.debug(f"Fetch transport error ({url[:80]}): {e}")
                resp = None
            except Exception as e:
                logger.debug(f"Fetch unexpected error ({url[:80]}): {e}")
                return None

            if resp is not None:
                last_status = resp.status_code
                # Retryable server / throttle responses.
                if resp.status_code in (429, 500, 502, 503, 504):
                    logger.debug(
                        f"Fetch got {resp.status_code} for {url[:80]} "
                        f"(attempt {attempt + 1}/{retries})"
                    )
                elif resp.status_code == 200:
                    if self._looks_blocked(resp.text):
                        logger.debug(f"Fetch blocked/challenge page for {url[:80]}")
                        # A challenge won't clear on immediate retry — bail.
                        return None
                    return resp
                else:
                    # 4xx (non-429) — no point retrying.
                    logger.debug(f"Fetch got {resp.status_code} for {url[:80]}")
                    return None

            # Backoff before next attempt.
            if attempt < retries - 1:
                delay = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)

        logger.debug(f"Fetch exhausted retries for {url[:80]} (last={last_status})")
        return None

    @staticmethod
    def _safe_soup(text: str) -> BeautifulSoup | None:
        """Parse HTML without ever raising."""
        try:
            return BeautifulSoup(text, "html.parser")
        except Exception as e:
            logger.debug(f"HTML parse failed: {e}")
            return None

    @staticmethod
    def _attr_str(value) -> str:
        """BS4 attributes can be a str or a list; coerce to a clean string."""
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else ""
        return str(value) if value else ""

    # ── Search backend abstraction ──

    async def _web_search(self, query: str) -> list[tuple[str, str]]:
        """Search the web using the best available backend.

        Returns list of (url, snippet) tuples.
        Tries backends in order: Serper API → DuckDuckGo → Bing → Google.
        """
        if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
            logger.info("Scout: Query limit reached for this cycle. Will continue next cycle.")
            return []

        self._queries_this_cycle += 1

        # Add a random delay between searches to be polite / avoid throttling.
        if self._queries_this_cycle > 1:
            await asyncio.sleep(random.uniform(*SEARCH_DELAY))

        serper_key = getattr(self.env, "serper_api_key", "") or ""
        tavily_key = getattr(self.env, "tavily_api_key", "") or ""
        backends = []
        if serper_key:
            backends.append(lambda: self._search_serper(query, serper_key))
        if tavily_key:
            backends.append(lambda: self._search_tavily(query, tavily_key))
        backends.extend([
            lambda: self._search_duckduckgo(query),
            lambda: self._search_bing(query),
            lambda: self._search_google(query),
        ])

        for backend in backends:
            try:
                results = await backend()
            except Exception as e:
                logger.debug(f"Search backend raised: {e}")
                results = []
            if results:
                return self._dedupe_results(results)

        logger.warning(f"Scout: All search backends failed for: {query[:80]}")
        return []

    @staticmethod
    def _dedupe_results(results: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Drop duplicate URLs while preserving order."""
        seen = set()
        out = []
        for url, snippet in results:
            if not url:
                continue
            norm = url.split("#")[0].rstrip("/")
            if norm in seen:
                continue
            seen.add(norm)
            out.append((url, snippet))
        return out

    async def _search_serper(self, query: str, api_key: str) -> list[tuple[str, str]]:
        """Search via Serper.dev API — most reliable, $5/mo for 2.5k searches."""
        resp = await self._fetch(
            "https://google.serper.dev/search",
            method="POST",
            json_body={"q": query, "num": MAX_RESULTS_PER_QUERY},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            retries=2,
        )
        if resp is None:
            return []
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Serper returned non-JSON: {e}")
            return []
        if not isinstance(data, dict):
            return []

        results = []
        for item in data.get("organic", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("link", "") or ""
            snippet = item.get("snippet", "") or ""
            if url:
                results.append((url, snippet))
        return results[:MAX_RESULTS_PER_QUERY]

    async def _search_tavily(self, query: str, api_key: str) -> list[tuple[str, str]]:
        """Search via Tavily. Used as the second API backend after Serper."""
        resp = await self._fetch(
            "https://api.tavily.com/search",
            method="POST",
            json_body={"query": query, "max_results": MAX_RESULTS_PER_QUERY},
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            retries=2,
        )
        if resp is None:
            return []
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Tavily returned non-JSON: {e}")
            return []
        if not isinstance(data, dict):
            return []

        results = []
        for item in data.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "") or ""
            snippet = item.get("content", "") or ""
            if url:
                results.append((url, snippet))
        return results[:MAX_RESULTS_PER_QUERY]

    async def _search_duckduckgo(self, query: str) -> list[tuple[str, str]]:
        """Search via DuckDuckGo HTML — free, no rate limiting."""
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = await self._fetch(url)
        if resp is None:
            return []

        soup = self._safe_soup(resp.text)
        if soup is None:
            return []

        results = []
        for result in soup.select(".result"):
            link = result.select_one(".result__a")
            snippet_el = result.select_one(".result__snippet")
            if not (link and link.get("href")):
                continue
            href = self._attr_str(link.get("href"))
            # DDG wraps URLs in a redirect — extract the real URL.
            if "uddg=" in href:
                try:
                    params = parse_qs(urlparse(href).query)
                    real = params.get("uddg", [])
                    if real:
                        href = real[0]
                except Exception:
                    pass
            snippet = snippet_el.get_text().strip() if snippet_el else ""
            if href.startswith("http"):
                results.append((href, snippet))

        if results:
            logger.debug(f"DuckDuckGo returned {len(results)} results")
        return results[:MAX_RESULTS_PER_QUERY]

    async def _search_bing(self, query: str) -> list[tuple[str, str]]:
        """Search via Bing HTML scraping — less aggressive than Google."""
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={MAX_RESULTS_PER_QUERY}"
        resp = await self._fetch(url)
        if resp is None:
            return []

        soup = self._safe_soup(resp.text)
        if soup is None:
            return []

        results = []
        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a")
            snippet_el = item.select_one(".b_caption p")
            if not (link and link.get("href")):
                continue
            href = self._attr_str(link.get("href"))
            snippet = snippet_el.get_text().strip() if snippet_el else ""
            if href.startswith("http"):
                results.append((href, snippet))

        if results:
            logger.debug(f"Bing returned {len(results)} results")
        return results[:MAX_RESULTS_PER_QUERY]

    async def _search_google(self, query: str) -> list[tuple[str, str]]:
        """Search via Google HTML scraping — aggressive rate limiting, last resort."""
        url = f"https://www.google.com/search?q={quote_plus(query)}&num={MAX_RESULTS_PER_QUERY}"
        # Google throttles hard; a single quick try avoids burning the cycle.
        resp = await self._fetch(url, retries=1)
        if resp is None:
            return []

        soup = self._safe_soup(resp.text)
        if soup is None:
            return []

        results = []
        for div in soup.select("div.g"):
            link = div.select_one("a")
            snippet_el = div.select_one("div.VwiC3b")
            if not (link and link.get("href")):
                continue
            href = self._attr_str(link.get("href"))
            snippet = snippet_el.get_text() if snippet_el else ""
            if href.startswith("http"):
                results.append((href, snippet))

        if results:
            logger.debug(f"Google returned {len(results)} results")
        return results[:MAX_RESULTS_PER_QUERY]

    # ── Strategy 1: LinkedIn ──

    async def _prospect_via_linkedin(self) -> int:
        """Search LinkedIn for ICP-matching profiles."""
        logger.info("Scout: Searching LinkedIn...")
        linkedin = LinkedInAutomation(
            self.env.linkedin_email, self.env.linkedin_password
        )

        try:
            await linkedin.start()
            if not await linkedin.login():
                logger.warning("Scout: LinkedIn login failed. Skipping.")
                return 0

            count = 0
            for title in self.config.icp.titles:
                for industry in self.config.icp.industries:
                    keywords = f"{title} {industry}"
                    try:
                        profiles = await linkedin.search_people(
                            keywords=keywords, max_results=10
                        )
                    except Exception as e:
                        logger.debug(f"LinkedIn search failed for '{keywords}': {e}")
                        continue

                    for profile in profiles or []:
                        if not isinstance(profile, dict):
                            continue

                        first_name = (profile.get("first_name") or "").strip()
                        last_name = (profile.get("last_name") or "").strip()
                        p_title = (profile.get("title") or title).strip()
                        company_name = (profile.get("company") or "").strip()
                        li_url = (profile.get("linkedin_url") or "").strip()

                        domain = ""
                        company_id = ""
                        if company_name:
                            try:
                                domain = await self._guess_domain(company_name)
                            except Exception:
                                domain = ""
                            if domain:
                                company_id = await self._ensure_company(
                                    name=company_name,
                                    domain=domain,
                                    industry=industry,
                                    source="linkedin_search",
                                )

                        email = ""
                        email_status = ""
                        if domain and first_name and last_name:
                            email, email_status = await self._resolve_email(
                                first_name, last_name, domain
                            )

                        prospect = Prospect(
                            first_name=first_name,
                            last_name=last_name,
                            email=email,
                            email_status=email_status,
                            email_verified=(email_status == "verified"),
                            linkedin_url=li_url,
                            company=company_name,
                            company_id=company_id,
                            title=p_title,
                            seniority=self._infer_seniority(p_title),
                            industry=industry,
                            source="linkedin_search",
                            source_url=li_url,
                        )

                        if not prospect.is_valid():
                            continue
                        if await self._is_duplicate_prospect(prospect):
                            continue

                        await self.state.add_prospect(prospect)
                        self._remember_prospect(prospect)
                        count += 1
                        logger.info(
                            f"Scout: Added prospect {prospect.full_name()} "
                            f"at {company_name}"
                        )
                        if count >= MAX_PROSPECTS_PER_CYCLE:
                            return count

            return count

        finally:
            try:
                await linkedin.stop()
            except Exception as e:
                logger.debug(f"LinkedIn stop failed: {e}")

    # ── Strategy 2: Web search → LinkedIn profiles ──

    async def _prospect_via_profile_search(self) -> int:
        """Find LinkedIn profiles via web search (any backend)."""
        logger.info("Scout: Searching for LinkedIn profiles...")
        count = 0

        geo = self.config.icp.geography[0] if self.config.icp.geography else ""
        industry = self.config.icp.industries[0] if self.config.icp.industries else ""

        for title in self.config.icp.titles:
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            query = f'site:linkedin.com/in "{title}"'
            if industry:
                query += f' "{industry}"'
            if geo:
                query += f' "{geo}"'

            results = await self._web_search(query)

            for url, snippet in results:
                if "/in/" not in url:
                    continue

                parsed = self._parse_linkedin_url(url, snippet)
                if not parsed:
                    continue

                prospect = Prospect(
                    first_name=parsed.get("first_name", ""),
                    last_name=parsed.get("last_name", ""),
                    linkedin_url=url,
                    title=title,
                    seniority=self._infer_seniority(title),
                    industry=industry,
                    source="web_search",
                    source_url=url,
                )

                if not prospect.is_valid():
                    continue
                if await self._is_duplicate_prospect(prospect):
                    continue

                await self.state.add_prospect(prospect)
                self._remember_prospect(prospect)
                count += 1

                if count >= 15:
                    return count

        return count

    # ── Strategy 3: Find companies, then scrape team pages ──

    ENABLE_PROFILE_SEARCH = False
    KNOWN_COMPANY_BATCH = 25  # one Reoon credit each per cycle (4 cycles/hour)
    # SERP and directory scraping produced only expo exhibitor lists, startup
    # aggregators and cookie-policy pages once Google Maps was wired in
    # (9 of 9 rows were junk). Listings discovery is the source now.
    ENABLE_WEB_SEARCH = False
    ENABLE_DIRECTORY_SEARCH = False
    REVERIFY_GUESSES_PER_CYCLE = 5

    # Per-cycle verifier credits, filled by the sweep's balance check and
    # decremented as addresses are verified: {"reoon": n, "hunter": m}.
    _verifier_credits: dict = {}
    HUNTER_PER_CYCLE = 10   # spread the small monthly allowance over the day

    async def _verify_address(self, email: str) -> str:
        """verified / risky / invalid, or 'guess' when nobody could tell.

        Reoon first (one retry on 'unknown', common with slow .com.do
        hosts), then Hunter when Reoon has no credits left.
        """
        import asyncio
        from mercury.integrations.email_finder import (
            _status_from_verdict, verify_hunter, verify_reoon, verify_treg,
        )

        credits = self._verifier_credits
        reoon_key = getattr(self.env, "reoon_api_key", "") or ""
        hunter_key = getattr(self.env, "hunter_api_key", "") or ""
        treg_token = getattr(self.env, "treg_token", "") or ""
        status = ""
        if treg_token and credits.get("treg", 1) > 0:
            # One prepaid balance, ~$0.0015 per address. VerifierExhausted
            # propagates so the sweep stops instead of minting guesses.
            verdict = await verify_treg(email, treg_token)
            status = (_status_from_verdict(verdict) if verdict else None) or ""
            if verdict is not None and verdict.get("status") == "unknown":
                status = ""   # let a second opinion try when one has credits
        if not status and reoon_key and credits.get("reoon", 1) > 0:
            try:
                verdict = await verify_reoon(email, reoon_key)
                credits["reoon"] = credits.get("reoon", 1) - 1
                status = (_status_from_verdict(verdict) if verdict else None) or ""
                if not status and credits.get("reoon", 1) > 0:
                    await asyncio.sleep(4)
                    verdict = await verify_reoon(email, reoon_key)
                    credits["reoon"] = credits.get("reoon", 1) - 1
                    status = (_status_from_verdict(verdict) if verdict else None) or ""
            except Exception as e:
                logger.debug(f"Reoon verify failed for {email}: {e}")
        if not status and hunter_key and credits.get("hunter", 0) > 0:
            try:
                verdict = await verify_hunter(email, hunter_key)
                credits["hunter"] = credits.get("hunter", 0) - 1
                status = (_status_from_verdict(verdict) if verdict else None) or ""
                if status:
                    logger.info(f"Scout: Hunter verified {email} -> {status}")
            except Exception as e:
                logger.debug(f"Hunter verify failed for {email}: {e}")
        return status or "guess"

    async def _reverify_guesses(self) -> int:
        """Re-check a few 'guess' addresses.

        Reoon answers 'unknown' when a mail server is slow or greylisting
        (common for .com.do hosts); a guess was otherwise permanent, so the
        prospect could never become writable.
        """
        import aiosqlite

        try:
            async with aiosqlite.connect(self.state.db_path) as db:
                db.row_factory = aiosqlite.Row
                # Newest first: today's discoveries are the market we are
                # working right now, the old leftovers can wait.
                rows = await (await db.execute(
                    """SELECT id, email FROM prospects
                       WHERE status = 'new' AND email_status = 'guess' AND email != ''
                         AND replace(updated_at, 'T', ' ') < datetime('now', '-6 hours')
                       ORDER BY created_at DESC LIMIT ?""",
                    (self.REVERIFY_GUESSES_PER_CYCLE,),
                )).fetchall()
        except Exception as e:
            logger.debug(f"Could not list guessed prospects: {e}")
            return 0
        from mercury.integrations.email_finder import VerifierExhausted

        changed = 0
        for row in rows:
            try:
                status = await self._verify_address(row["email"])
            except VerifierExhausted as e:
                logger.warning(f"Scout: verifier exhausted during re-verify ({e}); stopping.")
                break
            # Touch the row either way so the same five are not retried every cycle.
            await self.state.update_prospect_email(row["id"], row["email"], status)
            if status != "guess":
                changed += 1
                logger.info(f"Scout: re-verified {row['email']} -> {status}")
        return changed

    async def _prospects_from_known_companies(self) -> int:
        """Give contacts to companies that were discovered but never worked.

        The dashboard's discovery run writes companies straight to the
        database, so they never pass through the prospecting path and sit
        there forever with a domain and nobody to write to. This sweeps them
        up on every cycle, which is what makes the loop actually autonomous.
        """
        import aiosqlite
        from mercury.collectors.discover import is_junk
        from mercury.integrations.email_finder import (
            VerifierExhausted, hunter_verifications_left, reoon_balance,
        )

        # Every address found costs one verifier credit. With none left every
        # company in the batch would turn into a 'guess' prospect (never
        # written to, never re-swept), so size the batch to what is left and
        # hold when both accounts are empty. Reoon is the main verifier;
        # Hunter's monthly allowance is the reserve.
        reoon_key = getattr(self.env, "reoon_api_key", "") or ""
        hunter_key = getattr(self.env, "hunter_api_key", "") or ""
        reoon_left = await reoon_balance(reoon_key) if reoon_key else 0
        hunter_left = await hunter_verifications_left(hunter_key) if hunter_key else 0
        reoon_left = self.KNOWN_COMPANY_BATCH if reoon_left is None else max(reoon_left, 0)
        hunter_left = 0 if hunter_left is None else min(max(hunter_left, 0), self.HUNTER_PER_CYCLE)
        # treg has no balance endpoint we can read; it answers 402/429 when
        # empty and _verify_address raises VerifierExhausted, which ends the
        # batch below. Until then it carries the whole batch.
        treg_left = self.KNOWN_COMPANY_BATCH if getattr(self.env, "treg_token", "") else 0
        self._verifier_credits = {"treg": treg_left, "reoon": reoon_left, "hunter": hunter_left}
        batch = min(self.KNOWN_COMPANY_BATCH, treg_left + reoon_left + hunter_left)
        if batch <= 0:
            logger.warning(
                "Scout: no verifier credits left (Reoon 0, Hunter 0) — inbox sweep "
                "holding until Reoon is topped up (emailverifier.reoon.com)."
            )
            return 0
        if batch < self.KNOWN_COMPANY_BATCH:
            logger.warning(
                f"Scout: verifier credits low (Reoon {reoon_left}, Hunter {hunter_left} "
                f"this cycle) — sweeping {batch} companies instead of "
                f"{self.KNOWN_COMPANY_BATCH}."
            )
        if treg_left or reoon_left + hunter_left > batch + self.REVERIFY_GUESSES_PER_CYCLE:
            try:
                await self._reverify_guesses()
            except Exception as e:
                logger.debug(f"Re-verify pass failed: {e}")

        try:
            async with aiosqlite.connect(self.state.db_path) as db:
                db.row_factory = aiosqlite.Row
                # Markets in the order the ICP lists them (Dominican Republic
                # before Florida today), newest first within a market, so a
                # fresh discovery run is worked before older leftovers.
                places = [p for m in (getattr(self.config.icp, "markets", None) or [])
                          for p in m.places]
                if places:
                    case = " ".join(f"WHEN location LIKE ? THEN {i}"
                                    for i, _ in enumerate(places))
                    order = f"ORDER BY CASE {case} ELSE {len(places)} END, created_at DESC"
                else:
                    order = "ORDER BY created_at DESC"
                rows = await (await db.execute(
                    f"""SELECT id, name, domain, industry FROM companies
                       WHERE domain != ''
                         AND NOT EXISTS (SELECT 1 FROM prospects p
                                         WHERE p.company_id = companies.id)
                         AND NOT EXISTS (SELECT 1 FROM observations o
                                         WHERE o.company_id = companies.id
                                           AND o.signal_code = 'NO_PUBLIC_INBOX'
                                           AND o.observed_at > datetime('now', '-14 days'))
                       {order}
                       LIMIT ?""",
                    [f"%{p}%" for p in places] + [batch],
                )).fetchall()
        except Exception as e:
            logger.debug(f"Could not list unworked companies: {e}")
            return 0

        if not rows:
            return 0

        logger.info(f"Scout: {len(rows)} discovered companies have no contact yet.")
        added = 0
        for row in rows:
            if is_junk(row["domain"], row["name"] or ""):
                # Mark it so the batch never re-reads an aggregator row.
                try:
                    await self.state.add_observations([{
                        "signal_code": "NO_PUBLIC_INBOX", "company_id": row["id"],
                        "collector": "sweep", "value_num": 1.0, "value_text": "junk",
                        "confidence": 1.0, "evidence_url": f"https://{row['domain']}",
                    }])
                except Exception:
                    pass
                continue
            try:
                prospect = await self._contact_from_public_inbox(
                    company_id=row["id"],
                    domain=row["domain"],
                    company_name=row["name"] or self._domain_to_name(row["domain"]),
                    industry=row["industry"] or "",
                )
            except VerifierExhausted as e:
                logger.warning(
                    f"Scout: verifier exhausted ({e}); inbox sweep stops for this "
                    "cycle so no company is turned into a guess. Top up treg.to."
                )
                break
            except Exception as e:
                logger.debug(f"Inbox sweep failed for {row['domain']}: {e}")
                continue
            if not prospect:
                # A miss is an observation too: without it the batch re-reads
                # the same ten inbox-less companies every cycle and never
                # reaches the ones that do publish an address.
                try:
                    await self.state.add_observations([{
                        "signal_code": "NO_PUBLIC_INBOX", "company_id": row["id"],
                        "collector": "sweep", "value_num": 1.0, "value_text": "",
                        "confidence": 1.0, "evidence_url": f"https://{row['domain']}",
                    }])
                except Exception as e:
                    logger.debug(f"Could not record miss for {row['domain']}: {e}")
                continue
            try:
                await self.state.add_prospect(prospect)
                added += 1
                logger.info(
                    f"Scout: {prospect.email} at {row['domain']} "
                    f"({prospect.email_status})"
                )
            except Exception as e:
                logger.debug(f"add_prospect failed for {prospect.email}: {e}")
        return added

    async def _prospect_via_company_discovery(self) -> int:
        """Find companies via multiple methods, scrape team pages for contacts.

        Python does ALL the searching and scraping. Claude only scores/personalizes
        the contacts that Python already found.
        """
        # Work what we already have before spending searches on more.
        swept = await self._prospects_from_known_companies()

        logger.info("Scout: Discovering companies...")

        companies = []
        if self.ENABLE_WEB_SEARCH:
            try:
                companies.extend(await self._find_companies_via_search())
            except Exception as e:
                logger.debug(f"Company search failed: {e}")
        if self.ENABLE_DIRECTORY_SEARCH:
            try:
                companies.extend(await self._find_companies_via_directories())
            except Exception as e:
                logger.debug(f"Directory discovery failed: {e}")

        if not companies:
            logger.info("Scout: No new companies found this cycle.")
            return swept

        # Deduplicate by domain (defensive against missing keys).
        seen = set()
        unique = []
        for c in companies:
            domain = (c.get("domain") or "").lower()
            if not domain or domain in seen:
                continue
            seen.add(domain)
            unique.append(c)
        companies = unique[:MAX_COMPANIES_PER_CYCLE]

        logger.info(f"Scout: Found {len(companies)} candidate companies.")

        default_industry = (
            self.config.icp.industries[0] if self.config.icp.industries else ""
        )

        all_contacts = []
        for company in companies:
            domain = company["domain"]
            company_id = await self._ensure_company(
                name=company.get("name") or self._domain_to_name(domain),
                domain=domain,
                website=company.get("website", f"https://{domain}"),
                description=company.get("description", ""),
                industry=company.get("industry", default_industry),
                source=company.get("source", "web_search"),
                source_url=company.get("source_url", ""),
            )

            # Buying signals: what they run, and whether they're hiring for
            # roles that suggest they're in-market. Signals are gold for
            # personalization — reference something true and current.
            signal_note = ""
            try:
                signal_note = await self._enrich_company_signals(
                    company_id, domain, homepage_html=company.get("_homepage_html", "")
                )
            except Exception as e:
                logger.debug(f"Signal enrichment failed for {domain}: {e}")

            contacts = await self._contacts_from_company(
                company_id=company_id,
                domain=domain,
                company_name=company.get("name") or self._domain_to_name(domain),
                industry=company.get("industry", default_industry),
                signal_note=signal_note,
                source="company_website",
            )
            # A plumber has no /our-team page: the whole company is two vans and
            # one shared inbox printed on the homepage. Falling through here is
            # what made every local-business run return zero contacts.
            if not contacts:
                fallback = await self._contact_from_public_inbox(
                    company_id=company_id,
                    domain=domain,
                    company_name=company.get("name") or self._domain_to_name(domain),
                    industry=company.get("industry", default_industry),
                    signal_note=signal_note,
                )
                if fallback:
                    contacts = [fallback]
            all_contacts.extend(contacts)

        if not all_contacts:
            logger.info("Scout: No ICP-matching contacts found on team pages.")
            return 0

        # Use Claude to score/personalize, with a Python heuristic fallback.
        scored_contacts = await self._score_contacts(all_contacts)

        count = 0
        for prospect in scored_contacts:
            await self.state.add_prospect(prospect)
            count += 1
            logger.info(
                f"Scout: Added {prospect.full_name()} ({prospect.title}) "
                f"at {prospect.company} [score: {prospect.score}]"
            )
            if count >= MAX_PROSPECTS_PER_CYCLE:
                break

        return count

    async def _find_companies_via_search(self) -> list[dict]:
        """Find ICP-matching companies via web search."""
        companies = []

        for query in self._build_company_search_queries():
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            results = await self._web_search(query)

            for url, snippet in results:
                domain = self._extract_domain(url)
                if not self._is_candidate_domain(domain):
                    continue
                if await self.state.company_exists(domain):
                    self._seen_domains.add(domain)
                    continue

                self._seen_domains.add(domain)

                info = await self._scrape_company_info(domain)
                info["domain"] = domain
                info["source"] = "web_search"
                info["source_url"] = url
                if not info.get("name"):
                    info["name"] = self._domain_to_name(domain)

                companies.append(info)
                if len(companies) >= 5:
                    return companies

        return companies

    async def _find_companies_via_directories(self) -> list[dict]:
        """Scrape industry directory sites for company names and domains.

        These sites list companies by category and are much more reliable
        than Google for finding ICP-matching companies.
        """
        companies = []

        industries = self.config.icp.industries or []
        if not industries:
            return []

        directory_queries = []
        for industry in industries[:2]:
            directory_queries.extend([
                f'site:g2.com/categories "{industry}"',
                f'site:clutch.co "{industry}" companies',
                f'"{industry}" company directory list',
            ])

        for query in directory_queries:
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            results = await self._web_search(query)

            for url, snippet in results:
                for domain in self._extract_company_domains_from_snippet(snippet, url):
                    if not self._is_candidate_domain(domain):
                        continue
                    if await self.state.company_exists(domain):
                        self._seen_domains.add(domain)
                        continue

                    self._seen_domains.add(domain)
                    info = await self._scrape_company_info(domain)
                    info["domain"] = domain
                    info["source"] = "directory"
                    info["source_url"] = url
                    if not info.get("name"):
                        info["name"] = self._domain_to_name(domain)

                    companies.append(info)
                    if len(companies) >= 5:
                        return companies

                # Also scrape the directory page itself for company links.
                if len(companies) < 5:
                    for pc in await self._scrape_directory_page(url):
                        pd = pc.get("domain", "")
                        if not self._is_candidate_domain(pd):
                            continue
                        if await self.state.company_exists(pd):
                            self._seen_domains.add(pd)
                            continue
                        self._seen_domains.add(pd)
                        companies.append(pc)
                        if len(companies) >= 5:
                            return companies

        return companies

    def _is_candidate_domain(self, domain: str) -> bool:
        """A domain worth pursuing: real, unseen, not noise."""
        if not domain or len(domain) < 4:
            return False
        if domain in self._seen_domains:
            return False
        if self._is_noise_domain(domain):
            return False
        return True

    async def _scrape_directory_page(self, url: str) -> list[dict]:
        """Scrape a directory/list page for company links."""
        companies = []
        resp = await self._fetch(url, timeout=SCRAPE_TIMEOUT, retries=2)
        if resp is None:
            return []

        soup = self._safe_soup(resp.text)
        if soup is None:
            return []

        page_domain = self._extract_domain(url)
        seen_here = set()

        try:
            for link in soup.find_all("a", href=True):
                href = self._attr_str(link.get("href"))
                if not href.startswith("http"):
                    continue

                domain = self._extract_domain(href)
                if not domain or self._is_noise_domain(domain):
                    continue
                if domain == page_domain or domain in seen_here:
                    continue

                link_text = link.get_text().strip()
                if 3 < len(link_text) < 60:
                    seen_here.add(domain)
                    companies.append({
                        "name": link_text,
                        "domain": domain,
                        "website": f"https://{domain}",
                        "source": "directory",
                        "source_url": url,
                    })

                if len(companies) >= 10:
                    break
        except Exception as e:
            logger.debug(f"Failed to parse directory page {url}: {e}")

        return companies

    def _extract_company_domains_from_snippet(self, snippet: str, source_url: str) -> list[str]:
        """Extract potential company domains mentioned in a search snippet."""
        if not snippet:
            return []
        domains = []
        try:
            found = re.findall(
                r'\b([a-zA-Z0-9][a-zA-Z0-9-]*\.(?:com|io|co|net|org|ai))\b', snippet
            )
        except Exception:
            return []
        for d in found:
            d = d.lower()
            if not self._is_noise_domain(d) and len(d) > 5:
                domains.append(d)
        # Preserve order, drop dupes.
        seen = set()
        return [d for d in domains if not (d in seen or seen.add(d))][:5]

    def _build_company_search_queries(self) -> list[str]:
        """Build search queries to find ICP-matching companies."""
        queries = []
        industries = self.config.icp.industries or [""]
        geos = self.config.icp.geography or [""]
        size = self.config.icp.company_size or ""

        for industry in industries[:2]:
            geo = geos[0] if geos else ""

            parts = []
            if industry:
                parts.append(f'"{industry}"')
            if geo:
                parts.append(f'"{geo}"')
            if size:
                parts.append(f'"{size}"')
            parts.append("company")
            queries.append(" ".join(parts))

            if geo and industry:
                queries.append(f'top "{industry}" companies {geo}')

        # Drop empties / dupes, cap to budget.
        seen = set()
        out = []
        for q in queries:
            q = q.strip()
            if q and q != "company" and q not in seen:
                seen.add(q)
                out.append(q)
        return out[:3]

    async def _resolve_email(
        self, first_name: str, last_name: str, domain: str
    ) -> tuple[str, str]:
        """Find an email and return (address, honest_status).

        Status is one of verified / risky / guess / invalid — the pipeline
        never reports a mere pattern guess as verified (that old bug bounced
        mail and burned domains). Passes env (verifier keys) and state (the
        pattern cache) so the domain's pattern is learned once and reused.
        """
        if not (first_name and last_name and domain):
            return "", "invalid"
        try:
            result = await find_email(
                first_name, last_name, domain,
                verify=True, env=self.env, state=self.state,
            )
        except Exception as e:
            logger.debug(f"find_email failed for {domain}: {e}")
            return "", "invalid"
        return result.email, result.status

    async def _score_contacts(self, contacts: list[Prospect]) -> list[Prospect]:
        """Use Claude to score and add personalization notes to found contacts.

        Falls back to a deterministic Python heuristic if Claude is
        unavailable or returns malformed output, so scoring never blocks
        prospecting.
        """
        if not contacts:
            return contacts

        # Seed every contact with a heuristic baseline first.
        for c in contacts:
            c.score = self._heuristic_score(c)

        contact_summaries = []
        for i, c in enumerate(contacts):
            contact_summaries.append(
                f"{i+1}. {c.full_name()} — {c.title} at {c.company} "
                f"({c.industry}). Email: {c.email or 'none'}. "
                f"Seniority: {c.seniority or 'unknown'}."
            )

        prompt = f"""You are analyzing a batch of sales prospects that have already been found and verified.
Your job is to score each one (1-100) based on how well they match the ICP, and add a short personalization note.

Our product: {self.config.product.description}
Target industries: {', '.join(self.config.icp.industries)}
Target titles: {', '.join(self.config.icp.titles)}
Target company size: {self.config.icp.company_size}

Here are the prospects to score:
{chr(10).join(contact_summaries)}

Return a JSON array with one object per prospect, in the same order:
[{{"index": 1, "score": 75, "personalization": "Short angle for outreach"}}, ...]

Score criteria:
- 80-100: Perfect ICP match, right title, right industry, right company size
- 60-79: Good match, close to ICP
- 40-59: Partial match, might be worth a shot
- 1-39: Poor match, probably skip

Respond ONLY with the JSON array."""

        try:
            result = await self.brain.think_json(
                prompt, session_id="mercury-scout-score",
                agent="scout", task="score_contacts",
            )
        except Exception as e:
            logger.warning(f"Scout: Claude scoring errored ({e}); using heuristic scores.")
            result = None

        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index", 0)) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(contacts):
                    try:
                        score = int(item.get("score", contacts[idx].score))
                    except (TypeError, ValueError):
                        score = contacts[idx].score
                    contacts[idx].score = max(1, min(100, score))
                    note = item.get("personalization", "")
                    if isinstance(note, str):
                        contacts[idx].personalization_notes = note.strip()[:500]
        else:
            logger.warning("Scout: Claude scoring failed, using heuristic scores.")

        contacts = [c for c in contacts if c.score >= 30]
        contacts.sort(key=lambda c: c.score, reverse=True)
        return contacts

    def _heuristic_score(self, contact: Prospect) -> int:
        """Deterministic ICP fit score used as a fallback / baseline."""
        score = 40

        # Title / seniority fit.
        if self._title_matches_icp(contact.title):
            score += 25
        seniority_bonus = {
            "c_suite": 20, "vp": 15, "director": 10, "manager": 5, "individual": 0,
        }
        score += seniority_bonus.get(contact.seniority, 0)

        # Industry fit.
        if contact.industry and self.config.icp.industries:
            if any(
                ind.lower() in contact.industry.lower()
                or contact.industry.lower() in ind.lower()
                for ind in self.config.icp.industries
            ):
                score += 10

        # Deliverability: reward confidence in the address.
        if contact.email_status == "verified":
            score += 8
        elif contact.email_status == "risky":
            score += 3
        elif contact.email:
            score += 1

        # Timely buying signals (hiring for a relevant role right now)
        # outperform static fit — reward them strongly.
        if "Signal: hiring" in (contact.personalization_notes or ""):
            score += 10

        return max(1, min(100, score))

    # ── Shared utilities ──

    async def _ensure_company(
        self,
        name: str,
        domain: str,
        website: str = "",
        description: str = "",
        industry: str = "",
        company_size: str = "",
        location: str = "",
        source: str = "",
        source_url: str = "",
    ) -> str:
        """Get or create a company record. Returns company_id."""
        try:
            existing = await self.state.get_company_by_domain(domain)
            if existing:
                return existing.id
        except Exception as e:
            logger.debug(f"get_company_by_domain failed for {domain}: {e}")

        company = Company(
            name=name,
            domain=domain,
            website=website or f"https://{domain}",
            description=description,
            industry=industry,
            company_size=company_size,
            location=location,
            source=source,
            source_url=source_url,
        )
        return await self.state.add_company(company)

    async def _scrape_company_info(self, domain: str) -> dict:
        """Scrape a company's homepage for basic info. Pure Python."""
        info = {"name": "", "description": "", "website": f"https://{domain}"}

        resp = await self._fetch(f"https://{domain}", timeout=SCRAPE_TIMEOUT, retries=2)
        if resp is None:
            return info

        # Keep the raw HTML so signal detection reuses this fetch.
        info["_homepage_html"] = resp.text

        soup = self._safe_soup(resp.text)
        if soup is None:
            return info

        try:
            title_tag = soup.find("title")
            if title_tag:
                title_text = title_tag.get_text().strip()
                # Split on common separators to isolate the brand name.
                name = re.split(r"[|—–\-:]", title_text)[0].strip()
                if name:
                    info["name"] = name[:120]

            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                info["description"] = self._attr_str(meta_desc.get("content")).strip()[:500]

            og_name = soup.find("meta", attrs={"property": "og:site_name"})
            if og_name and og_name.get("content"):
                og = self._attr_str(og_name.get("content")).strip()
                if og:
                    info["name"] = og[:120]

            if not info["description"]:
                og_desc = soup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    info["description"] = self._attr_str(og_desc.get("content")).strip()[:500]
        except Exception as e:
            logger.debug(f"Failed to parse company info for {domain}: {e}")

        return info

    async def _scrape_team_page(self, domain: str) -> list[dict]:
        """Try to find and scrape a company's team/about page."""
        team_paths = ["/team", "/about", "/about-us", "/our-team", "/people",
                      "/leadership", "/about/team", "/company/team", "/staff"]
        members = []

        for path in team_paths:
            url = f"https://{domain}{path}"
            resp = await self._fetch(url, timeout=SCRAPE_TIMEOUT, retries=1)
            if resp is None:
                continue

            soup = self._safe_soup(resp.text)
            if soup is None:
                continue

            # Harvest any real address on the page — one confirmed email
            # reveals the domain's whole pattern (learned once, reused free).
            await self._learn_pattern_from_page(soup, resp.text, domain)

            try:
                cards = soup.select(
                    ".team-member, .person, .staff, [class*='team'], "
                    "[class*='leadership'], [class*='member'], [class*='person']"
                )
            except Exception:
                cards = []

            for card in cards:
                name_el = card.select_one("h2, h3, h4, .name, [class*='name']")
                title_el = card.select_one(
                    "p, .title, .role, .position, "
                    "[class*='title'], [class*='role'], [class*='position']"
                )
                if not name_el:
                    continue

                name = re.sub(r"\s+", " ", name_el.get_text()).strip()
                title = ""
                if title_el:
                    title = re.sub(r"\s+", " ", title_el.get_text()).strip()

                # Reject obvious non-names (too long/short, digits, symbols).
                if not (3 <= len(name) <= 50):
                    continue
                if any(ch.isdigit() for ch in name):
                    continue
                if not re.match(r"^[A-Za-z][A-Za-z.'\- ]+$", name):
                    continue

                parts = name.split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    continue

                members.append({
                    "first_name": parts[0].strip(),
                    "last_name": parts[1].strip(),
                    "title": title[:120],
                })

            if members:
                break

        # Dedup members by (first,last).
        seen = set()
        unique = []
        for m in members:
            key = (m["first_name"].lower(), m["last_name"].lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        return unique

    async def _note_from_observations(self, company_id: str) -> str:
        """Turn collected signals into an opening line the Writer can use.

        The prompt already demands a verifiable detail and calls the
        personalization notes "the hook". Signals were being written to the
        database and never handed over, so the Writer had nothing to open with
        and fell back to observations about the trade. This closes that gap.
        """
        try:
            rows = await self.state.get_observations(
                company_id=company_id, latest_only=True, limit=60
            )
        except Exception as e:
            logger.debug(f"Could not read observations for {company_id}: {e}")
            return ""

        num, text = {}, {}
        for r in rows:
            code = r.get("signal_code") or ""
            if r.get("value_num") is not None:
                num[code] = r["value_num"]
            if r.get("value_text"):
                text[code] = r["value_text"]

        facts: list[str] = []
        ads_google = num.get("RUNNING_GOOGLE_ADS") == 1.0
        ads_meta = num.get("RUNNING_META_ADS") == 1.0
        if ads_google and ads_meta:
            facts.append("pays for both Google Ads and Meta Ads")
        elif ads_google:
            facts.append("pays for Google Ads")
        elif ads_meta:
            facts.append("pays for Meta Ads")

        if num.get("NO_WEBSITE") == 1.0:
            facts.append("has no website at all, only a listing")
        if num.get("NO_ONLINE_BOOKING") == 1.0:
            # Saying "no way to reach them" when a quote form sits on the
            # homepage is a lie the reader can check in one click, and it
            # costs the whole email. The true gap is self-serve scheduling:
            # a form still has to be read, triaged and called back.
            if text.get("CONTACT_FORM_URL"):
                facts.append("the only way in is a quote form someone has to "
                             "read and call back, no self-serve scheduling")
            else:
                facts.append("no form and no booking on the site, so every job "
                             "starts as a phone call")
        if text.get("INCUMBENT_AGENCY"):
            facts.append(f"already pays an agency ({text['INCUMBENT_AGENCY']})")
        if text.get("TECH_STACK"):
            facts.append(f"site runs on {text['TECH_STACK']}")
        if num.get("BLOG_STALE") == 1.0:
            facts.append("blog abandoned")
        if num.get("UNCLAIMED_LISTING") == 1.0:
            facts.append("Google listing still unclaimed")
        pages = num.get("SITE_PAGE_COUNT")
        if pages and pages > 100:
            facts.append(f"{int(pages)} pages indexed, so the site was a real "
                         "investment at some point")
        rating, reviews = num.get("REVIEW_RATING"), num.get("REVIEW_COUNT")
        if reviews:
            # Phrased as qualification, not as a hook: opening with "your 72
            # reviews" was every Dominican draft's first line.
            facts.append(f"established and busy ({int(reviews)} Google reviews"
                         + (f", {rating}" if rating else "")
                         + ") — context only, never the opener")
        return ". ".join(facts)

    PUBLIC_INBOX_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us")
    _INBOX_JUNK = ("sentry", "wixpress", "example.com", "godaddy", "@2x",
                   ".png", ".jpg", ".gif", "yourdomain", "domain.com")
    _ROLE_LOCALPARTS = {"info", "contact", "hello", "sales", "office", "admin",
                        "service", "support", "inquiry", "inquiries", "estimates",
                        "quotes", "help", "team", "mail", "contacto", "ventas"}

    async def _contact_from_public_inbox(
        self,
        *,
        company_id: str,
        domain: str,
        company_name: str,
        industry: str,
        signal_note: str = "",
    ) -> "Prospect | None":
        """Build one contact from the inbox a small business puts on its site.

        Team-page scraping assumes a company with an About page and named
        staff. Local trades have neither, so the address on the homepage is
        the only contact that exists — and it verifies like any other.
        """
        import re as _re
        from mercury.integrations.email_finder import (
            verify_reoon, _status_from_verdict,
        )

        pattern = _re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
        found: list[str] = []
        for path in self.PUBLIC_INBOX_PATHS:
            resp = await self._fetch(f"https://{domain}{path}", timeout=10, retries=1)
            if resp is None:
                continue
            page = resp.text or ""
            # Addresses inside inline JSON arrive as "info\\u0040x.do" or
            # "...\\u002f", and the escape's tail was being read as a local
            # part (u002f@sirena.do). Decode \\uXXXX before matching.
            try:
                page = _re.sub(r"\\u([0-9a-fA-F]{4})",
                               lambda m: chr(int(m.group(1), 16)), page)
            except (ValueError, OverflowError):
                pass
            # Cloudflare hides addresses as data-cfemail="<hex>": the first
            # byte is an XOR key for the rest. Decode them so a protected
            # inbox counts like a plain one.
            for enc in _re.findall(r'data-cfemail="([0-9a-fA-F]+)"', page):
                try:
                    key = int(enc[:2], 16)
                    page += " " + "".join(chr(int(enc[i:i+2], 16) ^ key)
                                         for i in range(2, len(enc), 2))
                except ValueError:
                    pass
            for hit in pattern.findall(page):
                low = hit.lower()
                if len(low) > 60 or any(j in low for j in self._INBOX_JUNK):
                    continue
                if low not in found:
                    found.append(low)
            if found:
                break
        if not found:
            return None

        # An address on a third-party domain is almost always the web
        # developer or the agency in the footer, and writing to them is worse
        # than writing to nobody. Keep only the company's own domain or a
        # consumer mailbox, which is what small trades actually use.
        bare = domain.lower().replace("www.", "")
        root = bare[4:] if bare.startswith("www.") else bare
        CONSUMER = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                    "live.com", "aol.com", "icloud.com", "msn.com")
        # These prefixes are what ESPs and Spamhaus actually block, unlike
        # info@ or sales@, which in a fifteen-person trade is the owner's own
        # inbox. Never write to them.
        BLOCKED = ("abuse", "postmaster", "admin", "root", "noreply", "no-reply",
                   "security", "webmaster", "hostmaster", "mailer-daemon", "spam",
                   "privacy", "billing", "compliance", "unsubscribe")
        keep = []
        for e in found:
            if e.split("@", 1)[0].lower() in BLOCKED:
                continue
            host = e.split("@", 1)[1]
            if host == root or host.endswith("." + root) or root.endswith("." + host):
                keep.append((0, e))
            elif host in CONSUMER:
                keep.append((1, e))
        if not keep:
            logger.debug(f"Scout: {domain} only exposed third-party inboxes")
            return None
        keep.sort(key=lambda pair: (pair[0], len(pair[1])))
        email = keep[0][1]
        local = email.split("@", 1)[0]

        # A personal local part gives a real first name; a role inbox does not,
        # so the business itself becomes the addressee rather than inventing one.
        clean = _re.sub(r"[^a-zA-Z]", " ", local).strip()
        if clean.lower() in self._ROLE_LOCALPARTS or len(clean) < 3:
            # "Hi Pine," for Pine Island Nursery was the greeting in sent
            # mail. The addressee is the business, minus its legal suffix;
            # the writer knows not to greet a business by first name.
            biz = _re.sub(
                r"\b(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Co\.?|Ltd\.?|S\.?R\.?L\.?|"
                r"EIRL|E\.I\.R\.L\.?|S\.?A\.?|C\. ?por ?A\.?)\s*$",
                "", (company_name or bare).strip(), flags=_re.I,
            ).strip(" ,.-")
            first_name = (biz or "Owner")[:60]
            last_name = "Team"
            title = "Owner"
        else:
            parts = clean.split()
            first_name = parts[0].title()
            last_name = (parts[1].title() if len(parts) > 1 else "Team")
            title = "Owner"

        status = await self._verify_address(email)

        # Hand the Writer something true to open with, not a trade cliche.
        if not signal_note:
            signal_note = await self._note_from_observations(company_id)

        logger.info(
            f"Scout: public inbox {email} at {domain} -> {status}"
            + (f" | hook: {signal_note[:60]}" if signal_note else " | no hook")
        )
        return Prospect(
            company_id=company_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            email_status=status,
            email_verified=(status == "verified"),
            title=title,
            seniority="c_suite",
            source="public_inbox",
            source_url=f"https://{domain}",
            company=company_name,
            industry=industry,
            personalization_notes=signal_note,
        )

    async def _contacts_from_company(
        self,
        company_id: str,
        domain: str,
        company_name: str,
        industry: str,
        signal_note: str = "",
        source: str = "company_website",
    ) -> list[Prospect]:
        """Team-page scrape → ICP-matched, email-resolved, deduped prospects."""
        try:
            team_members = await self._scrape_team_page(domain)
        except Exception as e:
            logger.debug(f"Team scrape failed for {domain}: {e}")
            return []

        contacts: list[Prospect] = []
        for member in team_members:
            title = member.get("title", "")
            if not self._title_matches_icp(title):
                continue

            first_name = member.get("first_name", "")
            last_name = member.get("last_name", "")
            if not first_name or not last_name:
                continue

            email, email_status = await self._resolve_email(
                first_name, last_name, domain
            )

            prospect = Prospect(
                first_name=first_name,
                last_name=last_name,
                email=email,
                email_status=email_status,
                email_verified=(email_status == "verified"),
                company=company_name,
                company_id=company_id,
                title=title,
                seniority=self._infer_seniority(title),
                industry=industry,
                source=source,
                source_url=f"https://{domain}",
                personalization_notes=signal_note,
            )

            if not prospect.is_valid():
                continue
            if await self._is_duplicate_prospect(prospect):
                continue

            self._remember_prospect(prospect)
            contacts.append(prospect)
        return contacts

    # ── Strategy 4: Job boards — companies hiring for signal roles ──

    async def _prospect_via_hiring_boards(self) -> int:
        """Discover companies via job postings — the strongest intent signal.

        A company actively hiring a relevant role is in-market *right now*.
        Uses python-jobspy when installed (optional dependency:
        `pip install python-jobspy`); silently skipped otherwise.
        """
        try:
            from jobspy import scrape_jobs
        except ImportError:
            logger.debug("Scout: python-jobspy not installed; skipping job boards.")
            return 0

        keywords = self._signal_role_keywords()
        if not keywords:
            return 0
        search_term = keywords[0]
        geo = self.config.icp.geography[0] if self.config.icp.geography else ""

        logger.info(f"Scout: Searching job boards for '{search_term}' roles...")
        loop = asyncio.get_event_loop()

        def _search():
            return scrape_jobs(
                site_name=["indeed"],
                search_term=search_term,
                location=geo,
                results_wanted=15,
                hours_old=24 * 14,  # fresh signal only: last two weeks
            )

        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, _search), timeout=120
            )
        except Exception as e:
            logger.warning(f"Scout: job-board search failed: {e}")
            return 0
        if df is None or getattr(df, "empty", True):
            return 0

        default_industry = (
            self.config.icp.industries[0] if self.config.icp.industries else ""
        )
        today = datetime.now(timezone.utc).date().isoformat()
        all_contacts: list[Prospect] = []
        companies_used = 0

        try:
            rows = df.to_dict("records")
        except Exception:
            return 0

        seen_names: set[str] = set()
        for row in rows:
            if companies_used >= 5:
                break
            company_name = str(row.get("company") or "").strip()
            job_title = str(row.get("title") or "").strip()[:80]
            if not company_name or company_name.lower() in seen_names:
                continue
            seen_names.add(company_name.lower())

            try:
                domain = await self._guess_domain(company_name)
            except Exception:
                domain = ""
            if not self._is_candidate_domain(domain):
                continue
            if await self.state.company_exists(domain):
                self._seen_domains.add(domain)
                continue
            self._seen_domains.add(domain)
            companies_used += 1

            info = await self._scrape_company_info(domain)
            company_id = await self._ensure_company(
                name=info.get("name") or company_name,
                domain=domain,
                website=info.get("website", f"https://{domain}"),
                description=info.get("description", ""),
                industry=default_industry,
                source="job_board",
                source_url=str(row.get("job_url") or ""),
            )

            signal = {"type": "hiring", "detail": job_title, "found_at": today}
            try:
                await self.state.update_company_signals(
                    company_id, new_signals=[signal]
                )
            except Exception as e:
                logger.debug(f"update_company_signals failed: {e}")

            signal_note = f"Signal: hiring {job_title} (posted on job board)"
            from mercury.integrations.tech_detect import detect_tech
            tech = detect_tech(info.get("_homepage_html", ""))
            if tech:
                await self.state.update_company_signals(company_id, tech_stack=tech)
                signal_note += ". Tech on site: " + ", ".join(tech[:5])

            contacts = await self._contacts_from_company(
                company_id=company_id,
                domain=domain,
                company_name=info.get("name") or company_name,
                industry=default_industry,
                signal_note=signal_note[:400],
                source="job_board",
            )
            all_contacts.extend(contacts)

        if not all_contacts:
            logger.info("Scout: job-board sweep found no ICP-matching contacts.")
            return 0

        scored = await self._score_contacts(all_contacts)
        count = 0
        for prospect in scored:
            await self.state.add_prospect(prospect)
            count += 1
            logger.info(
                f"Scout: Added {prospect.full_name()} ({prospect.title}) "
                f"at {prospect.company} via job-board signal [score: {prospect.score}]"
            )
            if count >= MAX_PROSPECTS_PER_CYCLE:
                break
        return count

    # ── Buying signals (tech stack + hiring) ──

    def _signal_role_keywords(self) -> list[str]:
        """Role keywords that indicate in-market intent (config or ICP titles)."""
        keywords = [
            k.strip().lower()
            for k in (getattr(self.config.icp, "hiring_signals", None) or [])
            if k.strip()
        ]
        if keywords:
            return keywords
        return [t.strip().lower() for t in self.config.icp.titles if t.strip()]

    async def _enrich_company_signals(
        self, company_id: str, domain: str, homepage_html: str = ""
    ) -> str:
        """Detect tech stack + hiring signals, persist them, return a note.

        The returned note seeds prospects' personalization_notes so the
        Writer references something true and current instead of flattery.
        """
        from mercury.integrations.tech_detect import detect_tech

        if not homepage_html:
            resp = await self._fetch(
                f"https://{domain}", timeout=SCRAPE_TIMEOUT, retries=1
            )
            homepage_html = resp.text if resp is not None else ""

        tech = detect_tech(homepage_html)

        signals: list[dict] = []
        try:
            hiring_roles = await self._scrape_hiring_signals(domain)
        except Exception as e:
            logger.debug(f"Hiring scan failed for {domain}: {e}")
            hiring_roles = []
        today = datetime.now(timezone.utc).date().isoformat()
        for role in hiring_roles:
            signals.append({"type": "hiring", "detail": role, "found_at": today})

        if tech or signals:
            try:
                await self.state.update_company_signals(
                    company_id, tech_stack=tech, new_signals=signals
                )
            except Exception as e:
                logger.debug(f"update_company_signals failed for {domain}: {e}")

        notes = []
        if hiring_roles:
            notes.append("Signal: hiring " + ", ".join(hiring_roles[:2]))
        if tech:
            notes.append("Tech on site: " + ", ".join(tech[:5]))
        note = ". ".join(notes)
        if note:
            logger.info(f"Scout: signals for {domain} — {note}")
        return note[:400]

    _CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/join-us", "/join",
                      "/about/careers", "/company/careers", "/work-with-us")

    async def _scrape_hiring_signals(self, domain: str) -> list[str]:
        """Scan the company's careers page for open roles matching our
        signal keywords. Returns matched role titles (deduped, capped)."""
        keywords = self._signal_role_keywords()
        if not keywords:
            return []

        for path in self._CAREERS_PATHS:
            resp = await self._fetch(
                f"https://{domain}{path}", timeout=SCRAPE_TIMEOUT, retries=1
            )
            if resp is None:
                continue
            soup = self._safe_soup(resp.text)
            if soup is None:
                continue

            roles: list[str] = []
            seen = set()
            try:
                elements = soup.select("a, h2, h3, h4, li")
            except Exception:
                elements = []
            for el in elements[:400]:
                text = re.sub(r"\s+", " ", el.get_text()).strip()
                if not (4 <= len(text) <= 80):
                    continue
                low = text.lower()
                if not any(k in low for k in keywords):
                    continue
                key = low[:60]
                if key in seen:
                    continue
                seen.add(key)
                roles.append(text[:80])
                if len(roles) >= 3:
                    return roles
            if roles:
                return roles
            # A reachable careers page with no keyword hits still means we
            # checked — don't try more paths for this domain.
            return []
        return []

    async def _learn_pattern_from_page(self, soup, html: str, domain: str):
        """Find a real on-domain email on a page and cache the derived pattern.

        Prefers mailto: links (cleanest), falls back to inline addresses.
        Skips role addresses (info@, sales@) — they don't reveal a
        person-name pattern.
        """
        domain = domain.lower()
        role_locals = {
            "info", "sales", "support", "hello", "contact", "admin",
            "team", "help", "office", "press", "media", "careers", "jobs",
            "hr", "billing", "noreply", "no-reply", "marketing",
        }
        found = []
        try:
            for a in soup.find_all("a", href=True):
                href = self._attr_str(a.get("href"))
                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip().lower()
                    if "@" in addr:
                        found.append(addr)
        except Exception:
            pass
        if not found:
            try:
                found = [
                    m.lower() for m in re.findall(
                        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                        html[:20000],
                    )
                ]
            except Exception:
                found = []

        for addr in found:
            local, _, adomain = addr.partition("@")
            if adomain != domain or local in role_locals or "." not in local:
                continue
            # Looks like first.last@ — infer and cache the pattern.
            from mercury.integrations.email_finder import (
                infer_pattern_from_email,
            )
            parts = local.replace("_", ".").replace("-", ".").split(".")
            if len(parts) < 2:
                continue
            pattern = infer_pattern_from_email(addr, parts[0], parts[-1])
            if pattern:
                try:
                    await self.state.save_email_pattern(
                        domain, pattern=pattern, source="scraped_mailto",
                        confidence=0.85,
                    )
                    logger.debug(f"Scout: learned pattern {pattern} for {domain}")
                except Exception as e:
                    logger.debug(f"save_email_pattern failed: {e}")
                return

    async def _guess_domain(self, company_name: str) -> str:
        """Guess a company's domain from its name and confirm it resolves."""
        name = company_name.lower().strip()
        for suffix in [" inc", " llc", " ltd", " corp", " co", " group",
                       " incorporated", " limited", " company"]:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        name = re.sub(r"[^a-z0-9]", "", name)
        if not name:
            return ""

        for tld in (".com", ".io", ".co"):
            domain = f"{name}{tld}"
            resp = await self._fetch(
                f"https://{domain}", method="HEAD", timeout=6, retries=1
            )
            if resp is not None and resp.status_code < 400:
                return domain
        return ""

    def _extract_domain(self, url: str) -> str:
        """Extract the registrable-ish domain from a URL."""
        if not url:
            return ""
        try:
            if "://" not in url:
                url = "http://" + url
            parsed = urlparse(url)
            host = parsed.netloc or parsed.path.split("/")[0]
            host = host.split("@")[-1]      # strip credentials
            host = host.split(":")[0]        # strip port
            host = re.sub(r"^www\.", "", host.lower())
            if "." in host and len(host) > 3:
                return host
        except Exception:
            pass
        return ""

    def _is_noise_domain(self, domain: str) -> bool:
        """Filter out domains that aren't actual companies."""
        noise = {
            "linkedin.com", "facebook.com", "twitter.com", "x.com",
            "instagram.com", "youtube.com", "tiktok.com", "pinterest.com",
            "google.com", "bing.com", "yahoo.com", "duckduckgo.com",
            "wikipedia.org", "reddit.com", "quora.com",
            "yelp.com", "bbb.org", "glassdoor.com",
            "crunchbase.com", "zoominfo.com", "apollo.io",
            "indeed.com", "monster.com", "ziprecruiter.com",
            "github.com", "stackoverflow.com", "gitlab.com",
            "medium.com", "substack.com", "wordpress.com", "blogspot.com",
            "amazon.com", "apple.com", "microsoft.com", "adobe.com",
            "g2.com", "capterra.com", "clutch.co",
            "trustpilot.com", "getapp.com", "producthunt.com",
            "youtu.be", "goo.gl", "bit.ly", "t.co",
        }
        if domain in noise:
            return True
        return any(domain == n or domain.endswith(f".{n}") for n in noise)

    def _domain_to_name(self, domain: str) -> str:
        """Convert a domain to a rough company name."""
        if not domain:
            return ""
        name = domain.split(".")[0]
        return name.replace("-", " ").title()

    def _title_matches_icp(self, title: str) -> bool:
        """Check if a job title matches the ICP target titles."""
        if not title:
            return False
        title_lower = title.lower()
        if self.config.icp.titles:
            for target in self.config.icp.titles:
                t = target.lower().strip()
                if t and t in title_lower:
                    return True
            return False
        # No explicit ICP titles → fall back to any senior title.
        return any(kw in title_lower for kw in _SENIOR_KEYWORDS)

    def _infer_seniority(self, title: str) -> str:
        """Infer seniority level from a job title."""
        if not title:
            return ""
        t = title.lower()
        if any(kw in t for kw in ["ceo", "cto", "cfo", "cmo", "coo", "cro",
                                  "chief", "founder", "co-founder", "owner",
                                  "president"]) and "vice president" not in t:
            return "c_suite"
        if any(kw in t for kw in ["vp", "vice president", "svp", "evp", "head of"]):
            return "vp"
        if "director" in t:
            return "director"
        if any(kw in t for kw in ["manager", "team lead", "lead "]):
            return "manager"
        return "individual"

    def _parse_linkedin_url(self, url: str, snippet: str) -> dict | None:
        """Parse a LinkedIn profile URL and snippet to extract a name.

        Prefers the snippet (real cased names) and falls back to the URL slug.
        """
        # 1) Snippet often starts with "First Last - Title at Company".
        if snippet:
            name_match = re.match(
                r"^\s*([A-Z][a-z]+)\s+([A-Z][a-z'\-]+)", snippet.strip()
            )
            if name_match:
                return {
                    "first_name": name_match.group(1),
                    "last_name": name_match.group(2),
                }

        # 2) Fall back to the URL slug.
        match = re.search(r"/in/([\w][\w-]+)", url)
        if match:
            slug = match.group(1)
            # Remove trailing hex/numeric IDs that LinkedIn appends.
            slug = re.sub(r"-[0-9a-f]{4,}$", "", slug)
            slug = re.sub(r"-\d+$", "", slug)
            parts = [p for p in slug.split("-") if p]
            # Drop pure-numeric noise tokens.
            parts = [p for p in parts if not p.isdigit()]
            if len(parts) >= 2:
                return {
                    "first_name": parts[0].title(),
                    "last_name": " ".join(p.title() for p in parts[1:]),
                }

        return None
