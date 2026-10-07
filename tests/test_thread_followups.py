"""Sequence follow-ups go out as replies in the opener's thread
(channels.email.thread_followups, on by default)."""

import base64
import email
import email.policy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from mercury.models.campaign import Campaign, EmailStep
from mercury.state import reply_subject, thread_headers, wire_subject
from tests.test_inbox_settings import client  # noqa: F401 (fixture)
from tests.test_outbox_native import (  # noqa: F401 (state is a fixture)
    Cfg,
    FakeProvider,
    make_sender,
    seed_prospect,
    state,
)


def _ago(days):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()


async def seed_sequence(state, pid, steps=3):
    sequence = [
        EmailStep(step=1, subject="quick question for {{company}}",
                  body="Body for {{company}}. Question?", delay_days=0),
        EmailStep(step=2, subject="another angle", body="Another angle {{first_name}}.",
                  delay_days=3),
        EmailStep(step=3, subject="last note", body="One last idea for {{company}}.",
                  delay_days=4),
    ][:steps]
    campaign = Campaign(id="", name="segment_a", channel="email", sequence=sequence,
                        prospect_ids=[pid], status="draft")
    campaign.id = await state.add_campaign(campaign)
    return campaign


async def rows_by_step(state):
    rows = await state.get_outbox(limit=50)
    return {r["step"]: r for r in rows}


async def make_due(state, step):
    """Pretend the previous step went out long ago, so ``step`` is due now."""
    rows = await rows_by_step(state)
    await state.update_outbox_item(rows[step - 1]["id"], sent_at=_ago(30))
    await state.update_outbox_item(rows[step]["id"], send_at=_ago(1))


@pytest.fixture
def threading_off(monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "thread_followups", False, raising=False)


# ── helpers ──


def test_reply_subject_never_double_prefixes():
    assert reply_subject("hello") == "Re: hello"
    assert reply_subject("Re: hello") == "Re: hello"
    assert reply_subject("RE: hello") == "RE: hello"


def test_thread_headers_carry_the_whole_chain():
    step1 = {"message_id": "<a@example.com>", "thread_ref": "T", "subject": "hello"}
    h2 = thread_headers(step1)
    assert h2 == {"in_reply_to": "<a@example.com>", "thread_ref": "T",
                  "thread_references": "<a@example.com>", "thread_subject": "hello"}
    step2 = {**h2, "message_id": "<b@example.com>", "subject": "writer subject"}
    h3 = thread_headers(step2)
    assert h3["in_reply_to"] == "<b@example.com>"
    assert h3["thread_references"] == "<a@example.com> <b@example.com>"
    assert h3["thread_subject"] == "hello"
    assert thread_headers({"message_id": ""}) == {} and thread_headers(None) == {}


def test_wire_subject_respects_setting_for_queued_mail_only():
    queued = {"kind": "sequence", "step": 2, "status": "approved", "subject": "own",
              "in_reply_to": "<a@x>", "thread_subject": "hello"}
    assert wire_subject(queued) == "Re: hello"
    assert wire_subject(queued, thread_followups=False) == "own"
    # A sent row records how it went out; the setting no longer decides.
    assert wire_subject({**queued, "status": "sent"}, thread_followups=False) == "Re: hello"
    # Replies and openers keep their own subject.
    assert wire_subject({**queued, "kind": "reply"}) == "own"
    assert wire_subject({**queued, "step": 1}) == "own"


# ── inheritance on send ──


