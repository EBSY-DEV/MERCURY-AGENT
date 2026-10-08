"""Approval, capacity and bounce regressions found in the contact-policy audit."""

import aiosqlite
import pytest
import pytest_asyncio

import mercury.state as state_module
from mercury.control.exclusions import ExclusionService
from mercury.control.context import OperatorContext
from mercury.control.outbox import OutboxService
from mercury.integrations.mail_provider import InboundMessage
from mercury.state import StateManager
from tests.test_contact_policy import add_campaign, add_company, add_person, sender_with, _utc
from tests.test_outbox_native import FakeProvider, make_handler


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "state.db"))
    await sm.init_db()
    return sm


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_all", [False, True])
async def test_requeued_followup_waits_for_explicit_approval_after_restart(state, monkeypatch, approve_all):
    pid = await add_person(state, "a@acme.com", first="A")
    await add_campaign(state, [pid], steps=2)
    provider = FakeProvider()
    sender = sender_with(state, provider)
    await sender._run_native()
    svc = ExclusionService(state)
    rule = await svc.add("email", "a@acme.com")
    (blocked,) = await state.get_outbox(status="blocked")
    await svc.remove(rule["id"])
    await svc.requeue(blocked["id"])
    # Make the follow-up due, then restart both the state manager and sender.
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET sent_at = ? WHERE status = 'sent'", (_utc(days=-4),))
        await db.execute("UPDATE outbox SET send_at = ? WHERE id = ?",
                         (_utc(minutes=-1), blocked["id"]))
        await db.commit()
    state = StateManager(state.db_path)
    sender = sender_with(state, provider, autopilot=False)
    monkeypatch.setattr(sender.config.channels.email, "auto_approve_followups", True, raising=False)
    await sender._run_native()
    assert len(provider.sent) == 1
    assert (await state.get_outbox_item(blocked["id"]))["status"] == "pending_review"

    # A real approval, including Approve all, releases the review requirement.
    outbox = OutboxService(OperatorContext.local("cli"), state)
    if approve_all:
        assert (await outbox.approve_all(await outbox.pending_snapshot()))["approved"] == 1
    else:
        assert (await outbox.approve(blocked["id"], blocked["revision"]))["approved"] == 1
    await sender._run_native()
    assert len(provider.sent) == 2
    assert (await state.get_outbox_item(blocked["id"]))["status"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reject", "cancel"])
async def test_ending_blocked_sequence_frees_capacity_without_lifting_exclusion(state, action):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    b = await add_person(state, "b@acme.com", company, first="B")
    await add_campaign(state, [a], steps=2)
    provider = FakeProvider()
    sender = sender_with(state, provider, max_active=1)
    await sender._run_native()
    await ExclusionService(state).add("email", "a@acme.com")
    (blocked,) = await state.get_outbox(status="blocked")
    await add_campaign(state, [b], steps=2)
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["a@acme.com"]

    if action == "reject":
        assert await state.reject_outbox_item(blocked["id"]) == 1
    else:
        assert await state.cancel_pending_outbox_for_prospect(a) == 1
    assert (await state.company_contact_usage(company))["active"] == 0
    assert await state.find_suppressions("a@acme.com")
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["a@acme.com", "b@acme.com"]


@pytest.mark.asyncio
async def test_active_limit_counts_people_across_campaigns_and_claims(state):
    company = await add_company(state)
    a = await add_person(state, "a@acme.com", company, first="A")
    for campaign in ("campaign1", "campaign2"):
        for step, status in ((1, "sent"), (2, "approved")):
            await state.add_outbox_item(
                prospect_id=a, to_email="a@acme.com", subject="s", body="b",
                send_at=_utc(), status=status, campaign_id=campaign,
                step=step, company_id=company)
    assert (await state.company_contact_usage(company))["active"] == 1
    assert (await state.company_contact_usage(company, exclude_prospect=a))["active"] == 0

    # Another campaign for that person does not need a second contact slot.
    item_id = await state.add_outbox_item(
        prospect_id=a, to_email="a@acme.com", subject="s", body="b", send_at=_utc(),
        status="approved", campaign_id="campaign3", company_id=company)
    assert (await state.claim_for_send(await state.get_outbox_item(item_id), "m@x.co",
                                      company_id=company, max_active=1))[0] == "claimed"

    # A second distinct person fits under a limit of two, a third does not.
    for name, verdict in (("b", "claimed"), ("c", "company_active_limit")):
        pid = await add_person(state, f"{name}@acme.com", company, first=name.upper())
        item_id = await state.add_outbox_item(
            prospect_id=pid, to_email=f"{name}@acme.com", subject="s", body="b",
            send_at=_utc(), status="approved", campaign_id=name, company_id=company)
        claim, _ = await state.claim_for_send(await state.get_outbox_item(item_id), "m@x.co",
                                              company_id=company, max_active=2)
        assert claim == verdict


@pytest.mark.asyncio
async def test_v14_blocked_mail_keeps_review_requirement_on_upgrade(tmp_path, monkeypatch):
    path = str(tmp_path / "v14.db")
    migrations = state_module.MIGRATIONS
    monkeypatch.setattr(state_module, "MIGRATIONS", migrations[:14])
    state = StateManager(path)
    await state.init_db()
    # Simulate rows created by the original version of the PR. Raw SQL, since
    # add_outbox_item writes columns later migrations add.
    item_id = "blocked-step-2"
    async with aiosqlite.connect(path) as db:
        for row_id, step, status in (("sent-step-1", 1, "sent"), (item_id, 2, "blocked")):
            await db.execute(
                "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, "
                "subject, body, status, send_at) VALUES (?, 'c', 'p', ?, 'sequence', "
                "'a@acme.com', 's', 'b', ?, ?)", (row_id, step, status, _utc()))
        await db.commit()
    monkeypatch.setattr(state_module, "MIGRATIONS", migrations)
    await state.init_db()
    assert (await state.get_outbox_item(item_id))["requires_manual_review"] == 1
    assert await state.requeue_blocked_outbox(item_id) == "requeued"
    assert await state.approve_ready_followups() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code, excluded", [
    ("5.1.1", True), ("", True), ("5.7.26", False),
    ("5.7.606", False), ("4.7.28", False), ("5.2.2", False),
])
async def test_bounce_exclusions_preserve_the_dsn_classification(state, code, excluded):
    pid = await add_person(state, "a@acme.com", status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="bounce", from_email="mailer-daemon@x.com", subject="Undeliverable",
        body=f"Delivery failed: {code}", is_bounce=True,
        headers={"bounced_recipient": "a@acme.com", "dsn_status": code})]
    await make_handler(state, provider)._run_native()
    assert bool(await state.find_suppressions("a@acme.com")) == excluded
    prospect = await state.get_prospect(pid)
    assert (prospect.email_status == "invalid") == excluded
