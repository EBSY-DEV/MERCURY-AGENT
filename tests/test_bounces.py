"""Bounce classification by DSN status code, and what each bucket does:
LIST invalidates the address, SENDER pauses the mailbox, BURNED pauses the
domain, THROTTLE halves a cap, NOISE is ignored, and the kill switch trips on
the SENDER+BURNED share or the total rate. It must fail closed."""

import base64
import json
import os
import sqlite3
import tempfile
import textwrap
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from mercury import bounces, metrics, warmup
from mercury.integrations.gmail import GmailProvider
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.integrations.smtp_mail import SmtpImapProvider
from mercury.state import StateManager
from tests.test_mailboxes import make_config
from tests.test_outbox_native import FakeProvider, make_handler, seed_prospect


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── Realistic DSN bodies ──

GMAIL_NO_SUCH_USER = """\
** Address not found **

Your message wasn't delivered to jane@acme.com because the address couldn't be found, or is unable to receive mail.

The response from the remote server was:

550 5.1.1 The email account that you tried to reach does not exist. Please try double-checking the recipient's email address for typos or unnecessary spaces. For more information, go to https://support.google.com/mail/?p=NoSuchUser x4si1234567wmb.12 - gsmtp
"""

GMAIL_AUTH_BLOCK = """\
Message blocked

Your message to jane@acme.com has been blocked. See technical details below for more information.

The response from the remote server was:

550-5.7.26 This mail is unauthenticated, which poses a security risk to the
550-5.7.26 sender and Gmail users, and has been blocked. The sender must
550-5.7.26 authenticate with at least one of SPF or DKIM. For this message,
550-5.7.26 DKIM checks did not pass and SPF check for [send.example.co]
550-5.7.26 did not pass with ip: [203.0.113.9]. The sender should visit
550 5.7.26 https://support.google.com/mail/answer/81126#authentication
"""

GMAIL_UNSOLICITED = """\
Message blocked

The response from the remote server was:

550-5.7.1 [203.0.113.9 19] Gmail has detected that this message is
550-5.7.1 likely unsolicited mail. To reduce the amount of spam sent to Gmail,
550 5.7.1 this message has been blocked. gsmtp
"""

GMAIL_RATE_LIMIT = """\
Delivery incomplete

There was a temporary problem delivering your message to jane@acme.com. Gmail will retry for 47 more hours.

The response was:

421 4.7.28 Our system has detected an unusual rate of unsolicited mail originating from your IP address [10.4.2.1]. To protect our users from spam, mail sent from your IP address has been temporarily rate limited. gsmtp
"""

M365_BLOCKED_IP = """\
Your message to jane@contoso.com couldn't be delivered.

contoso.com rejected your message to the following email addresses:

jane@contoso.com ( jane@contoso.com )

Remote Server returned '550 5.7.606 Access denied, banned sending IP [203.0.113.9]. To request removal from this list please visit https://sender.office.com/ and follow the directions. For more information please go to http://go.microsoft.com/fwlink/?LinkID=526655 AS(1430) [DM6NAM11FT012.eop-nam11.prod.protection.outlook.com 2026-10-07T14:02:11.321Z]'

Diagnostic information for administrators:

Generating server: BN8PR05MB6420.namprd05.prod.outlook.com (mapi id 15.20.5.1)

Original message headers:
Received: from mail.send.example.co (203.0.113.9) by BN8PR05MB6420.namprd05.prod.outlook.com
"""

M365_NO_RECIPIENT = """\
Your message to jane@contoso.com couldn't be delivered.

The address wasn't found at contoso.com, or it can't receive email.

Remote Server returned '550 5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup'
"""

M365_ACCESS_DENIED = """\
contoso.com rejected your message to the following email addresses:

Remote Server returned '550 5.4.1 Recipient address rejected: Access denied. AS(201806281) [BN8NAM11FT009.eop-nam11.prod.protection.outlook.com]'
"""

M365_GROUP = """\
Remote Server returned '550 5.7.133 RESOLVER.RST.SenderNotAuthenticatedForGroup; authentication required; Delivery restriction check failed because the sender was not authenticated when sending to this group'
"""