@pytest.mark.asyncio
async def test_followups_inherit_thread_and_send_as_replies(state):
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()  # stages 3 steps, sends step 1 only
    assert [m["subject"] for m in provider.sent] == ["quick question for Acme"]
    assert provider.sent[0]["in_reply_to"] == "" and provider.sent[0]["references"] == ""
    rows = await rows_by_step(state)
    for step in (2, 3):
        assert rows[step]["in_reply_to"] == "<m1@x>"
        assert rows[step]["thread_ref"] == "t1"
        assert rows[step]["thread_references"] == "<m1@x>"
        assert rows[step]["thread_subject"] == "quick question for Acme"
        assert wire_subject(rows[step]) == "Re: quick question for Acme"
    # The writer's subject stays in the row for review.
    assert rows[2]["subject"] == "another angle"

    await make_due(state, 2)
    await sender._drain_due()
    step2 = provider.sent[1]
    assert step2["subject"] == "Re: quick question for Acme"
    assert step2["in_reply_to"] == "<m1@x>"
    assert step2["references"] == "<m1@x>"
    assert step2["thread_ref"] == "t1"
    rows = await rows_by_step(state)
    assert rows[2]["status"] == "sent" and rows[2]["subject"] == "another angle"
    assert wire_subject(rows[2], thread_followups=False) == "Re: quick question for Acme"
    # Step 3 now replies to step 2, with the whole chain in References.
    assert rows[3]["in_reply_to"] == "<m2@x>"
    assert rows[3]["thread_references"] == "<m1@x> <m2@x>"
    assert rows[3]["thread_subject"] == "quick question for Acme"

    await make_due(state, 3)
    await sender._drain_due()
    step3 = provider.sent[2]
    assert step3["subject"] == "Re: quick question for Acme"
    assert step3["in_reply_to"] == "<m2@x>"
    assert step3["references"] == "<m1@x> <m2@x>"


@pytest.mark.asyncio
async def test_regenerate_and_edit_after_send_keep_inherited_headers(state):
    from mercury.personas import PersonaStore

    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid, steps=2)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    step2 = (await rows_by_step(state))[2]
    assert step2["in_reply_to"] == "<m1@x>"

    await PersonaStore(state).replace_draft(step2["id"], {
        "subject": "rewritten angle", "body": "A rewritten follow-up. Question?",
        "generation_id": "",
    })
    assert await state.edit_outbox_item(step2["id"], subject="edited angle",
                                        body="An edited follow-up. Question?",
                                        manually_edited=1)
    step2 = await state.get_outbox_item(step2["id"])
    assert step2["status"] == "pending_review"
    assert step2["in_reply_to"] == "<m1@x>" and step2["thread_ref"] == "t1"
    assert step2["thread_references"] == "<m1@x>"
    assert wire_subject(step2) == "Re: quick question for Acme"

    await state.approve_outbox(step2["id"])
    await make_due(state, 2)
    await sender._drain_due()
    sent = provider.sent[1]
    assert sent["body"].startswith("An edited follow-up.")
    assert sent["subject"] == "Re: quick question for Acme"
    assert sent["in_reply_to"] == "<m1@x>"


@pytest.mark.asyncio
async def test_followup_staged_after_the_opener_was_sent_inherits(state):
    pid = await seed_prospect(state, email="owner@example.com")
    campaign = await seed_sequence(state, pid, steps=2)
    # The writer stages a personal step 1 itself; it can go out before the
    # sender stages the rest of the campaign.
    opener = await state.add_outbox_item(
        prospect_id=pid, campaign_id=campaign.id, step=1, to_email="owner@example.com",
        subject="a personal opener", body="Personal body. Question?",
        send_at=_ago(1), status="approved",
    )
    await state.update_outbox_item(opener, status="sent", sent_at=_ago(1),
                                   message_id="<p1@example.com>", thread_ref="T9")
    sender = make_sender(state, FakeProvider(), require_approval=True)
    await sender._stage_campaign_native(campaign)

    step2 = (await rows_by_step(state))[2]
    assert step2["in_reply_to"] == "<p1@example.com>" and step2["thread_ref"] == "T9"
    assert wire_subject(step2) == "Re: a personal opener"


@pytest.mark.asyncio
async def test_failed_opener_leaves_followups_unthreaded(state):
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid, steps=2)
    provider = FakeProvider()
    provider.fail_next = True  # a permanent failure, not a retry
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()

    rows = await rows_by_step(state)
    assert rows[1]["status"] == "failed"
    assert rows[2]["in_reply_to"] == "" and rows[2]["thread_references"] == ""
    assert wire_subject(rows[2]) == "another angle"
    assert await state.thread_followups(rows[2]["campaign_id"], pid) == 0


@pytest.mark.asyncio
async def test_replies_keep_their_own_headers(state):
    pid = await seed_prospect(state, email="owner@example.com")
    await state.add_outbox_item(
        prospect_id=pid, to_email="owner@example.com", subject="Re: hello",
        body="Thanks for the note.", send_at=_ago(1), status="approved",
        kind="reply", in_reply_to="<in@example.com>", thread_ref="T1",
    )
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent[0]["subject"] == "Re: hello"
    assert provider.sent[0]["in_reply_to"] == "<in@example.com>"
    assert provider.sent[0]["references"] == ""


