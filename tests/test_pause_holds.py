"""Operator pause vs. health holds (mercury/holds.py, control/sending.py).

Resume lifts only the operator pause; a bounce kill switch and its counters
survive it. Pause stops new claims; an email already claimed finishes and is
reported as in flight. The agent stops cooperatively. The dashboard and the
service list the same reasons.

Fake sender and seeded state only: nothing here sends mail.
"""

import asyncio
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

import mercury.dashboard as dash
from mercury import bounces, holds, warmup
from mercury.control import runtime as runtime_mod
from mercury.control.context import OperatorContext
from mercury.control.errors import Forbidden
from mercury.control.runtime import RuntimeService
from mercury.control.sending import SendingService
from mercury.integrations.mail_provider import SendResult
from tests.test_control_services import _run, client  # noqa: F401
from tests.test_outbox_native import (
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,  # noqa: F401
)

CLI = OperatorContext.local("cli")
KILL = "bounce rate 6/50 exceeded 2%. Check list quality before clearing the hold"


async def _seed_bounce_hold(state):
    await state.set_setting("bounce_count", "6")
    await bounces.record_bucket(state, bounces.LIST)
    await bounces.record_bucket(state, bounces.SENDER)
    assert await bounces.engage_kill_switch(state, KILL)


async def _two_due(state):
    """Two prospects with a staged, approved, due first email each."""
    ids = [await seed_prospect(state, email=f"p{i}@example.com") for i in (1, 2)]
    await seed_campaign(state, ids)
    return ids


class PausingProvider(FakeProvider):
    """Sends, but a pause (or a shutdown) lands while the first email is in
    flight. Records what status said at that moment."""

    def __init__(self, state, on_first):
        super().__init__()
        self.state, self.on_first, self.seen = state, on_first, None

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        if self.seen is None:
            await self.on_first()
            self.seen = await SendingService(CLI, self.state).status()
        return await super().send_email(to_email, subject, body, thread_ref, in_reply_to)


# ── Resume never lifts a health hold ──


@pytest.mark.asyncio
async def test_resume_during_a_health_hold_leaves_sending_blocked_and_keeps_counters(state):
    await _two_due(state)
    await _seed_bounce_hold(state)
    service = SendingService(CLI, state)
    await service.pause()
    before = await holds.bounce_counters(state)

    resumed = await service.resume()
    assert resumed["paused"] is False and resumed["reason"] == ""
    assert resumed["blocked"] is True
    assert [(h["kind"], h["reason"]) for h in resumed["holds"]] == [("bounce_kill_switch", KILL)]
    assert resumed["bounce_counters"] == before
    assert before["bounces"] == 6 and before["buckets"]["SENDER"] == 1
    assert await state.get_setting("sending_paused") == KILL

    provider = FakeProvider()
    await make_sender(state, provider, require_approval=False)._run_native()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_resume_while_only_health_blocked_changes_nothing(state):
    await _seed_bounce_hold(state)
    service = SendingService(CLI, state)
    before = await service.status()
    assert before["blocked"] and not before["paused"]
    assert await service.resume() == before


@pytest.mark.asyncio
async def test_clearing_the_health_hold_is_its_own_admin_command(state):
    await _seed_bounce_hold(state)
    await SendingService(CLI, state).pause()

    runner = OperatorContext(client="mcp", scopes=frozenset({"read", "run"}))
    with pytest.raises(Forbidden):
        await SendingService(runner, state).clear_hold()
    assert await state.get_setting("sending_paused") == KILL

    cleared = await SendingService(CLI, state).clear_hold()
    assert cleared["cleared"]["reason"] == KILL and cleared["cleared"]["bounces"] == 6
    assert cleared["holds"] == [] and cleared["bounce_counters"]["bounces"] == 0
    # Only the hold: the operator pause is still the person's to lift.
    assert cleared["paused"] and cleared["blocked"]
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT agent, details_json FROM actions "
                              "WHERE action_type = 'sending_hold_cleared'") as cursor:
            logged = await cursor.fetchall()
    assert len(logged) == 1 and logged[0][0] == "cli" and '"bounces": 6' in logged[0][1]

    # Nothing to clear: the counters that feed the next check stay.
    await state.set_setting("bounce_count", "3")
    again = await SendingService(CLI, state).clear_hold()
    assert again["cleared"] is None and again["bounce_counters"]["bounces"] == 3


# ── Pause before and after a claim ──