POSTFIX_UNKNOWN_USER = """\
This is the mail system at host mail.send.example.co.

I'm sorry to have to inform you that your message could not
be delivered to one or more recipients. It's attached below.

For further assistance, please send mail to postmaster.

                   The mail system

<jane@acme.com>: host mx.acme.com[198.51.100.7] said: 550 5.1.1
    <jane@acme.com>: Recipient address rejected: User unknown in virtual
    mailbox table (in reply to RCPT TO command)
"""

POSTFIX_MAILBOX_FULL = """\
<jane@acme.com>: host mx.acme.com[198.51.100.7] said: 552 5.2.2 Mailbox full
    (in reply to RCPT TO command)
"""

POSTFIX_GREYLIST = """\
<jane@acme.com>: host mx.acme.com[198.51.100.7] said: 451 4.7.1 Greylisted,
    please try again in 300 seconds (in reply to RCPT TO command)
"""

POSTFIX_RELAY_DENIED = """\
<jane@acme.com>: host mx.acme.com[198.51.100.7] said: 554 5.7.1
    <jane@acme.com>: Relay access denied (in reply to RCPT TO command)
"""

NO_CODE = "Your message could not be delivered. The recipient server said no."


@pytest.mark.parametrize("body, code, bucket", [
    (GMAIL_NO_SUCH_USER, "5.1.1", bounces.LIST),
    (GMAIL_AUTH_BLOCK, "5.7.26", bounces.SENDER),
    (GMAIL_UNSOLICITED, "5.7.1", bounces.SENDER),
    (GMAIL_RATE_LIMIT, "4.7.28", bounces.THROTTLE),
    (M365_BLOCKED_IP, "5.7.606", bounces.BURNED),
    (M365_NO_RECIPIENT, "5.1.10", bounces.LIST),
    (M365_ACCESS_DENIED, "5.4.1", bounces.LIST),
    (M365_GROUP, "5.7.133", bounces.NOISE),
    (POSTFIX_UNKNOWN_USER, "5.1.1", bounces.LIST),
    (POSTFIX_MAILBOX_FULL, "5.2.2", bounces.NOISE),
    (POSTFIX_GREYLIST, "4.7.1", bounces.THROTTLE),
    (POSTFIX_RELAY_DENIED, "5.7.1", bounces.SENDER),
    (NO_CODE, "", bounces.UNKNOWN),
])
def test_dsn_bodies_map_to_their_bucket(body, code, bucket):
    assert bounces.classify_bounce({}, body) == (code, bucket)


def test_codes_inside_ip_addresses_and_build_ids_are_not_read():
    # 10.4.2.1 and 15.20.5.1 contain 4.2.1 / 5.20.5 lookalikes; the real
    # answer is further down.
    text = "from [10.4.2.1] mapi id 15.20.5.1 version 4.2.1.7\n550 5.1.1 no such user"
    assert bounces.extract_dsn_code(text) == "5.1.1"
    assert bounces.extract_dsn_code("only an ip 10.5.1.2 here") == ""


def test_code_after_the_reply_code_beats_an_earlier_stray_one():
    text = "Generated by gateway 5.4.9 (build)\nremote said: 550 5.7.1 blocked"
    assert bounces.extract_dsn_code(text) == "5.7.1"


def test_delivery_status_field_wins_but_generic_codes_defer():
    assert bounces.extract_dsn_code("550 5.1.1 gone", status_hint="5.7.1") == "5.7.1"
    # A bare x.0.0 only says "failed": the specific code in the text is used.
    assert bounces.extract_dsn_code("550 5.1.1 gone", status_hint="5.0.0") == "5.1.1"
    assert bounces.extract_dsn_code("no code here", status_hint="5.0.0") == "5.0.0"