# ── setting off ──


@pytest.mark.asyncio
async def test_setting_off_sends_followups_as_new_emails(state, threading_off):
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid, steps=2)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()

    step2 = (await rows_by_step(state))[2]
    assert step2["in_reply_to"] == "" and step2["thread_ref"] == ""
    assert wire_subject(step2, thread_followups=False) == "another angle"

    await make_due(state, 2)
    await sender._drain_due()
    sent = provider.sent[1]
    assert sent["subject"] == "another angle"
    assert sent["in_reply_to"] == "" and sent["thread_ref"] == "" and sent["references"] == ""


@pytest.mark.asyncio
async def test_turning_off_after_inheritance_still_sends_a_new_email(state, monkeypatch):
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid, steps=2)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    assert (await rows_by_step(state))[2]["in_reply_to"] == "<m1@x>"

    monkeypatch.setattr(Cfg.channels.email, "thread_followups", False, raising=False)
    await make_due(state, 2)
    await sender._drain_due()
    sent = provider.sent[1]
    assert sent["subject"] == "another angle" and sent["in_reply_to"] == ""
    # The sent row records that it went out unthreaded.
    step2 = (await rows_by_step(state))[2]
    assert step2["in_reply_to"] == "" and wire_subject(step2) == "another angle"


# ── providers: what actually goes on the wire ──


class SmtpEnv(SimpleNamespace):
    def __init__(self):
        super().__init__(smtp_host="smtp.example.com", smtp_port=587,
                         smtp_username="sender@example.com", smtp_password="pw")


@pytest.mark.asyncio
async def test_smtp_raw_message_threads_the_whole_chain(state, monkeypatch):
    from mercury.integrations import smtp_mail

    captured = []

    async def fake_send(msg, **_kwargs):
        captured.append(email.message_from_bytes(msg.as_bytes(), policy=email.policy.default))

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    provider = smtp_mail.SmtpImapProvider(Cfg(), SmtpEnv())
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid)
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()
    await make_due(state, 2)
    await sender._drain_due()
    await make_due(state, 3)
    await sender._drain_due()

    first, second, third = captured
    id1, id2 = first["Message-ID"], second["Message-ID"]
    assert first["In-Reply-To"] is None and first["References"] is None
    assert second["In-Reply-To"] == id1 and second["References"] == id1
    assert second["Subject"] == "Re: quick question for Acme"
    assert third["In-Reply-To"] == id2
    assert third["References"].split() == [id1, id2]
    assert third["Subject"] == "Re: quick question for Acme"


@pytest.mark.asyncio
async def test_smtp_references_falls_back_to_in_reply_to(monkeypatch):
    from mercury.integrations import smtp_mail

    captured = []

    async def fake_send(msg, **_kwargs):
        captured.append(msg)

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    provider = smtp_mail.SmtpImapProvider(Cfg(), SmtpEnv())
    await provider.send_email("owner@example.com", "Re: hi", "Thanks.", in_reply_to="<a@x>")
    assert captured[0]["In-Reply-To"] == "<a@x>" and captured[0]["References"] == "<a@x>"


@pytest.mark.asyncio
async def test_gmail_send_carries_thread_id_and_references(monkeypatch):
    from mercury.integrations.gmail import GmailProvider

    calls = []

    async def fake_request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            return {"id": "g2", "threadId": "T1"}
        return {"payload": {"headers": [{"name": "Message-ID", "value": "<g2@mail.example.com>"}]}}

    monkeypatch.setattr(GmailProvider, "_request", fake_request)
    provider = GmailProvider(Cfg(), SimpleNamespace(gmail_client_id="id", gmail_client_secret="s"))
    result = await provider.send_email(
        "owner@example.com", "Re: quick question", "Body.", thread_ref="T1",
        in_reply_to="<b@x>", references="<a@x> <b@x>",
    )
    assert result.ok and result.thread_ref == "T1"
    payload = calls[0][2]["json"]
    assert payload["threadId"] == "T1"
    raw = email.message_from_bytes(base64.urlsafe_b64decode(payload["raw"]),
                                   policy=email.policy.default)
    assert raw["In-Reply-To"] == "<b@x>"
    assert raw["References"] == "<a@x> <b@x>"
    assert raw["Subject"] == "Re: quick question"


