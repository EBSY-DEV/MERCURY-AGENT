"""Exclusions, company holds and per-company limits, against a real database
and a recording mail provider: a blocked email must never reach send_email."""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

import mercury.state as state_module
from mercury.control.exclusions import ExclusionError, ExclusionService
from mercury.control.imports import ImportService
from mercury.integrations.mail_provider import InboundMessage
from mercury.models.campaign import Campaign, EmailStep
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.policy import CompanyLimits, ContactPolicy
from mercury.state import StateManager
from tests.test_outbox_native import FakeProvider, make_handler, make_sender


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def _utc(**delta) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**delta)).isoformat()


async def add_company(state, domain="acme.com", name="Acme"):
    return await state.add_company(Company(name=name, domain=domain))


async def add_person(state, email, company_id="", first="Jane", status="new"):
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="VP", company="Acme", email=email,
        email_status="verified", email_verified=True, status=status, company_id=company_id,
    ))


async def add_campaign(state, prospect_ids, steps=2, name="c"):
    sequence = [EmailStep(step=1, subject="hi {{first_name}}", body="Short note. Question?",
                          delay_days=0)]
    if steps > 1:
        sequence.append(EmailStep(step=2, subject="again", body="Another angle.", delay_days=3))
    campaign = Campaign(id="", name=name, channel="email", sequence=sequence,
                        prospect_ids=prospect_ids, status="draft")
    campaign.id = await state.add_campaign(campaign)
    return campaign


def sender_with(state, provider=None, autopilot=True, **limits):
    sender = make_sender(state, provider or FakeProvider(), require_approval=not autopilot)
    sender.policy.limits = CompanyLimits(**limits)
    return sender


# ── Matching ──


@pytest.mark.asyncio
async def test_email_rule_matches_one_address_only(state):
    svc = ExclusionService(state)
    await svc.add("email", " Jane@Acme.com ", reason="asked by phone")
    assert (await svc.check("jane@acme.com"))["excluded"]
    assert not (await svc.check("john@acme.com"))["excluded"]


@pytest.mark.asyncio
async def test_domain_rule_is_exact_unless_subdomains_are_asked_for(state):
    svc = ExclusionService(state)
    await svc.add("domain", "https://www.acme.com/about")      # stored as acme.com
    assert (await svc.check("a@acme.com"))["excluded"]
    assert not (await svc.check("a@eu.acme.com"))["excluded"]
    assert not (await svc.check("a@notacme.com"))["excluded"]  # suffix, not a subdomain

    await svc.add("domain", "beta.io", include_subdomains=True)
    assert (await svc.check("a@beta.io"))["excluded"]
    assert (await svc.check("a@mail.eu.beta.io"))["excluded"]
    assert not (await svc.check("a@alphabeta.io"))["excluded"]


@pytest.mark.asyncio
async def test_invalid_rules_are_refused(state):
    svc = ExclusionService(state)
    with pytest.raises(ExclusionError):
        await svc.add("email", "not-an-address")
    with pytest.raises(ExclusionError):
        await svc.add("domain", "localhost")
    with pytest.raises(ExclusionError):
        await svc.add("email", "a@b.co", include_subdomains=True)


# ── Send-time enforcement ──


@pytest.mark.asyncio
async def test_rule_added_after_approval_blocks_before_the_provider_call(state):
    pid = await add_person(state, "jane@acme.com")
    await add_campaign(state, [pid], steps=1)
    provider = FakeProvider()
    sender = sender_with(state, provider)
    for c in await state.get_campaigns_by_status("draft"):
        await sender._stage_campaign_native(c)
    assert len(await state.get_outbox(status="approved")) == 1

    await ExclusionService(state).add("domain", "acme.com", reason="customer already")
    await sender._drain_due()

    assert provider.sent == []
    blocked = await state.get_outbox(status="blocked")
    assert len(blocked) == 1 and "acme.com" in blocked[0]["error"]


@pytest.mark.asyncio
async def test_stale_due_scan_cannot_send_to_a_new_exclusion(state):
    """The rule lands between the due scan and the claim."""
    pid = await add_person(state, "jane@acme.com")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="s",
                                body="b", send_at=_utc(), status="approved", campaign_id="c1")
    (item,) = await state.get_outbox(status="approved")
    # Bypass the add-time blocking to model a rule that arrives mid-cycle.
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("INSERT INTO suppressions (id, kind, value, source) "
                         "VALUES ('r1', 'email', 'jane@acme.com', 'manual')")
        await db.commit()
    claim, detail = await state.claim_for_send(item, "mercury@x.co")
    assert claim == "suppressed"
    assert (await state.get_outbox_item(item["id"]))["status"] == "blocked"