def test_every_issue_code_lands_in_its_bucket():
    for code in ("5.1.1", "5.1.10", "5.1.0", "5.2.1", "5.4.1", "5.5.0"):
        assert bounces.classify(code) == bounces.LIST, code
    for code in ("5.7.1", "5.7.0", "5.7.23", "5.7.26", "5.7.509", "5.7.520"):
        assert bounces.classify(code) == bounces.SENDER, code
    for n in range(606, 615):
        assert bounces.classify(f"5.7.{n}") == bounces.BURNED, n
    for code in ("4.0.0", "4.2.2", "4.7.0", "4.4.1"):
        assert bounces.classify(code) == bounces.THROTTLE, code
    for code in ("5.2.2", "5.3.4", "5.7.133"):
        assert bounces.classify(code) == bounces.NOISE, code


def test_unlisted_codes_fail_toward_caution():
    # Any other 5.7.x is policy or security, so it is a sender problem.
    assert bounces.classify("5.7.25") == bounces.SENDER
    assert bounces.classify("5.7.615") == bounces.SENDER
    # Unlisted elsewhere, or garbage: UNKNOWN, which keeps the old behaviour
    # (address marked invalid, counted in the total rate).
    assert bounces.classify("5.3.0") == bounces.UNKNOWN
    assert bounces.classify("") == bounces.UNKNOWN
    assert bounces.classify("garbage") == bounces.UNKNOWN
    assert bounces.classify("2.0.0") == bounces.UNKNOWN


# ── Kill switch arithmetic ──


def test_kill_switch_share_rule():
    counts = {bounces.LIST: 20, bounces.SENDER: 5, bounces.BURNED: 1, bounces.THROTTLE: 4}
    # 30 classified, 6 bad = 20%: not over the limit
    assert bounces.kill_switch_reason(counts, 30, 10, 0.02) == ""
    counts[bounces.SENDER] = 6          # 7 of 31 = 22.6%
    assert "sender or reputation" in bounces.kill_switch_reason(counts, 31, 10, 0.02)
    # under 30 classified bounces the share says nothing
    assert bounces.kill_switch_reason({bounces.SENDER: 5, bounces.LIST: 5}, 10, 10, 0.02) == ""


def test_list_bounces_alone_never_trip_the_share_rule():
    assert bounces.kill_switch_reason({bounces.LIST: 200}, 200, 100000, 0.02) == ""


def test_kill_switch_rate_rule_needs_fifty_sends_and_skips_noise():
    assert bounces.kill_switch_reason({bounces.LIST: 3}, 3, 49, 0.02) == ""
    assert "3/50" in bounces.kill_switch_reason({bounces.LIST: 3}, 3, 50, 0.02)
    # exactly at the cap is not over it
    assert bounces.kill_switch_reason({bounces.LIST: 1}, 1, 50, 0.02) == ""
    # NOISE never counts: 3 bounces of which 2 are noise = 1 of 50 = 2%
    assert bounces.kill_switch_reason({bounces.LIST: 1, bounces.NOISE: 2}, 3, 50, 0.02) == ""
    # 0 turns the rate rule off, but not the share rule
    assert bounces.kill_switch_reason({bounces.LIST: 40}, 40, 50, 0) == ""
    assert bounces.kill_switch_reason({bounces.SENDER: 30}, 30, 50, 0)


def test_default_max_bounce_rate_is_two_percent():
    from mercury.config import EmailChannelConfig, load_config

    assert EmailChannelConfig().max_bounce_rate == 0.02
    assert load_config().channels.email.max_bounce_rate == 0.02
    # an explicit value in an existing config still wins
    assert EmailChannelConfig(max_bounce_rate=0.05).max_bounce_rate == 0.05


# ── Raw messages through the providers ──


def _dsn_rfc822(body: str, status: str) -> bytes:
    return textwrap.dedent("""\
        From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
        To: carlos@send.example.co
        Subject: Delivery Status Notification (Failure)
        Message-ID: <dsn-1@googlemail.com>
        In-Reply-To: <orig-1@send.example.co>
        MIME-Version: 1.0
        Content-Type: multipart/report; report-type=delivery-status; boundary="BOUND"

        --BOUND
        Content-Type: text/plain; charset="UTF-8"

        BODY_HERE
        --BOUND
        Content-Type: message/delivery-status

        Reporting-MTA: dns; googlemail.com

        Final-Recipient: rfc822; jane@acme.com
        Action: failed
        Status: STATUS_HERE
        Diagnostic-Code: smtp; 550 STATUS_HERE whatever

        --BOUND
        Content-Type: message/rfc822

        Message-ID: <orig-1@send.example.co>
        To: jane@acme.com
        Subject: hello

        original body
        --BOUND--
        """).replace("BODY_HERE", body).replace("STATUS_HERE", status).encode()