@pytest.mark.asyncio
async def test_pause_before_claim_sends_nothing(state):
    await _two_due(state)
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await SendingService(CLI, state).pause()
    await sender._run_native()
    assert sender.provider.sent == []
    assert await state.get_outbox(status="sending") == []

    await SendingService(CLI, state).resume()
    await sender._run_native()
    assert len(sender.provider.sent) == 2


@pytest.mark.asyncio
async def test_pause_after_claim_finishes_the_email_in_flight_and_claims_no_more(state):
    await _two_due(state)
    provider = PausingProvider(state, lambda: SendingService(CLI, state).pause())
    await make_sender(state, provider, require_approval=False)._run_native()

    # At the moment of the pause, the claimed row was reported in flight.
    seen = provider.seen
    assert seen["paused"] and seen["blocked"]
    assert [i["to_email"] for i in seen["in_flight"]] == [provider.sent[0]["to"]]
    assert seen["in_flight"][0]["interrupted"] is False

    # It finished and was recorded; the next one was never claimed.
    assert len(provider.sent) == 1
    assert len(await state.get_outbox(status="sent")) == 1
    still = [i for i in await state.get_outbox(status="approved") if i["step"] == 1]
    assert len(still) == 1
    assert (await SendingService(CLI, state).status())["in_flight"] == []


@pytest.mark.asyncio
async def test_a_hold_landing_mid_drain_also_stops_the_next_claim(state):
    await _two_due(state)
    provider = PausingProvider(state, lambda: bounces.engage_kill_switch(state, KILL))
    await make_sender(state, provider, require_approval=False)._run_native()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_an_interrupted_claim_is_reported_as_such(state):
    await _two_due(state)
    await make_sender(state, FakeProvider())._run_native()  # stage only (approval on)
    row = (await state.get_outbox(status="pending_review"))[0]
    await state.update_outbox_item(row["id"], status="sending")
    assert (await holds.in_flight(state))[0]["interrupted"] is False
    later = await holds.in_flight(state, now="2999-01-01T00:00:00")
    assert [(f["id"], f["interrupted"]) for f in later] == [(row["id"], True)]


# ── Repeats and migration ──


@pytest.mark.asyncio
async def test_repeated_pause_and_resume_are_idempotent(state):
    await state.set_setting("bounce_count", "4")
    service = SendingService(CLI, state)
    first = await service.pause()
    assert await service.pause() == first
    resumed = await service.resume()
    assert await service.resume() == resumed
    assert not resumed["blocked"] and resumed["bounce_counters"]["bounces"] == 4
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_an_old_operator_pause_moves_out_of_the_kill_switch_key(state):
    await state.set_setting("sending_paused", "paused from dashboard")
    status = await SendingService(CLI, state).status()
    assert status["paused"] and status["reason"] == "paused from dashboard"
    assert status["holds"] == []
    assert await state.get_setting("sending_paused") == ""
    assert (await SendingService(CLI, state).resume())["blocked"] is False


@pytest.mark.asyncio
async def test_an_old_free_text_reason_stays_a_health_hold(state):
    # Written by the bounce monitor before the split: fails closed.
    await state.set_setting("sending_paused", "a bounce could not be processed")
    status = await SendingService(CLI, state).resume()
    assert status["blocked"] and not status["paused"]
    assert status["holds"][0]["kind"] == "bounce_kill_switch"


# ── Cooperative stop ──


@pytest.mark.asyncio
async def test_shutdown_mid_drain_claims_nothing_new(state):
    await _two_due(state)
    stop = asyncio.Event()

    async def request_stop():
        stop.set()

    provider = PausingProvider(state, request_stop)
    sender = make_sender(state, provider, require_approval=False)
    sender.stop_event = stop
    sender.send_pacing = True  # the pacing sleep is skipped once stopping
    await asyncio.wait_for(sender._run_native(), timeout=5)
    assert len(provider.sent) == 1
    assert len(await state.get_outbox(status="sent")) == 1


def test_heartbeat_hands_its_stop_event_to_the_sender(monkeypatch):
    from mercury import main

    sender = SimpleNamespace()

    async def fake_runtime():
        return SimpleNamespace(sender=sender, config=SimpleNamespace(
            usage=SimpleNamespace(heartbeat_interval_minutes=15)))

    monkeypatch.setattr(main, "build_runtime", fake_runtime)
    stop = asyncio.Event()
    stop.set()  # already asked to stop: the loop body never runs
    asyncio.run(main.heartbeat(stop))
    assert sender.stop_event is stop


_SLOW_EXIT = textwrap.dedent("""
    import signal, sys, time
    hits = []
    signal.signal(signal.SIGTERM, lambda *a: hits.append(1))
    while not hits:
        time.sleep(0.05)
    time.sleep(1.5)              # finishing the current step
    sys.exit(len(hits))          # 1 = exactly one SIGTERM arrived
""")