@pytest.mark.asyncio
async def test_exclusions_also_stop_replies(state):
    pid = await add_person(state, "jane@acme.com", status="replied")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", kind="reply",
                                subject="Re: hi", body="Thanks!", send_at=_utc(),
                                status="approved")
    await ExclusionService(state).add("email", "jane@acme.com")
    provider = FakeProvider()
    await sender_with(state, provider)._drain_due()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_lifting_a_rule_never_approves_blocked_mail(state):
    pid = await add_person(state, "jane@acme.com")
    item_id = await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com",
                                          subject="s", body="b", send_at=_utc(),
                                          status="approved", campaign_id="c1")
    svc = ExclusionService(state)
    rule = await svc.add("email", "jane@acme.com")
    assert rule["blocked"] == 1

    with pytest.raises(ExclusionError) as still:
        await svc.requeue(item_id)
    assert still.value.code == "still_excluded"

    await svc.remove(rule["id"], note="wrong person")
    assert (await state.get_outbox_item(item_id))["status"] == "blocked"
    await svc.requeue(item_id)
    assert (await state.get_outbox_item(item_id))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_staging_skips_excluded_addresses(state):
    pid = await add_person(state, "jane@acme.com")
    other = await add_person(state, "bob@beta.io", first="Bob")
    await add_campaign(state, [pid, other])
    await ExclusionService(state).add("email", "jane@acme.com")
    sender = sender_with(state)
    for c in await state.get_campaigns_by_status("draft"):
        await sender._stage_campaign_native(c)
    queued = await state.get_outbox(status="approved")
    assert {i["to_email"] for i in queued} == {"bob@beta.io"}


# ── Opt-outs ──


@pytest.mark.asyncio
async def test_opt_out_survives_deletion_and_reimport(state):
    pid = await add_person(state, "jane@acme.com", status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="jane@acme.com",
                                       subject="Re: hi", body="Please unsubscribe me.")]
    await make_handler(state, provider)._run_native()
    rules = await state.find_suppressions("jane@acme.com")
    assert [r["source"] for r in rules] == ["opt_out"]

    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("DELETE FROM prospects WHERE id = ?", (pid,))
        await db.commit()
    # Rediscovered: a fresh, sendable record for the same address.
    again = await add_person(state, "jane@acme.com")
    await add_campaign(state, [again])
    sender = sender_with(state)
    for c in await state.get_campaigns_by_status("draft"):
        await sender._stage_campaign_native(c)
    assert await state.get_outbox(status="approved") == []

    # A CSV import skips them too.
    csv = b"email,first name\njane@acme.com,Jane\nnew@beta.io,New\n"
    preview = await ImportService(state).preview(csv)
    jane = next(r for r in preview["rows"] if r["email"] == "jane@acme.com")
    assert jane["action"] == "skip" and jane["suppressed"]
    assert "excluded" in jane["reason"]


@pytest.mark.asyncio
async def test_duplicate_opt_outs_are_one_rule(state):
    for _ in range(2):
        await state.add_suppression("email", "jane@acme.com", source="opt_out")
    assert len(await state.list_suppressions(source="opt_out")) == 1


@pytest.mark.asyncio
async def test_removing_a_manual_rule_keeps_the_opt_out(state):
    svc = ExclusionService(state)
    await state.add_suppression("email", "jane@acme.com", source="opt_out")
    manual = await svc.add("email", "jane@acme.com", reason="duplicate entry")
    removed = await svc.remove(manual["id"], note="cleanup")
    assert [r["source"] for r in removed["still_excluded_by"]] == ["opt_out"]
    assert (await svc.check("jane@acme.com"))["excluded"]


@pytest.mark.asyncio
async def test_opt_out_needs_explicit_confirmation_and_a_note(state):
    svc = ExclusionService(state)
    rule, _ = await state.add_suppression("email", "jane@acme.com", source="opt_out")
    with pytest.raises(ExclusionError) as refused:
        await svc.remove(rule["id"], note="")
    assert refused.value.code == "protected"
    with pytest.raises(ExclusionError):
        await svc.remove(rule["id"], note="she asked", confirm_opt_out=False)
    await svc.remove(rule["id"], note="she wrote asking to hear from us", confirm_opt_out=True)
    events = (await svc.get(rule["id"]))["events"]
    assert [e["action"] for e in events] == ["added", "removed"]