def test_smtp_provider_reads_the_delivery_status_code():
    msg = SmtpImapProvider._parse_rfc822(_dsn_rfc822(NO_CODE, "5.7.1"))
    assert msg.is_bounce
    assert msg.headers["dsn_status"] == "5.7.1"
    assert msg.headers["bounced_recipient"] == "jane@acme.com"
    # the body carries no code at all, the Status field does
    assert bounces.classify_bounce(msg.headers, msg.body) == ("5.7.1", bounces.SENDER)


def test_smtp_provider_text_part_alone_is_enough():
    msg = SmtpImapProvider._parse_rfc822(_dsn_rfc822(POSTFIX_UNKNOWN_USER, "5.0.0"))
    assert bounces.classify_bounce(msg.headers, msg.body) == ("5.1.1", bounces.LIST)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_gmail_provider_reads_the_delivery_status_part():
    payload = {
        "headers": [
            {"name": "From", "value": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"},
            {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
            {"name": "In-Reply-To", "value": "<orig-1@send.example.co>"},
        ],
        "mimeType": "multipart/report",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(NO_CODE)}},
            {"mimeType": "message/delivery-status", "body": {},
             "parts": [{"headers": [{"name": "Status", "value": "5.7.606"}], "body": {}}]},
        ],
    }
    msg = GmailProvider._parse_message({"id": "g1", "threadId": "t", "payload": payload})
    assert msg.is_bounce and msg.headers["dsn_status"] == "5.7.606"
    assert bounces.classify_bounce(msg.headers, msg.body) == ("5.7.606", bounces.BURNED)


def test_gmail_provider_without_a_status_part_falls_back_to_the_text():
    payload = {
        "headers": [{"name": "From", "value": "mailer-daemon@googlemail.com"},
                    {"name": "Subject", "value": "Delivery Status Notification (Failure)"}],
        "mimeType": "text/plain", "body": {"data": _b64(GMAIL_NO_SUCH_USER)},
    }
    msg = GmailProvider._parse_message({"id": "g2", "payload": payload})
    assert "dsn_status" not in msg.headers
    assert bounces.classify_bounce(msg.headers, msg.body) == ("5.1.1", bounces.LIST)


# ── Handler: what each bucket does ──


async def _sent_item(state, pid, email, message_id, mailbox="", step=1):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b", send_at=_now_iso(),
        status="approved", campaign_id=f"c-{message_id}", step=step, mailbox=mailbox)
    await state.update_outbox_item(item, status="sent", message_id=message_id,
                                   sent_at=_now_iso())
    return item


async def _filler_sends(state, n, mailbox=""):
    pid = await seed_prospect(state, email="filler@acme.com", status="contacted")
    for i in range(n):
        await _sent_item(state, pid, "filler@acme.com", f"<filler{i}@x>", mailbox)


def _bounce(n, body):
    return InboundMessage(
        provider_id=f"b{n}", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)", body=body,
        in_reply_to=f"<m{n}@x>", is_bounce=True)


async def _scenario(state, bodies, mailbox="", pool=None, sends=0):
    """One sent email per body, then the bounces come back. Returns the
    prospect ids and the handler."""
    pids = []
    for n, _ in enumerate(bodies):
        email = f"lead{n}@acme.com"
        pid = await seed_prospect(state, email=email, status="contacted")
        await _sent_item(state, pid, email, f"<m{n}@x>", mailbox)
        # a queued follow-up that a list bounce must cancel
        await state.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s2", body="b", send_at=_now_iso(),
            status="approved", campaign_id=f"c-{n}", step=2, mailbox=mailbox)
        pids.append(pid)
    if sends:
        await _filler_sends(state, sends, mailbox)
    provider = FakeProvider()
    provider.inbound = [_bounce(n, body) for n, body in enumerate(bodies)]
    handler = make_handler(state, provider)
    if pool is not None:
        handler.mailboxes = pool
        handler.provider = pool.primary.provider
        pool.primary.provider.inbound = provider.inbound
    await handler._run_native()
    return pids, handler


