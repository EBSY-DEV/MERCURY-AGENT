"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import aiosmtplib

from mercury.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)

logger = logging.getLogger("mercury.smtp")


def _bounce_details(msg) -> dict:
    """Pull the failed recipient and the original Message-ID out of a DSN.

    Gmail puts the original Message-ID in In-Reply-To; most other servers
    only carry it inside the message/delivery-status and message/rfc822
    parts, so without this walk their bounces could never be matched and
    a dead address kept receiving steps 2 and 3.
    """
    out: dict = {}
    try:
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload()
            if ctype == "message/delivery-status" and isinstance(payload, list):
                for sub in payload:
                    for key in ("Final-Recipient", "Original-Recipient"):
                        val = str(sub.get(key, "") or "").strip()
                        if val and "bounced_recipient" not in out:
                            out["bounced_recipient"] = val.split(";")[-1].strip().lower()
            elif ctype in ("message/rfc822", "text/rfc822-headers"):
                inner = None
                if isinstance(payload, list) and payload:
                    inner = payload[0]
                elif isinstance(payload, (str, bytes)):
                    text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
                    inner = email.message_from_string(text, policy=email.policy.default)
                if inner is not None:
                    mid = str(inner.get("Message-ID", "") or "").strip()
                    if mid and "original_message_id" not in out:
                        out["original_message_id"] = mid
                    to = parseaddr(str(inner.get("To", "") or ""))[1].lower()
                    if to and "bounced_recipient" not in out:
                        out["bounced_recipient"] = to
    except Exception:
        pass
    return out


class SmtpImapProvider(MailProvider):
    name = "smtp"
    LOOKBACK_DAYS = 3

    def __init__(self, config, env, mailbox=None):
        """``mailbox`` (a MailboxConfig) sends as that address with its own
        login; without it this is the single SMTP_* mailbox from .env."""
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass
        # The address mail goes out as. None = persona.email (legacy).
        self.from_email: str | None = None
        self.from_name: str | None = None
        if mailbox is not None:
            secret = env.secret(mailbox.password_env) if hasattr(env, "secret") else ""
            self.smtp_host = mailbox.smtp_host or self.smtp_host
            self.smtp_port = int(mailbox.smtp_port or self.smtp_port)
            self.smtp_user = mailbox.username or mailbox.email
            self.smtp_pass = secret
            self.imap_host = mailbox.imap_host or getattr(env, "imap_host", "") or self.smtp_host
            self.imap_port = int(mailbox.imap_port or self.imap_port)
            self.imap_user = self.smtp_user
            self.imap_pass = secret
            self.from_email = mailbox.email
            self.from_name = mailbox.name or None

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    @property
    def sender_address(self) -> str:
        return self.from_email or self.config.persona.email or self.smtp_user

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        persona = self.config.persona
        # From must be the mailbox we authenticate as: SPF/DKIM align on its
        # domain, and replies have to land in the inbox we poll.
        sender_addr = self.sender_address
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = formataddr((self.from_name or persona.name, sender_addr))
        msg["Subject"] = subject
        message_id = make_msgid(domain=sender_addr.split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        # RFC 2369 opt-out header in mailto form. RFC 8058 one-click needs an
        # HTTPS endpoint we do not run, so List-Unsubscribe-Post is omitted.
        msg["List-Unsubscribe"] = f"<mailto:{sender_addr}?subject=unsubscribe>"
        msg.set_content(body)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unseen, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            return []

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            # A date window instead of the Seen flag: a reply the operator
            # opened in webmail, or one in a batch that aborted, is not
            # UNSEEN any more and was lost for good. PEEK leaves flags
            # untouched; processed_replies is the dedup.
            from datetime import datetime, timedelta
            since = (datetime.utcnow() - timedelta(days=self.LOOKBACK_DAYS)).strftime("%d-%b-%Y")
            status, data = conn.search(None, "SINCE", since)
            if status != "OK":
                return []
            ids = data[0].split()[-limit:]
            for msg_id in ids:
                try:
                    status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
                except Exception as e:
                    logger.warning(f"IMAP fetch of {msg_id!r} failed: {e}")
                    continue
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        headers = {
            k: str(msg.get(k, "")).strip()
            for k in ("Auto-Submitted", "Precedence", "X-Autoreply",
                      "X-Autorespond", "Return-Path")
            if msg.get(k)
        }
        bounce = looks_like_bounce(from_email, subject)
        if bounce:
            headers.update(_bounce_details(msg))
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=bounce,
            headers=headers,
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            if self.from_email:
                return False, (f"{self.from_email}: SMTP_HOST or its password env var "
                               "is missing from .env")
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"

    def __repr__(self) -> str:  # never include the password
        return f"SmtpImapProvider({self.smtp_user!r} @ {self.smtp_host}:{self.smtp_port})"