@pytest.mark.asyncio
async def test_audit_log_is_append_only_and_rules_are_never_deleted(state):
    rule, _ = await state.add_suppression("email", "jane@acme.com", source="manual")
    async with aiosqlite.connect(state.db_path) as db:
        with pytest.raises(Exception):
            await db.execute("UPDATE suppression_events SET note = 'x'")
        with pytest.raises(Exception):
            await db.execute("DELETE FROM suppression_events")
        with pytest.raises(Exception):
            await db.execute("DELETE FROM suppressions")


@pytest.mark.asyncio
async def test_bounce_becomes_an_exclusion(state):
    pid = await add_person(state, "jane@acme.com", status="contacted")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="s",
                                body="b", send_at=_utc(), status="sent", campaign_id="c1")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET message_id = '<m1@x>'")
        await db.commit()
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="b1", from_email="mailer-daemon@x.com",
                                       subject="Undeliverable", body="no such user",
                                       in_reply_to="<m1@x>", is_bounce=True)]
    await make_handler(state, provider)._run_native()
    assert [r["source"] for r in await state.find_suppressions("jane@acme.com")] == ["bounce"]


@pytest.mark.asyncio
async def test_migration_turns_existing_opt_outs_into_rules(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "old.db")
        full = state_module.MIGRATIONS
        monkeypatch.setattr(state_module, "MIGRATIONS", full[:13])
        old = StateManager(path)
        await old.init_db()
        await add_person(old, "gone@acme.com", status="opted_out")
        monkeypatch.setattr(state_module, "MIGRATIONS", full)
        await old.init_db()
        rules = await old.find_suppressions("gone@acme.com")
        assert [r["source"] for r in rules] == ["opt_out"]
        assert (await old.suppression_events(rules[0]["id"]))[0]["actor"] == "migration"


# ── CSV import / export ──


@pytest.mark.asyncio
async def test_csv_round_trip_only_adds(state):
    svc = ExclusionService(state)
    await state.add_suppression("email", "keep@acme.com", source="opt_out")
    data = (b"kind,value,include_subdomains,reason\n"
            b"domain,beta.io,yes,competitor\n"
            b"email,keep@acme.com,,\n"
            b",carol@gamma.com,,\n"
            b"domain,???,,\n")
    result = await svc.import_csv(data, actor="test")
    assert result["added"] == 3 and result["invalid_count"] == 1
    assert (await svc.check("x@eu.beta.io"))["excluded"]
    # The existing opt-out is untouched (still one opt_out rule, now plus an import rule).
    sources = sorted(r["source"] for r in await state.find_suppressions("keep@acme.com"))
    assert sources == ["import", "opt_out"]

    exported = await svc.export_csv()
    assert exported.splitlines()[0].startswith("kind,value,include_subdomains,source")
    assert "beta.io,1,import,competitor" in exported


# ── Company identity ──


@pytest.mark.asyncio
async def test_shared_mail_domains_never_make_a_company(state):
    await add_company(state, domain="gmail.com", name="Bogus")
    policy = ContactPolicy(state)
    a = await state.get_prospect(await add_person(state, "a@gmail.com"))
    assert await policy.company_for(a) == ""
    known = await add_company(state)
    b = await state.get_prospect(await add_person(state, "b@acme.com"))
    assert await policy.company_for(b) == known          # via the company's domain
    c = await state.get_prospect(await add_person(state, "c@gmail.com", company_id=known))
    assert await policy.company_for(c) == known          # explicit company_id wins


@pytest.mark.asyncio
async def test_gmail_contacts_are_not_limited_together(state):
    a = await add_person(state, "a@gmail.com", first="A")
    b = await add_person(state, "b@gmail.com", first="B")
    await add_campaign(state, [a, b], steps=1)
    provider = FakeProvider()
    await sender_with(state, provider, max_new_per_day=1)._run_native()
    assert sorted(m["to"] for m in provider.sent) == ["a@gmail.com", "b@gmail.com"]
    item = (await state.get_outbox(status="sent"))[0]
    verdict = await ContactPolicy(state).explain({**item, "status": "approved"})
    assert verdict.code == "company_unknown"


# ── Company limits ──