def _events(state):
    rows = sqlite3.connect(state.db_path).execute(
        "SELECT details_json FROM actions WHERE action_type = 'bounce' ORDER BY rowid").fetchall()
    return [json.loads(r[0]) for r in rows]


@pytest.mark.asyncio
async def test_bounce_record_stores_code_and_bucket(state):
    await _scenario(state, [GMAIL_NO_SUCH_USER, M365_BLOCKED_IP.replace("5.7.606", "5.7.0"),
                            NO_CODE])
    events = _events(state)
    assert [(e["dsn_code"], e["bucket"]) for e in events] == [
        ("5.1.1", "LIST"), ("5.7.0", "SENDER"), ("", "UNKNOWN")]


@pytest.mark.asyncio
async def test_list_bounces_invalidate_prospects_and_never_pause(state):
    pids, _ = await _scenario(state, [GMAIL_NO_SUCH_USER, M365_NO_RECIPIENT,
                                      POSTFIX_UNKNOWN_USER, M365_ACCESS_DENIED],
                              sends=20)
    for pid in pids:
        p = await state.get_prospect(pid)
        assert p.email_status == "invalid"
    # their queued follow-ups are cancelled
    assert len(await state.get_outbox(status="cancelled")) == 4
    # LIST never touches a mailbox, and 24 sends is too small a sample for the rate
    assert await state.get_setting("sending_paused") == ""
    assert await state.get_warmup_inbox("mercury@x.co") is None


@pytest.mark.asyncio
async def test_sender_bounce_pauses_the_mailbox_and_leaves_the_prospect(state):
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30),
                        Mailbox("b@two.co", FakeProvider(), 30)])
    pids, _ = await _scenario(state, [GMAIL_AUTH_BLOCK], mailbox="a@one.co", pool=pool)
    p = await state.get_prospect(pids[0])
    assert p.email_status == "verified"                        # not the address's fault
    a = await state.get_warmup_inbox("a@one.co")
    assert a["status"] == "paused" and "5.7.26" in a["pause_reason"]
    assert "DNS" in a["pause_reason"]                         # points at the checklist
    assert await state.get_warmup_inbox("b@two.co") is None   # the other inbox is untouched
    assert await state.get_setting("sending_paused") == ""    # one block is not the kill switch
    # and the sender really stops using it
    await warmup.apply_health(state, pool)
    assert pool.cap_on(pool.mailboxes[0], date.today()) == 0
    assert pool.cap_on(pool.mailboxes[1], date.today()) == 30


@pytest.mark.asyncio
async def test_burned_bounce_pauses_the_whole_domain_and_flags_it(state):
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30),
                        Mailbox("c@one.co", FakeProvider(), 30),
                        Mailbox("b@two.co", FakeProvider(), 30)])
    await _scenario(state, [M365_BLOCKED_IP], mailbox="a@one.co", pool=pool)
    for email in ("a@one.co", "c@one.co"):
        row = await state.get_warmup_inbox(email)
        assert row["status"] == "paused", email
    assert await state.get_warmup_inbox("b@two.co") is None
    verdict = await bounces.get_verdict(state, "one.co")
    assert verdict["verdict"] == "CANCEL_CANDIDATE" and verdict["code"] == "5.7.606"
    assert await bounces.get_verdict(state, "two.co") is None

    # visible on the Mailboxes tab payload
    data = await warmup.overview(state, make_config(), pool)
    by = {i["email"]: i for i in data["inboxes"]}
    assert by["a@one.co"]["verdict"] == "CANCEL_CANDIDATE"
    assert by["c@one.co"]["verdict"] == "CANCEL_CANDIDATE"
    assert by["b@two.co"]["verdict"] is None
    assert by["a@one.co"]["bounce_buckets"]["BURNED"] == 1
    assert by["b@two.co"]["bounce_buckets"]["BURNED"] == 0
    assert by["a@one.co"]["status"] == "paused"

    # a person resuming the inbox clears the flag
    await warmup.set_resumed(state, "a@one.co")
    assert await bounces.get_verdict(state, "one.co") is None