# ── review surfaces ──


@pytest.mark.asyncio
async def test_outbox_api_shows_the_wire_subject(client, state, monkeypatch):  # noqa: F811
    import mercury.dashboard as dash
    from pathlib import Path

    monkeypatch.setattr(dash, "DB_PATH", Path(state.db_path))
    pid = await seed_prospect(state, email="owner@example.com")
    await seed_sequence(state, pid, steps=2)
    await make_sender(state, FakeProvider(), require_approval=False)._run_native()

    data = await dash.get_outbox_api()
    step2 = next(r for r in data["approved"] if r["step"] == 2)
    assert step2["wire_subject"] == "Re: quick question for Acme"
    assert step2["subject"] == "another angle"
    sent = data["sent"][0]
    assert sent["wire_subject"] == sent["subject"] == "quick question for Acme"


def test_cli_outbox_prints_the_wire_subject(tmp_path, monkeypatch, capsys):
    import asyncio
    import mercury.cli as cli
    import mercury.state as state_module
    from mercury.state import StateManager

    db = str(tmp_path / "cli.db")

    async def seed():
        sm = StateManager(db)
        await sm.init_db()
        pid = await seed_prospect(sm, email="owner@example.com")
        await seed_sequence(sm, pid, steps=2)
        await make_sender(sm, FakeProvider(), require_approval=True)._stage_campaign_native(
            (await sm.get_campaigns_by_status("draft"))[0])
        rows = await rows_by_step(sm)
        await sm.update_outbox_item(rows[1]["id"], status="sent", sent_at=_ago(1),
                                    message_id="<a@example.com>", thread_ref="T")
        await sm.thread_followups(rows[1]["campaign_id"], pid)

    asyncio.run(seed())
    monkeypatch.setattr(state_module, "StateManager", lambda: StateManager(db))
    cli.cmd_outbox(SimpleNamespace(approve_all=False, approve=None, reject=None))
    out = capsys.readouterr().out
    assert "Subject: Re: quick question for Acme" in out
    assert "drafted subject: another angle" in out


# ── the Settings switch ──


def test_settings_toggle_round_trips_to_the_private_config(client):  # noqa: F811
    assert client.get("/api/settings/email-options").json() == {"thread_followups": True}
    before = client.config_path.read_text()

    res = client.post("/api/settings/email-options", json={"thread_followups": False})
    assert res.status_code == 200 and res.json()["success"]
    assert client.config_path.read_text() == before  # the tracked template is untouched
    saved = yaml.safe_load(client.local_path.read_text())
    assert saved["channels"]["email"]["thread_followups"] is False
    assert client.get("/api/settings/email-options").json() == {"thread_followups": False}

    res = client.post("/api/settings/email-options", json={"thread_followups": True})
    assert res.json()["success"]
    assert client.get("/api/settings/email-options").json() == {"thread_followups": True}


@pytest.mark.parametrize("payload", [
    {"thread_followups": "yes"}, {"thread_followups": 1}, {"max_daily_sends": 500}, {}, [],
])
def test_settings_toggle_rejects_anything_but_the_switch(client, payload):  # noqa: F811
    res = client.post("/api/settings/email-options", json=payload)
    assert res.status_code == 400 and not res.json()["success"]
    assert not client.local_path.exists()


def test_settings_toggle_keeps_comments(client):  # noqa: F811
    client.config_path.write_text(
        "# top comment\n" + client.config_path.read_text().replace(
            "channels:", "channels:  # keep me", 1))
    assert client.post("/api/settings/email-options",
                       json={"thread_followups": False}).json()["success"]
    text = client.local_path.read_text()
    assert "# top comment" in text and "# keep me" in text


def test_gate_does_not_count_the_reply_prefix_against_the_subject_limit():
    from mercury.gate import MAX_SUBJECT_LEN, pre_send_check

    opener = "x" * MAX_SUBJECT_LEN
    assert pre_send_check("a@example.com", reply_subject(opener), "Hi there.")
    assert not pre_send_check("a@example.com", "Re: " + opener + "x", "Hi there.")