@pytest.mark.asyncio
async def test_daily_limit_holds_across_two_campaigns(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await add_campaign(state, [a], steps=1, name="one")
    await add_campaign(state, [b], steps=1, name="two")
    provider = FakeProvider()
    sender = sender_with(state, provider, max_new_per_day=1)
    await sender._run_native()
    await sender._run_native()          # a second cycle (or a restart) changes nothing
    assert len(provider.sent) == 1
    (waiting,) = await state.get_outbox(status="approved")
    policy = ContactPolicy(state)
    policy.limits = CompanyLimits(max_new_per_day=1)
    verdict = await policy.explain(waiting)
    assert verdict.code == "company_daily_limit" and "1 of 1" in verdict.reason


@pytest.mark.asyncio
async def test_daily_limit_is_a_rolling_24_hours(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await state.add_outbox_item(prospect_id=a, to_email="a@acme.com", subject="s", body="b",
                                send_at=_utc(hours=-26), status="sent", campaign_id="c1",
                                company_id=company)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET sent_at = ?", (_utc(hours=-25),))
        await db.commit()
    await state.add_outbox_item(prospect_id=b, to_email="b@acme.com", subject="s", body="b",
                                send_at=_utc(), status="approved", campaign_id="c2")
    (item,) = await state.get_outbox(status="approved")
    claim, _ = await state.claim_for_send(item, "m@x.co", company_id=company, max_new_per_day=1)
    assert claim == "claimed"
    # ...and one sent 23 hours ago still counts.
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET status = 'sent', sent_at = ? WHERE id != ?",
                         (_utc(hours=-23), item["id"]))
        await db.execute("UPDATE outbox SET status = 'approved' WHERE id = ?", (item["id"],))
        await db.commit()
    item = await state.get_outbox_item(item["id"])
    claim, detail = await state.claim_for_send(item, "m@x.co", company_id=company,
                                               max_new_per_day=1)
    assert claim == "company_daily_limit" and detail["new_today"] == 1


@pytest.mark.asyncio
async def test_concurrent_claims_cannot_both_take_the_last_slot(state):
    company = await add_company(state)
    items = []
    for i in range(6):
        pid = await add_person(state, f"p{i}@acme.com", company, first=f"P{i}")
        await state.add_outbox_item(prospect_id=pid, to_email=f"p{i}@acme.com", subject="s",
                                    body="b", send_at=_utc(), status="approved",
                                    campaign_id=f"c{i}")
    items = await state.get_outbox(status="approved")
    # Two sender processes, each with its own StateManager and connection.
    other = StateManager(state.db_path)
    results = await asyncio.gather(*[
        (state if n % 2 else other).claim_for_send(item, "m@x.co", company_id=company,
                                                   max_new_per_day=2)
        for n, item in enumerate(items)
    ])
    assert sum(1 for claim, _ in results if claim == "claimed") == 2


@pytest.mark.asyncio
async def test_failed_first_touch_frees_the_slot_and_success_counts_once(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await add_campaign(state, [a], steps=1, name="one")
    provider = FakeProvider()
    provider.fail_next = True                       # a 5xx-style verdict: no retry
    sender = sender_with(state, provider, max_new_per_day=1)
    await sender._run_native()
    assert provider.sent == []
    assert len(await state.get_outbox(status="failed")) == 1

    await add_campaign(state, [b], steps=1, name="two")
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["b@acme.com"]
    usage = await state.company_contact_usage(company)
    assert usage["new_today"] == 1


@pytest.mark.asyncio
async def test_transient_retry_keeps_one_slot_for_one_person(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    await add_campaign(state, [a], steps=1)

    class Flaky(FakeProvider):
        async def send_email(self, *args, **kwargs):
            from mercury.integrations.mail_provider import SendResult
            if not self.sent and not getattr(self, "tripped", False):
                self.tripped = True
                return SendResult(ok=False, error="421 try again later")
            return await super().send_email(*args, **kwargs)

    provider = Flaky()
    sender = sender_with(state, provider, max_new_per_day=1)
    await sender._run_native()
    (retry,) = await state.get_outbox(status="approved")
    assert retry["error"].startswith("retry 1/3")
    assert (await state.company_contact_usage(company))["new_today"] == 0

    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET send_at = ?", (_utc(minutes=-1),))
        await db.commit()
    await sender._run_native()
    assert len(provider.sent) == 1
    assert (await state.company_contact_usage(company))["new_today"] == 1


@pytest.mark.asyncio
async def test_active_limit_counts_unfinished_sequences_until_ended(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await add_campaign(state, [a], steps=2, name="one")
    provider = FakeProvider()
    sender = sender_with(state, provider, max_active=1)
    await sender._run_native()                      # a: step 1 out, step 2 queued
    await add_campaign(state, [b], steps=2, name="two")
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["a@acme.com"]
    assert (await state.company_contact_usage(company))["active"] == 1

    # Ending a's sequence (rejecting what is left) frees the slot.
    a_step2 = next(i for i in await state.get_outbox(status="approved")
                   if i["to_email"] == "a@acme.com")
    await state.reject_outbox_item(a_step2["id"])
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["a@acme.com", "b@acme.com"]


# ── Company holds ──


async def _two_colleagues_in_flight(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await add_campaign(state, [a, b], steps=2)
    provider = FakeProvider()
    sender = sender_with(state, provider)
    await sender._run_native()                      # both openers out
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET send_at = ? WHERE status = 'approved'",
                         (_utc(minutes=-1),))
        await db.execute("UPDATE campaigns SET sequence_json = replace(sequence_json, "
                         "'\"delay_days\": 3', '\"delay_days\": 0')")
        await db.commit()
    return company, a, b, provider, sender


@pytest.mark.asyncio
async def test_human_reply_holds_colleagues_but_not_the_conversation(state):
    company, a, b, provider, sender = await _two_colleagues_in_flight(state)
    provider.inbound = [InboundMessage(provider_id="in1", from_email="a@acme.com",
                                       subject="Re: hi", body="Tell me more about pricing?",
                                       message_id="<r1@acme>")]
    await make_handler(state, provider, intent="question")._run_native()
    hold = await state.get_company_hold(company)
    assert hold and hold["reason"] == "reply" and hold["prospect_id"] == a
    # The answer to a, approved (as the handler would queue it on autopilot).
    await state.add_outbox_item(prospect_id=a, to_email="a@acme.com", kind="reply",
                                subject="Re: hi", body="Happy to explain.", send_at=_utc(),
                                status="approved", in_reply_to="<r1@acme>")

    before = len(provider.sent)
    await sender._drain_due()
    new = provider.sent[before:]
    assert [m["to"] for m in new] == ["a@acme.com"]                # the reply to a
    assert new[0]["in_reply_to"] == "<r1@acme>"
    (held,) = [i for i in await state.get_outbox(status="approved")
               if i["to_email"] == "b@acme.com"]
    verdict = await ContactPolicy(state).explain(held)
    assert verdict.code == "company_hold" and "a@acme.com replied" in verdict.reason

    # Resuming lets b's follow-up go, without approving anything new.
    svc = ExclusionService(state)
    await svc.release(hold["id"], note="a is handled")
    await sender._drain_due()
    assert provider.sent[-1]["to"] == "b@acme.com"


@pytest.mark.asyncio
async def test_vacation_reply_does_not_hold_the_company(state):
    company, a, b, provider, sender = await _two_colleagues_in_flight(state)
    provider.inbound = [
        InboundMessage(provider_id="ooo1", from_email="a@acme.com",
                       subject="Automatic reply: hi", body="I'm away until Monday.",
                       headers={"Auto-Submitted": "auto-replied"}),
        InboundMessage(provider_id="rr1", from_email="b@acme.com",
                       subject="Read: hi", body="Your message was read."),
    ]
    await make_handler(state, provider, intent="ooo")._run_native()
    assert await state.get_company_hold(company) is None


@pytest.mark.asyncio
async def test_classified_out_of_office_does_not_hold_the_company(state):
    company, a, *_ = await _two_colleagues_in_flight(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="a@acme.com",
                                       subject="Re: hi", body="Back next week.")]
    await make_handler(state, provider, intent="ooo")._run_native()
    assert await state.get_company_hold(company) is None


@pytest.mark.asyncio
async def test_reply_hold_can_be_switched_off(state):
    company, a, *_ = await _two_colleagues_in_flight(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="a@acme.com",
                                       subject="Re: hi", body="Interesting, tell me more")]
    handler = make_handler(state, provider, intent="interested")
    handler.policy.limits = CompanyLimits(pause_on_reply=False)
    await handler._run_native()
    assert await state.get_company_hold(company) is None


@pytest.mark.asyncio
async def test_manual_hold_and_resume_are_logged(state):
    company = await add_company(state)
    svc = ExclusionService(state)
    hold = await svc.hold(company, note="talking to them offline", actor="test")
    again = await svc.hold(company, note="twice")
    assert again["id"] == hold["id"] and not again["created"]
    (listed,) = await svc.holds()
    assert listed["reason_text"].startswith("Paused by you")
    await svc.release(hold["id"], note="done", actor="test")
    assert await svc.holds() == []
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT action_type FROM actions ORDER BY created_at") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["company_hold", "company_resume"]