@pytest.mark.asyncio
async def test_sender_bounce_from_an_unknown_mailbox_trips_the_global_switch(state):
    # Rotation pool that doesn't contain the mailbox the bounce names.
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30),
                        Mailbox("b@two.co", FakeProvider(), 30)])
    note = await bounces.apply_bucket(
        state, pool, bucket=bounces.SENDER, code="5.7.26", mailbox="gone@removed.co")
    assert "kill switch" in note
    reason = await state.get_setting("sending_paused")
    assert reason and "5.7.26" in reason


@pytest.mark.asyncio
async def test_sender_bounce_with_no_pool_trips_the_global_switch(state):
    note = await bounces.apply_bucket(
        state, None, bucket=bounces.SENDER, code="5.7.1", mailbox="a@one.co")
    assert "kill switch" in note
    assert await state.get_setting("sending_paused")


@pytest.mark.asyncio
async def test_pause_that_cannot_be_saved_trips_the_global_switch(state, monkeypatch):
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30)])

    async def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(warmup, "set_paused", boom)
    note = await bounces.apply_bucket(
        state, pool, bucket=bounces.SENDER, code="5.7.1", mailbox="a@one.co")
    assert "kill switch" in note
    assert "disk full" in await state.get_setting("sending_paused")


@pytest.mark.asyncio
async def test_throttle_bounce_halves_the_cap_for_a_week_only(state):
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30),
                        Mailbox("b@two.co", FakeProvider(), 30)])
    pids, _ = await _scenario(state, [GMAIL_RATE_LIMIT], mailbox="a@one.co", pool=pool)
    assert (await state.get_prospect(pids[0])).email_status == "verified"
    assert len(await state.get_outbox(status="cancelled")) == 0   # follow-up stays queued
    until = await bounces.throttled_until(state, "a@one.co")
    assert until
    delta = datetime.fromisoformat(until) - datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)

    await warmup.apply_health(state, pool)
    a, b = pool.mailboxes
    assert pool.cap_on(a, date.today()) == 15 and pool.cap_on(b, date.today()) == 30
    # a week later it is over
    later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=8)
    assert await bounces.throttled_until(state, "a@one.co", later) is None
    await warmup.apply_health(state, pool, now=later)
    assert pool.cap_on(a, date.today()) == 30
    # slowed, never paused
    assert (await state.get_warmup_inbox("a@one.co")) is None


@pytest.mark.asyncio
async def test_noise_bounces_are_ignored_for_rates_and_the_address(state):
    pids, _ = await _scenario(state, [POSTFIX_MAILBOX_FULL, M365_GROUP, POSTFIX_MAILBOX_FULL],
                              sends=60)
    for pid in pids:
        assert (await state.get_prospect(pid)).email_status == "verified"
    # 3 noise bounces on 63 sends would be 4.8% if counted
    assert await state.get_setting("sending_paused") == ""
    counts = await metrics.window_counts(state.db_path, "2000-01-01T00:00:00")
    assert counts["bounces"] == 0
    by_mailbox = await metrics.window_counts_by_mailbox(state.db_path, "2000-01-01T00:00:00")
    assert all(c["bounces"] == 0 for c in by_mailbox.values())


# ── The kill switch trips ──


