"""Review revisions, approval snapshots, frozen batches, idempotent replays
and the audit trail (mercury/control/audit.py, the outbox and config services,
and the send claim in StateManager).

The contract: an approval covers one revision of one email's content. Any
change a reviewer would read (text, recipient, sending mailbox, a send time an
operator picks, a regenerated draft) is a new revision and sends the email
back to review, and nothing goes out on an approval for an earlier revision.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import mercury.config as config_module
import mercury.dashboard as dash
from mercury.control.audit import REDACTED, redact, redact_text, run_command
from mercury.control.context import OperatorContext
from mercury.control.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from mercury.control.outbox import OutboxService
from mercury.control.queries import QueryService
from mercury.models.prospect import Prospect
from mercury.state import MIGRATIONS, StateManager, _split_sql, outbox_hash
from tests.test_outbox_native import Cfg, FakeProvider, make_sender

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"
CLI = OperatorContext.local("cli")
DASH = OperatorContext.local("dashboard")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "mercury.db"))
    await sm.init_db()
    return sm


async def _prospect(state, email="pat@example.com"):
    return await state.add_prospect(Prospect(
        first_name="Pat", last_name="Lee", title="Owner", company="Example Co",
        email=email, email_status="verified", email_verified=True, status="new",
    ))


async def _queue(state, pid, status="pending_review", campaign_id="c1", step=1, mailbox="",
                 send_at=None, generation_id=""):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="pat@example.com", subject=f"offer_a step {step}",
        body="A short note about offer_a. Worth a look?",
        send_at=send_at or (_now() - timedelta(minutes=5)).isoformat(),
        status=status, campaign_id=campaign_id, step=step, mailbox=mailbox,
        generation_id=generation_id,
    )


def _outbox(state, ctx=CLI, **kwargs):
    return OutboxService(ctx, state, **kwargs)


def _approved_snapshot_holds(row: dict) -> bool:
    """An approved row's approval is for exactly what it holds now."""
    return (row["approved_revision"] == row["revision"] and row["approved_hash"] == outbox_hash(
        row["to_email"], row["subject"], row["body"], row["mailbox"], row["generation_id"]))


# ── Approval follows the revision ──


