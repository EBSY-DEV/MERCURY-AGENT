"""Email discovery — find email addresses without paid tools.

Verification order:
1. Hunter.io (if HUNTER_API_KEY is set) — most reliable.
2. SMTP RCPT-TO probing — free fallback, degrades gracefully.
3. Pattern best-guess — when nothing can be verified.
"""

import asyncio
import logging
import os
import random
import re
from typing import Optional

import dns.resolver
import aiosmtplib
import httpx

logger = logging.getLogger("harvey.email_finder")

# Cache MX records per domain to avoid repeated lookups.
# Failed lookups are cached as None so we don't hammer DNS.
_mx_cache: dict[str, Optional[str]] = {}

HUNTER_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
HUNTER_MAX_RETRIES = 3


def _clean_domain(domain: str) -> str:
    """Normalize a domain: strip scheme, www, path, whitespace."""
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0].split("?")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def generate_patterns(first_name: str, last_name: str, domain: str) -> list[str]:
    """Generate common email patterns for a person at a domain."""
    first = (first_name or "").lower().strip()
    last = (last_name or "").lower().strip()
    domain = _clean_domain(domain)

    # Remove non-alpha characters BEFORE the empty check — otherwise names
    # with no ascii letters slip through and first[0] raises IndexError.
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)

    if not first or not last or not domain:
        return []

    patterns = [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first}{last[0]}@{domain}",
        f"{first[0]}.{last}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first}-{last}@{domain}",
    ]
    # Dedupe while preserving order (short names can collide).
    return list(dict.fromkeys(patterns))


async def get_mx_host(domain: str) -> Optional[str]:
    """Get the primary MX host for a domain."""
    domain = _clean_domain(domain)
    if not domain:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]

    try:
        loop = asyncio.get_event_loop()
        answers = await loop.run_in_executor(
            None, lambda: dns.resolver.resolve(domain, "MX")
        )
        records = sorted(answers, key=lambda x: x.preference)
        if not records:
            _mx_cache[domain] = None
            return None
        mx_host = str(records[0].exchange).rstrip(".")
        _mx_cache[domain] = mx_host or None
        return _mx_cache[domain]
    except Exception as e:
        logger.debug(f"MX lookup failed for {domain}: {e}")
        _mx_cache[domain] = None
        return None


async def verify_email_smtp(email: str) -> bool:
    """Verify an email exists via SMTP RCPT TO check.

    Note: Many servers don't support this (catch-all domains, etc.)
    so a failed check doesn't guarantee the email is invalid.
    A successful check is a strong signal though.
    """
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    mx_host = await get_mx_host(domain)
    if not mx_host:
        return False

    smtp = aiosmtplib.SMTP(hostname=mx_host, port=25, timeout=10)
    try:
        await smtp.connect()
        await smtp.ehlo()

        # Try VRFY first
        try:
            code, _ = await smtp.vrfy(email)
            if code == 250:
                return True
        except Exception:
            pass  # VRFY often disabled

        # Fall back to MAIL FROM + RCPT TO
        await smtp.mail("verify@example.com")
        code, message = await smtp.rcpt(email)

        # 250 = accepted, 550 = rejected
        return code == 250

    except Exception as e:
        logger.debug(f"SMTP verification failed for {email}: {e}")
        return False
    finally:
        try:
            await smtp.quit()
        except Exception:
            pass


def _hunter_api_key() -> str:
    """Hunter is optional; absence just means we degrade to SMTP checks."""
    return os.getenv("HUNTER_API_KEY", "").strip()


async def verify_email_hunter(email: str, api_key: str | None = None) -> Optional[bool]:
    """Verify an email via Hunter.io.

    Returns True (deliverable), False (undeliverable), or None
    (unknown / Hunter unavailable / no API key) so callers can
    degrade gracefully to SMTP verification.
    """
    key = (api_key or _hunter_api_key())
    if not key:
        return None

    def _redact(text: str) -> str:
        return text.replace(key, "***REDACTED***") if text else ""

    async with httpx.AsyncClient(timeout=HUNTER_TIMEOUT) as client:
        for attempt in range(HUNTER_MAX_RETRIES + 1):
            try:
                resp = await client.get(
                    "https://api.hunter.io/v2/email-verifier",
                    params={"email": email, "api_key": key},
                )
            except httpx.HTTPError as e:
                if attempt < HUNTER_MAX_RETRIES:
                    delay = min(2 ** attempt, 15) * random.uniform(0.5, 1.5)
                    logger.debug(
                        f"Hunter request error for {email}: {_redact(str(e))}. "
                        f"Retrying in {delay:.1f}s."
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    f"Hunter verification unavailable for {email}: "
                    f"{_redact(str(e))}"
                )
                return None

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < HUNTER_MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        delay = min(float(retry_after), 30) if retry_after else 0
                    except ValueError:
                        delay = 0
                    delay = delay or min(2 ** attempt, 15) * random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay)
                    continue
                logger.warning(f"Hunter API error {resp.status_code} for {email}.")
                return None

            if resp.status_code in (401, 403):
                logger.warning(
                    "Hunter API key rejected; falling back to SMTP verification."
                )
                return None

            if resp.status_code >= 400:
                logger.debug(f"Hunter API error {resp.status_code} for {email}.")
                return None

            try:
                payload = resp.json()
            except ValueError:
                return None

            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return None

            status = data.get("status") or data.get("result")
            if status in ("valid", "deliverable"):
                return True
            if status in ("invalid", "undeliverable"):
                return False
            return None  # risky / unknown / accept_all

    return None


async def find_email(
    first_name: str,
    last_name: str,
    domain: str,
    verify: bool = True,
) -> Optional[str]:
    """Try to find a valid email for a person at a company domain.

    1. Generate common email patterns
    2. Check MX records exist for domain
    3. Verify each pattern (Hunter if configured, else SMTP)
    4. Return first verified email, or best guess if verification unavailable
    """
    patterns = generate_patterns(first_name, last_name, domain)
    if not patterns:
        return None

    # first.last@domain is the most common corporate pattern — use it as
    # the consistent best guess everywhere.
    clean = _clean_domain(domain)
    first = re.sub(r"[^a-z]", "", (first_name or "").lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").lower())
    best_guess = f"{first}.{last}@{clean}"

    # First check if domain has MX records at all
    mx_host = await get_mx_host(clean)
    if not mx_host:
        logger.info(f"No MX records for {clean}. Returning best guess.")
        return best_guess

    if not verify:
        return best_guess

    use_hunter = bool(_hunter_api_key())

    # Try to verify each pattern
    for email in patterns:
        logger.debug(f"Checking {email}...")
        try:
            is_valid: Optional[bool] = None
            if use_hunter:
                is_valid = await verify_email_hunter(email)
                if is_valid is None:
                    # Hunter couldn't say — degrade to SMTP for this address
                    is_valid = await verify_email_smtp(email)
            else:
                is_valid = await verify_email_smtp(email)

            if is_valid:
                logger.info(f"Verified email: {email}")
                return email
        except Exception as e:
            logger.debug(f"Verification error for {email}: {e}")

        # Rate limit between checks (jittered so probes don't look scripted)
        await asyncio.sleep(random.uniform(0.8, 2.0))

    # If no verification worked, return the most common pattern
    logger.info(f"No verified email found. Best guess: {best_guess}")
    return best_guess