@pytest.mark.asyncio
async def test_kill_switch_trips_on_sender_and_burned_share(state):
    # 40 bounces: 30 bad addresses and 10 blocks = 25% blocks, past 20%.
    # Only 40 sends, so the total-rate rule (needs 50) cannot be the cause.
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30)])
    bodies = [GMAIL_NO_SUCH_USER] * 30 + [GMAIL_UNSOLICITED] * 10
    await _scenario(state, bodies, mailbox="a@one.co", pool=pool)
    reason = await state.get_setting("sending_paused")
    assert reason and "sender or reputation" in reason
    assert "8 of 38" in reason          # 7 of 37 is 18.9%: the first count over 20%


@pytest.mark.asyncio
async def test_kill_switch_does_not_trip_below_the_share_limit(state):
    pool = MailboxPool([Mailbox("a@one.co", FakeProvider(), 30)])
    bodies = [GMAIL_NO_SUCH_USER] * 28 + [GMAIL_UNSOLICITED] * 2      # 30 bounces, 6.7%
    await _scenario(state, bodies, mailbox="a@one.co", pool=pool)
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_kill_switch_trips_on_total_rate_after_fifty_sends(state):
    # the second list bounce makes 2 of 50 = 4%, over the 2% in the test config
    await _scenario(state, [GMAIL_NO_SUCH_USER] * 3, sends=47)
    reason = await state.get_setting("sending_paused")
    assert reason and "2/50" in reason and "2%" in reason     # 1/50 = 2% is not over


@pytest.mark.asyncio
async def test_total_rate_waits_for_fifty_sends(state):
    await _scenario(state, [GMAIL_NO_SUCH_USER] * 3, sends=46)      # 49 sends
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_existing_bounce_count_still_counts_toward_the_rate(state):
    # Bounces counted by the old counter before this feature carry no bucket.
    await state.set_setting("bounce_count", "5")
    await _scenario(state, [GMAIL_NO_SUCH_USER], sends=49)           # 50 sends, 6 bounces
    assert await state.get_setting("sending_paused")


@pytest.mark.asyncio
async def test_resume_resets_every_counter(state):
    await _scenario(state, [GMAIL_NO_SUCH_USER] * 3, sends=47)
    assert await state.get_setting("sending_paused")
    await state.set_setting("sending_paused", "")
    await bounces.reset_counters(state)
    assert await state.get_setting("bounce_count") == "0"
    assert all(v == 0 for v in (await bounces.load_counts(state)).values())


@pytest.mark.asyncio
async def test_kill_switch_keeps_the_first_reason(state):
    await state.set_setting("sending_paused", "first cause")
    assert await bounces.engage_kill_switch(state, "something else")
    assert await state.get_setting("sending_paused") == "first cause"


@pytest.mark.asyncio
async def test_an_operator_pause_does_not_hide_the_kill_switch(state):
    # A legacy operator pause in the shared key moves aside first, so the
    # health hold is recorded rather than swallowed by it.
    await state.set_setting("sending_paused", "paused manually")
    assert await bounces.engage_kill_switch(state, "something else")
    assert await state.get_setting("sending_paused") == "something else"
    assert await state.get_setting("operator_pause") == "paused manually"


@pytest.mark.asyncio
async def test_a_bounce_that_cannot_be_processed_stops_sending(state, monkeypatch):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "jane@acme.com", "<m0@x>")
    provider = FakeProvider()
    provider.inbound = [_bounce(0, GMAIL_NO_SUCH_USER)]
    handler = make_handler(state, provider)

    async def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(bounces, "record_bucket", boom)
    await handler._run_native()
    assert "could not be processed" in await state.get_setting("sending_paused")


@pytest.mark.asyncio
async def test_a_bounce_is_kept_for_retry_if_even_the_switch_cannot_be_set(state, monkeypatch):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "jane@acme.com", "<m0@x>")
    provider = FakeProvider()
    provider.inbound = [_bounce(0, GMAIL_NO_SUCH_USER)]
    handler = make_handler(state, provider)

    async def boom(*a, **k):
        raise RuntimeError("db gone")

    async def no_switch(*a, **k):
        return False

    monkeypatch.setattr(bounces, "record_bucket", boom)
    monkeypatch.setattr(bounces, "engage_kill_switch", no_switch)
    await handler._run_native()
    assert not await state.is_reply_processed("b0")                 # tried again next beat