@pytest.mark.asyncio
async def test_editing_an_approved_draft_sends_it_back_and_the_old_approval_fails(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    outbox = _outbox(state)

    assert (await outbox.approve(item, 1))["revision"] == 1
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and row["approved_by"] == "cli:local"
    assert _approved_snapshot_holds(row)

    edited = await outbox.edit(item, "offer_a, revised", "A revised note. Worth a look?", 1)
    assert edited == {"id": item, "revision": 2, "status": "pending_review", "approval_cleared": True}
    row = await state.get_outbox_item(item)
    assert row["status"] == "pending_review" and row["approved_revision"] is None
    assert row["approved_hash"] == "" and row["approved_by"] == ""

    with pytest.raises(Conflict) as error:
        await outbox.approve(item, 1)
    assert error.value.code == "stale_revision" and error.value.details == {"revision": 2}
    assert (await state.get_outbox_item(item))["status"] == "pending_review"

    await outbox.approve(item, 2)
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and _approved_snapshot_holds(row)
    assert row["subject"] == "offer_a, revised"


@pytest.mark.asyncio
async def test_a_revision_is_required_and_must_be_a_whole_number(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    for bad, code in ((None, "revision_required"), ("", "revision_required"),
                      (0, "invalid_revision"), (True, "invalid_revision"), ("two", "invalid_revision")):
        with pytest.raises(Invalid) as error:
            await _outbox(state).approve(item, bad)
        assert error.value.code == code
    # A numeric string (a CLI argument, a form field) is fine.
    assert (await _outbox(state).approve(item, "1"))["approved"] == 1


@pytest.mark.asyncio
async def test_two_reviewers_cannot_overwrite_each_other_or_approve_an_unseen_edit(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    alice = _outbox(state, OperatorContext(client="dashboard", operator="alice"))
    bob = _outbox(state, OperatorContext(client="mcp", operator="bob"))
    seen = (await state.get_outbox_item(item))["revision"]          # both read revision 1

    await alice.edit(item, "Alice's subject", "Alice's body. Question?", seen)
    with pytest.raises(Conflict) as error:
        await bob.edit(item, "Bob's subject", "Bob's body. Question?", seen)
    assert error.value.code == "stale_revision"
    with pytest.raises(Conflict):
        await bob.approve(item, seen)                                 # Bob never saw Alice's text
    with pytest.raises(Conflict):
        await bob.reject(item, seen)
    row = await state.get_outbox_item(item)
    assert (row["subject"], row["status"], row["revision"]) == ("Alice's subject", "pending_review", 2)


@pytest.mark.asyncio
async def test_concurrent_reviewers_race_and_exactly_one_wins(state):
    pid = await _prospect(state)
    for round_ in range(5):
        item = await _queue(state, pid, campaign_id=f"race{round_}")
        alice = _outbox(state, OperatorContext(client="dashboard", operator="alice"))
        bob = _outbox(state, OperatorContext(client="mcp", operator="bob"))
        results = await asyncio.gather(
            alice.edit(item, "From Alice", "Alice wrote this. Question?", 1),
            bob.edit(item, "From Bob", "Bob wrote this. Question?", 1),
            return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert all(r.code == "stale_revision" for r in results if isinstance(r, Exception))
        assert (await state.get_outbox_item(item))["revision"] == 2

        # An approval racing an edit, both decided on revision 2: whichever
        # order SQLite runs them in, nothing ends approved for unseen text.
        await asyncio.gather(alice.approve(item, 2), bob.edit(item, "Late", "Late edit. Question?", 2),
                             return_exceptions=True)
        row = await state.get_outbox_item(item)
        assert row["status"] == "pending_review" or _approved_snapshot_holds(row)
        if row["status"] == "approved":
            assert row["subject"] != "Late"


# ── Every reviewable change invalidates approval ──


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["recipient", "mailbox", "schedule", "regenerate"])
async def test_reviewable_changes_send_an_approved_email_back(state, monkeypatch, change):
    from mercury.agents.writer import Writer

    calls = []

    async def fake_regenerate(self, item, prospect, instruction=""):
        calls.append(instruction)
        return {"subject": "offer_a, rewritten", "body": "Rewritten. Question?", "generation_id": ""}

    monkeypatch.setattr(Writer, "regenerate_email", fake_regenerate)
    pool = SimpleNamespace(legacy=SimpleNamespace(email="one@example.com"),
                           mailboxes=[SimpleNamespace(email="one@example.com"),
                                      SimpleNamespace(email="two@example.com")])
    pid = await _prospect(state)
    item = await _queue(state, pid, mailbox="one@example.com")
    outbox = _outbox(state, config=SimpleNamespace(), pool=pool, env=SimpleNamespace())
    await outbox.approve(item, 1)

    if change == "recipient":
        result = await outbox.reroute(item, 1, to_email="Sam@Example.com")
        assert result["to_email"] == "sam@example.com"
    elif change == "mailbox":
        with pytest.raises(Invalid) as error:
            await outbox.reroute(item, 1, mailbox="stranger@example.org")
        assert error.value.code == "unknown_mailbox"
        result = await outbox.reroute(item, 1, mailbox="two@example.com")
    elif change == "schedule":
        result = await outbox.reschedule(item, (_now() + timedelta(days=2)).replace(microsecond=0), 1)
    else:
        result = await outbox.regenerate(item, "shorter", expected_revision=1)
        assert calls == ["shorter"]
    assert result["revision"] == 2 and result["status"] == "pending_review"

    row = await state.get_outbox_item(item)
    assert row["status"] == "pending_review" and row["approved_revision"] is None
    with pytest.raises(Conflict):
        await outbox.approve(item, 1)
    audit = next(a for a in await state.get_audit("outbox", item) if a["outcome"] == "ok")
    assert (audit["revision_before"], audit["revision_after"], audit["outcome"]) == ("1", "2", "ok")
    assert audit["detail"]["approval_cleared"] is True


@pytest.mark.asyncio
async def test_a_stale_regeneration_does_not_spend_a_model_call(state, monkeypatch):
    from mercury.agents.writer import Writer

    calls = []

    async def fake_regenerate(self, item, prospect, instruction=""):
        calls.append(instruction)
        return {"subject": "s", "body": "b", "generation_id": ""}

    monkeypatch.setattr(Writer, "regenerate_email", fake_regenerate)
    pid = await _prospect(state)
    item = await _queue(state, pid)
    outbox = _outbox(state, config=SimpleNamespace(), env=SimpleNamespace())
    await outbox.edit(item, "Edited", "Edited body. Question?", 1)
    with pytest.raises(Conflict):
        await outbox.regenerate(item, "", expected_revision=1)
    assert calls == []


@pytest.mark.asyncio
async def test_regeneration_keeps_generation_history(state, monkeypatch):
    from mercury.agents.writer import Writer
    from mercury.personas import PersonaStore

    pid = await _prospect(state)
    store = PersonaStore(state)
    profile = await store.resolve(Cfg())
    first = await store.record(profile, Cfg(), "first prompt", {}, "personal_email")
    second = await store.record(profile, Cfg(), "second prompt", {}, "personal_email")

    async def fake_regenerate(self, item, prospect, instruction=""):
        return {"subject": "offer_a again", "body": "Second draft. Question?", "generation_id": second}

    monkeypatch.setattr(Writer, "regenerate_email", fake_regenerate)
    item = await _queue(state, pid, status="approved", generation_id=first)
    outbox = _outbox(state, config=SimpleNamespace(), env=SimpleNamespace())
    result = await outbox.regenerate(item, "", expected_revision=1)
    assert result["revision"] == 2 and result["generation_id"] == second
    assert [h["id"] for h in await store.history(item)] == [second, first]


# ── Nothing sends on an approval for an earlier revision ──


@pytest.mark.asyncio
async def test_a_stale_due_scan_cannot_send_an_edited_draft(state):
    pid = await _prospect(state)
    item = await _queue(state, pid, status="approved")
    provider = FakeProvider()
    sender = make_sender(state, provider)
    real_get_outbox = state.get_outbox

    async def scan_then_edit(*args, **kwargs):
        rows = await real_get_outbox(*args, **kwargs)
        # The reviewer edits (and someone re-approves) after the scan read the row.
        await _outbox(state).edit(item, "Edited after the scan", "New body. Question?", 1)
        await _outbox(state).approve(item, 2)
        return rows

    state.get_outbox = scan_then_edit
    await sender._drain_due()
    state.get_outbox = real_get_outbox
    assert provider.sent == []
    assert (await state.get_outbox_item(item))["status"] == "approved"

    await sender._drain_due()
    assert [m["subject"] for m in provider.sent] == ["Edited after the scan"]


@pytest.mark.asyncio
async def test_a_stale_due_scan_cannot_send_to_a_rerouted_recipient(state):
    pid = await _prospect(state)
    item = await _queue(state, pid, status="approved")
    provider = FakeProvider()
    sender = make_sender(state, provider)
    real_get_outbox = state.get_outbox

    async def scan_then_reroute(*args, **kwargs):
        rows = await real_get_outbox(*args, **kwargs)
        await _outbox(state).reroute(item, 1, to_email="other@example.com")
        await _outbox(state).approve(item, 2)
        return rows

    state.get_outbox = scan_then_reroute
    await sender._drain_due()
    state.get_outbox = real_get_outbox
    assert provider.sent == []
    assert (await state.get_outbox_item(item))["status"] == "approved"

    # A later scan must re-run the recipient gate against the changed
    # address; it cannot reuse the earlier recipient's successful check.
    current = await state.get_outbox_item(item)
    assert current["to_email"] == "other@example.com"
    assert await state.claim_outbox_item(current, "one@example.com")


@pytest.mark.asyncio
async def test_send_claim_requires_the_scanned_revision_even_if_content_returns_to_it(state):
    pid = await _prospect(state)
    item = await _queue(state, pid, status="approved")
    stale = await state.get_outbox_item(item)
    await _outbox(state).reroute(item, 1, to_email="other@example.com")
    await _outbox(state).reroute(item, 2, to_email=stale["to_email"])
    await _outbox(state).approve(item, 3)

    assert not await state.claim_outbox_item(stale, "one@example.com")
    current = await state.get_outbox_item(item)
    assert current["revision"] == 3 and current["status"] == "approved"
    assert await state.claim_outbox_item(current, "one@example.com")


@pytest.mark.asyncio
async def test_content_changed_behind_the_approval_never_sends(state, tmp_path):
    pid = await _prospect(state)
    ids = [await _queue(state, pid, status="approved", campaign_id=f"c{i}") for i in range(4)]
    db = sqlite3.connect(state.db_path)
    # Writes that bypass every service and state method.
    db.execute("UPDATE outbox SET body = 'Swapped body. Question?' WHERE id = ?", (ids[0],))
    db.execute("UPDATE outbox SET to_email = 'other@example.org' WHERE id = ?", (ids[1],))
    db.execute("UPDATE outbox SET revision = revision + 1 WHERE id = ?", (ids[2],))
    db.commit()
    db.close()
    for item in ids[:3]:
        row = await state.get_outbox_item(item)
        assert row["status"] == "approved"
        assert not await state.claim_outbox_item(row, "one@example.com"), item
    assert await state.claim_outbox_item(await state.get_outbox_item(ids[3]), "one@example.com")

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_bookkeeping_edits_drop_approval_but_sender_timing_keeps_it(state):
    pid = await _prospect(state)
    item = await _queue(state, pid, status="approved")
    await state.update_outbox_item(item, send_at=(_now() + timedelta(hours=1)).isoformat(),
                                   error="retry 1/3: deferred")
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and row["revision"] == 1 and _approved_snapshot_holds(row)

    await state.update_outbox_item(item, body="Changed by a script. Question?")
    row = await state.get_outbox_item(item)
    assert (row["status"], row["revision"], row["approved_revision"]) == ("pending_review", 2, None)


@pytest.mark.asyncio
async def test_pinning_a_rotating_email_carries_its_approval(state):
    pid = await _prospect(state)
    item = await _queue(state, pid, status="approved")              # mailbox '' = rotate
    assert await state.claim_outbox_item(await state.get_outbox_item(item), "one@example.com")
    row = await state.get_outbox_item(item)
    assert row["mailbox"] == "one@example.com" and _approved_snapshot_holds(row)

    # An interrupted send goes back to approved and can still be claimed.
    db = sqlite3.connect(state.db_path)
    db.execute("UPDATE outbox SET updated_at = '2000-01-01T00:00:00' WHERE id = ?", (item,))
    db.commit()
    db.close()
    assert await state.recover_stale_outbox() == 1
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and _approved_snapshot_holds(row)
    assert await state.claim_outbox_item(row, "one@example.com")


# ── Frozen batches ──


@pytest.mark.asyncio
async def test_approve_all_is_a_frozen_batch(state):
    pid = await _prospect(state)
    a, b, c = [await _queue(state, pid, campaign_id=f"c{i}") for i in range(3)]
    outbox = _outbox(state)
    snapshot = await outbox.pending_snapshot()
    assert snapshot == [{"id": a, "revision": 1}, {"id": b, "revision": 1}, {"id": c, "revision": 1}]

    # After the reviewer saw the list: b is edited, d is queued.
    await outbox.edit(b, "Changed", "Changed body. Question?", 1)
    d = await _queue(state, pid, campaign_id="c9")

    result = await outbox.approve_all(snapshot)
    assert (result["approved"], result["failed"]) == (2, 1)
    failed = [r for r in result["results"] if not r["ok"]]
    assert failed == [{"id": b, "ok": False, "code": "stale_revision", "revision": 2,
                       "message": failed[0]["message"]}]
    statuses = {i: (await state.get_outbox_item(i))["status"] for i in (a, b, c, d)}
    assert statuses == {a: "approved", b: "pending_review", c: "approved", d: "pending_review"}

    rows = [r for r in await state.get_audit("outbox") if r["action"] == "outbox.approve_all"]
    assert {r["batch_id"] for r in rows} == {result["batch_id"]}
    assert sorted((r["object_id"], r["outcome"]) for r in rows) == sorted(
        [(a, "ok"), (c, "ok"), (b, "stale_revision")])

    with pytest.raises(Invalid):
        await outbox.approve_all([])
    with pytest.raises(Invalid):
        await outbox.approve_all([{"id": a}])


# ── Idempotent replay ──


@pytest.mark.asyncio
async def test_replaying_a_request_key_returns_the_recorded_result(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    keyed = _outbox(state, OperatorContext.local("mcp", request_id="edit-1"))

    first = await keyed.edit(item, "Once", "Only once. Question?", 1)
    again = await keyed.edit(item, "Once", "Only once. Question?", 1)
    assert again == first and first["revision"] == 2
    assert (await state.get_outbox_item(item))["revision"] == 2      # no second bump

    with pytest.raises(Conflict) as error:
        await keyed.edit(item, "Different", "Different text. Question?", 2)
    assert error.value.code == "idempotency_key_reused"
    assert (await state.get_outbox_item(item))["subject"] == "Once"

    outcomes = [r["outcome"] for r in reversed(await state.get_audit("outbox", item))]
    assert outcomes == ["ok", "replayed", "idempotency_key_reused"]

    # The same key from another client is another request.
    other = _outbox(state, OperatorContext.local("cli", request_id="edit-1"))
    assert (await other.edit(item, "Once", "Only once. Question?", 2))["revision"] == 3


@pytest.mark.asyncio
async def test_a_recorded_failure_replays_as_the_same_failure(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    keyed = _outbox(state, OperatorContext.local("mcp", request_id="approve-1"))
    with pytest.raises(Conflict) as first:
        await keyed.approve(item, 5)
    with pytest.raises(Conflict) as again:
        await keyed.approve(item, 5)
    assert (again.value.code, str(again.value), again.value.details) == (
        first.value.code, str(first.value), first.value.details)


@pytest.mark.asyncio
async def test_a_transient_failure_releases_the_key(state, monkeypatch):
    from mercury.agents.writer import Writer

    answers = [None, {"subject": "s", "body": "Second try. Question?", "generation_id": ""}]

    async def flaky(self, item, prospect, instruction=""):
        return answers.pop(0)

    monkeypatch.setattr(Writer, "regenerate_email", flaky)
    pid = await _prospect(state)
    item = await _queue(state, pid)
    keyed = _outbox(state, OperatorContext.local("mcp", request_id="regen-1"),
                    config=SimpleNamespace(), env=SimpleNamespace())
    with pytest.raises(Unavailable):
        await keyed.regenerate(item, "", expected_revision=1)
    first = await keyed.regenerate(item, "", expected_revision=1)     # runs again
    assert first["revision"] == 2 and answers == []
    assert await keyed.regenerate(item, "", expected_revision=1) == json.loads(json.dumps(first))


def _client(tmp_path, monkeypatch):
    db = tmp_path / "mercury.db"
    config_path = tmp_path / "mercury.yaml"
    config_path.write_text(TEMPLATE.read_text())
    local = tmp_path / "mercury.local.yaml"
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.setattr(config_module, "_find_config_file",
                        lambda: str(local if local.exists() else config_path))
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_mail_context", lambda: (_ for _ in ()).throw(RuntimeError("no mail")))
    monkeypatch.setattr(dash, "_demo_config", lambda: None)
    sm = StateManager(db_path=str(db))
    asyncio.run(sm.init_db())
    client = TestClient(dash.app)
    client.sm, client.config_path, client.local_path = sm, config_path, local
    return client


def test_dashboard_replays_an_idempotency_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    pid = asyncio.run(_prospect(client.sm))
    item = asyncio.run(_queue(client.sm, pid))
    headers = {"Idempotency-Key": "desk-save-1"}
    body = {"subject": "Saved", "body": "Saved once. Question?", "revision": 1}

    first = client.put(f"/api/outbox/{item}", json=body, headers=headers)
    again = client.put(f"/api/outbox/{item}", json=body, headers=headers)
    assert first.json() == again.json() == {"success": True, "revision": 2, "status": "pending_review",
                                            "approval_cleared": False}
    assert asyncio.run(client.sm.get_outbox_item(item))["revision"] == 2
    # Without a key, the same request is a new command, and stale.
    stale = client.put(f"/api/outbox/{item}", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"

    items = [{"id": item, "revision": 2}]
    first = client.post("/api/outbox/approve-all", json={"items": items}, headers={"Idempotency-Key": "all-1"})
    again = client.post("/api/outbox/approve-all", json={"items": items}, headers={"Idempotency-Key": "all-1"})
    assert first.json() == again.json() and first.json()["approved"] == 1
    assert client.post("/api/outbox/approve-all", json={}).status_code == 400
    assert client.post("/api/outbox/approve-all").status_code == 400


def test_dashboard_payloads_carry_revisions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    pid = asyncio.run(_prospect(client.sm))
    item = asyncio.run(_queue(client.sm, pid, send_at=(_now() + timedelta(days=1)).isoformat()))
    assert client.get("/api/outbox").json()["pending"][0]["revision"] == 1
    assert client.get(f"/api/outbox/{item}").json()["revision"] == 1
    day = _now().date()
    events = client.get("/api/calendar", params={"start": day.isoformat(),
                                                 "end": (day + timedelta(days=3)).isoformat()}).json()
    assert [e["revision"] for e in events["items"]] == [1]
    r = client.post(f"/api/outbox/{item}/approve", json={"revision": 3})
    assert r.status_code == 409 and r.json()["code"] == "stale_revision"
    assert client.post(f"/api/outbox/{item}/approve").status_code == 400


# ── Config revisions ──


def test_config_changes_need_the_revision_that_was_read(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    seen = client.get("/api/config").json()["revision"]
    first = client.patch("/api/config", json={"revision": seen, "changes": {
        "channels.email.max_daily_sends": 20}})
    assert first.status_code == 200 and first.json()["revision"] != seen
    before = client.local_path.read_bytes()

    # A second client still holding the old revision cannot overwrite it.
    stale = client.patch("/api/config", json={"revision": seen, "changes": {
        "channels.email.max_daily_sends": 40}})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert stale.json()["revision"] == first.json()["revision"]
    assert client.local_path.read_bytes() == before

    # A hand edit is a new revision too.
    client.local_path.write_text(client.local_path.read_text() + "\n# edited by hand\n")
    r = client.patch("/api/config", json={"revision": first.json()["revision"],
                                          "changes": {"channels.email.max_daily_sends": 30}})
    assert r.status_code == 409
    assert client.patch("/api/config", json={"changes": {"channels.email.max_daily_sends": 30}}
                        ).json()["code"] == "revision_required"

    audit = asyncio.run(client.sm.get_audit("config"))
    assert [a["outcome"] for a in reversed(audit)] == ["ok", "stale_revision", "stale_revision",
                                                       "revision_required"]
    ok = audit[-1]
    assert (ok["revision_before"], ok["revision_after"]) == (seen, first.json()["revision"])
    assert ok["detail"]["changes"] == {"channels.email.max_daily_sends": 20}


# ── Audit and redaction ──


@pytest.mark.asyncio
async def test_audit_records_who_what_revisions_and_outcome(state):
    pid = await _prospect(state)
    item = await _queue(state, pid)
    keyed = _outbox(state, OperatorContext(client="dashboard", operator="alice", request_id="r-9"))
    await keyed.edit(item, "Edited", "Edited body. Question?", 1)
    alice = _outbox(state, OperatorContext(client="dashboard", operator="alice"))
    with pytest.raises(Conflict):
        await alice.approve(item, 1)
    reader = _outbox(state, OperatorContext(client="mcp", operator="viewer", scopes=frozenset({"read"})))
    with pytest.raises(Forbidden):
        await reader.reject(item, 2)
    with pytest.raises(NotFound):
        await alice.approve("missing", 1)

    rows = list(reversed(await QueryService(CLI, state).audit(limit=10)))
    assert [(r["client"], r["operator"], r["action"], r["object_id"], r["revision_before"],
             r["revision_after"], r["outcome"]) for r in rows] == [
        ("dashboard", "alice", "outbox.edit", item, "1", "2", "ok"),
        ("dashboard", "alice", "outbox.approve", item, "1", "", "stale_revision"),
        ("mcp", "viewer", "outbox.reject", item, "2", "", "missing_scope"),
        ("dashboard", "alice", "outbox.approve", "missing", "1", "", "not_found"),
    ]
    assert rows[0]["request_key"] == "r-9" and rows[0]["at"]
    assert (await state.get_outbox_item(item))["status"] == "pending_review"

    db = sqlite3.connect(state.db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_log SET outcome = 'ok'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_log")
    db.close()


def test_redaction_masks_secret_shapes_and_keys(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "env-secret-value-1")
    text = ("login failed: password=hunter2 for smtp://bob:pa55word@mail.example.com "
            "with Authorization: Bearer abcdef123456 and {\"api_key\": \"k-123\"} "
            "using env-secret-value-1")
    cleaned = redact_text(text)
    for secret in ("hunter2", "pa55word", "abcdef123456", "k-123", "env-secret-value-1"):
        assert secret not in cleaned
    assert "mail.example.com" in cleaned and "login failed" in cleaned
    # Our own refusal messages name a field without being mangled.
    assert redact_text("SMTP_PASSWORD: secrets cannot be changed here") == \
        "SMTP_PASSWORD: secrets cannot be changed here"
    assert redact({"to": "pat@example.com", "smtp_password": "x", "nested": [{"token": "t"}]}) == \
        {"to": "pat@example.com", "smtp_password": REDACTED, "nested": [{"token": REDACTED}]}


@pytest.mark.asyncio
async def test_failures_are_audited_with_secrets_redacted(state, monkeypatch):
    async def leaky(trail):
        raise RuntimeError("SMTP auth failed for smtp://bob:pa55word@mail.example.com password=hunter2")

    ctx = OperatorContext.local("mcp", request_id="leak-1")
    with pytest.raises(RuntimeError):
        await run_command(state, ctx, "outbox.test", scope="edit", params={}, work=leaky,
                          object_type="outbox", object_id="o1", revision_before=1)

    async def details(trail):
        trail.record("o2", 1, 2, smtp_password="hunter2", note="token=abc12345")
        return {"ok": True}

    await run_command(state, OperatorContext.local("mcp"), "outbox.test", scope="edit",
                      params={}, work=details, object_type="outbox")
    dumped = json.dumps(await state.get_audit())
    assert "pa55word" not in dumped and "hunter2" not in dumped and "abc12345" not in dumped
    assert "mail.example.com" in dumped
    # An unexpected failure is not recorded for replay: the key can run again.
    async def fine(trail):
        return {"ran": True}
    assert await run_command(state, ctx, "outbox.test", scope="edit", params={}, work=fine,
                             object_type="outbox") == {"ran": True}


def test_dashboard_errors_are_redacted(tmp_path, monkeypatch):
    from mercury.agents.writer import Writer

    async def explode(self, item, prospect, instruction=""):
        raise RuntimeError("provider said: api_key=sk-live-123456 rejected")

    monkeypatch.setattr(Writer, "regenerate_email", explode)
    monkeypatch.setattr("mercury.config.load_config", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("mercury.config.load_env", lambda *a, **k: SimpleNamespace())
    client = _client(tmp_path, monkeypatch)
    pid = asyncio.run(_prospect(client.sm))
    item = asyncio.run(_queue(client.sm, pid))
    r = client.post(f"/api/outbox/{item}/regenerate", json={"revision": 1})
    assert r.status_code == 500 and "sk-live-123456" not in r.text
    assert "sk-live-123456" not in json.dumps(asyncio.run(client.sm.get_audit()))


# ── Migration ──


def test_legacy_database_keeps_drafts_and_generation_history(tmp_path):
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    for script in MIGRATIONS[:15]:
        for statement in _split_sql(script):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 15")
    conn.execute("INSERT INTO personas (id, name, avatar_seed) VALUES ('pa', 'Voice A', 'seed')")
    conn.execute("INSERT INTO persona_versions (id, persona_id, revision, tone) "
                 "VALUES ('pv1', 'pa', 1, 'direct')")
    for gen in ("g1", "g2"):
        conn.execute("INSERT INTO email_generations (id, persona_version_id, persona_json, config_json, "
                     "prompt, output_json, task) VALUES (?, 'pv1', '{}', '{}', 'prompt', '{}', "
                     "'personal_email')", (gen,))
    rows = [
        ("o-pending", "pending_review", "Draft subject", "Draft body.", "", "g2"),
        ("o-approved", "approved", "Approved subject", "Approved body.", "one@example.com", "g1"),
        ("o-sending", "sending", "Sending subject", "Sending body.", "one@example.com", ""),
        ("o-sent", "sent", "Sent subject", "Sent body.", "one@example.com", ""),
    ]
    for i, (oid, status, subject, body, mailbox, gen) in enumerate(rows):
        conn.execute("INSERT INTO outbox (id, campaign_id, prospect_id, step, to_email, subject, body, "
                     "status, send_at, mailbox, generation_id, manually_edited) "
                     "VALUES (?, ?, 'p1', 1, 'pat@example.com', ?, ?, ?, '2026-01-01T00:00:00', ?, ?, ?)",
                     (oid, f"c{i}", subject, body, status, mailbox, gen, int(oid == "o-approved")))
    conn.execute("INSERT INTO email_generation_history VALUES ('o-pending', 'g1', 'First', 'First draft.', "
                 "'2026-01-01')")
    conn.execute("INSERT INTO email_generation_history VALUES ('o-pending', 'g2', 'Draft subject', "
                 "'Draft body.', '2026-01-02')")
    conn.execute("INSERT INTO email_generation_history VALUES ('o-approved', 'g1', 'Approved subject', "
                 "'Approved body.', '2026-01-01')")
    conn.commit()
    conn.close()

    state = StateManager(db)
    asyncio.run(state.init_db())

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM outbox")}
    for oid, status, subject, body, mailbox, gen in rows:
        row = after[oid]
        assert (row["status"], row["subject"], row["body"], row["mailbox"], row["generation_id"],
                row["revision"]) == (status, subject, body, mailbox, gen, 1)
    assert after["o-approved"]["manually_edited"] == 1
    for oid in ("o-approved", "o-sending"):
        assert after[oid]["approved_by"] == "legacy" and _approved_snapshot_holds(after[oid])
    for oid in ("o-pending", "o-sent"):
        assert after[oid]["approved_revision"] is None and after[oid]["approved_hash"] == ""
    history = conn.execute("SELECT outbox_id, generation_id, original_subject FROM email_generation_history "
                           "ORDER BY outbox_id, generation_id").fetchall()
    assert [tuple(h) for h in history] == [("o-approved", "g1", "Approved subject"),
                                           ("o-pending", "g1", "First"),
                                           ("o-pending", "g2", "Draft subject")]
    assert conn.execute("SELECT COUNT(*) FROM email_generations").fetchone()[0] == 2
    conn.close()

    # The legacy approval still sends; the legacy draft still needs review.
    approved = asyncio.run(state.get_outbox_item("o-approved"))
    assert asyncio.run(state.claim_outbox_item(approved, "one@example.com"))
    pending = asyncio.run(state.get_outbox_item("o-pending"))
    assert asyncio.run(_outbox(state).approve("o-pending", 1))["approved"] == 1
    assert pending["revision"] == 1
    # Migrating again changes nothing.
    asyncio.run(StateManager(db).init_db())