def _child(code):
    child = subprocess.Popen([sys.executable, "-c", code])
    threading.Thread(target=child.wait, daemon=True).start()  # reap it
    return child


def _wait_until_signal_ready(child):
    # Give the interpreter time to install its handler.
    import time
    time.sleep(0.4)
    assert child.poll() is None


def test_runtime_stop_reports_progress_and_never_double_signals(client):  # noqa: F811
    child = _child(_SLOW_EXIT)
    _wait_until_signal_ready(child)
    dash.PID_FILE.write_text(str(child.pid))
    service = RuntimeService(CLI, dash.PROJECT_ROOT, dash.PID_FILE, dash.LOG_FILE)

    first = _run(service.stop(wait_seconds=0.2))
    assert first["stopping"] and not first["stopped"] and not first["forced"]
    status = client.get("/api/mercury/status").json()
    assert status["running"] and status["stopping"]
    assert status["stop_requested_at"] == first["stop_requested_at"]

    # Asking again reports progress; a second SIGTERM would cut it short.
    again = _run(service.stop(wait_seconds=5))
    assert again["stopped"] and not again["forced"]
    assert child.wait(timeout=5) == 1
    after = client.get("/api/mercury/status").json()
    assert not after["running"] and not after["stopping"] and not dash.PID_FILE.exists()


def test_runtime_force_stop_is_explicit(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runtime_mod, "STOP_WAIT_SECONDS", 0.2)
    child = _child("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)")
    _wait_until_signal_ready(child)
    dash.PID_FILE.write_text(str(child.pid))
    served = client.post("/api/mercury/stop").json()
    assert served["success"] and served["stopping"] and child.poll() is None
    forced = client.post("/api/mercury/stop?force=true").json()
    assert forced["stopped"] and forced["forced"]
    assert child.wait(timeout=5) is not None


# ── One status for every interface ──


def test_dashboard_cli_and_service_list_the_same_reasons(client, capsys, monkeypatch):  # noqa: F811
    from mercury import cli

    sm = client.sm
    _run(SendingService(CLI, sm).pause("checking copy"))
    _run(_seed_bounce_hold(sm))
    _run(warmup.set_paused(sm, "a@example.com", "paused manually"))
    pid = _run(seed_prospect(sm, email="p1@example.com"))
    item = _run(sm.add_outbox_item(prospect_id=pid, to_email="p1@example.com", subject="s",
                                   body="b", send_at="2026-01-01T00:00:00", status="approved"))
    _run(sm.update_outbox_item(item, status="sending"))

    service = _run(SendingService(CLI, sm, dash._demo_config()).status())
    served = client.get("/api/sending/status").json()
    assert served == service
    assert service["reason"] == "checking copy"
    kinds = [h["kind"] for h in service["holds"]]
    assert kinds == ["bounce_kill_switch", "compliance", "mailbox_paused"]
    assert service["holds"][2]["source"] == "operator"
    assert [i["id"] for i in service["in_flight"]] == [item]

    titles = [i["title"] for i in client.get("/api/today").json()["items"]]
    assert titles.count("Sending is on hold") == 2 and "Sending is paused" in titles

    monkeypatch.setattr("mercury.state.DB_PATH", sm.db_path)
    cli.cmd_sending(SimpleNamespace(sending_action="status"))
    out = capsys.readouterr().out
    assert "Paused by you: checking copy" in out
    for hold in service["holds"]:
        assert hold["reason"] in out
    assert "In flight: step 1 to p1@example.com" in out

    cli.cmd_sending(SimpleNamespace(sending_action="resume"))
    out = capsys.readouterr().out
    assert "still on hold" in out and "counters were kept" in out
    assert _run(sm.get_setting("bounce_count")) == "6"


@pytest.mark.asyncio
async def test_a_failed_send_in_flight_is_still_settled(state):
    """A claimed email whose provider call fails is settled (failed, or
    approved for a retry) like any other, never left in 'sending' by a pause."""
    await _two_due(state)

    class Failing(PausingProvider):
        async def send_email(self, *a, **k):
            if self.seen is None:
                await self.on_first()
                self.seen = True
            return SendResult(ok=False, error="550 5.1.1 no such user")

    provider = Failing(state, lambda: SendingService(CLI, state).pause())
    await make_sender(state, provider, require_approval=False)._run_native()
    assert await state.get_outbox(status="sending") == []
    assert len(await state.get_outbox(status="failed")) == 1
