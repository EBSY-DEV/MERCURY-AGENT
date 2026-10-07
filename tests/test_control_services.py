"""Shared command services (mercury/control): the dashboard and a direct
service call must give the same answer, and the services must refuse what
they are not allowed to do before anything changes.

Every test runs against a throwaway SQLite file and a copy of the template
config. Nothing here sends mail or calls a discovery provider.
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

import mercury.config as config_module
import mercury.dashboard as dash
from mercury.control import discovery as discovery_mod
from mercury.control import runtime as runtime_mod
from mercury.control.context import OperatorContext
from mercury.control.discovery import DiscoveryService
from mercury.control.errors import (
    Conflict, ControlError, Forbidden, Invalid, NotFound, ProhibitedField,
)
from mercury.control.outbox import OutboxService
from mercury.control.queries import QueryService
from mercury.control.runtime import RuntimeService
from mercury.control.sending import SendingService
from mercury.control.settings import EDITABLE, ConfigService
from mercury.models.campaign import Campaign, EmailStep
from mercury.models.company import Company
from mercury.models.conversation import Conversation
from mercury.models.prospect import Prospect
from mercury.pipeline import PipelineReport
from mercury.signals import seed_signal_catalog
from mercury.state import StateManager

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"
DASH = OperatorContext.local("dashboard")
CLI = OperatorContext.local("cli")


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _plain(value):
    """What a value looks like after a trip through JSON, as the dashboard returns it."""
    return json.loads(json.dumps(value, default=str))


def _without(value, key):
    """``value`` with ``key`` removed at every depth."""
    if isinstance(value, dict):
        return {k: _without(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_without(v, key) for v in value]
    return value


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A dashboard bound to a throwaway database and a copy of the template config."""
    db = tmp_path / "mercury.db"
    config_path = tmp_path / "mercury.yaml"
    config_path.write_text(TEMPLATE.read_text())
    local = tmp_path / "mercury.local.yaml"
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.setattr(config_module, "_find_config_file",
                        lambda: str(local if local.exists() else config_path))
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "mercury.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "mercury.log")
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    # No native mailboxes: the outbox shows what each row stores.
    monkeypatch.setattr(dash, "_mail_context", lambda: (_ for _ in ()).throw(RuntimeError("no mail")))
    monkeypatch.setattr(discovery_mod, "_task", None)
    monkeypatch.setattr(discovery_mod, "_report", None)
    monkeypatch.setattr(runtime_mod, "_process", None)
    monkeypatch.setattr(runtime_mod, "_started_at", None)
    sm = StateManager(db_path=str(db))
    _run(sm.init_db())
    with TestClient(dash.app) as c:
        c.sm, c.tmp, c.config_path, c.local_path = sm, tmp_path, config_path, local
        yield c


def _prospect(sm, email, company_id="", status="new", score=50):
    return _run(sm.add_prospect(Prospect(
        first_name="Pat", last_name="Lee", title="Owner", email=email,
        email_status="verified", status=status, score=score, company_id=company_id,
    )))


def _queue(sm, pid, step=1, status="pending_review", campaign_id="c1", kind="sequence"):
    return _run(sm.add_outbox_item(
        prospect_id=pid, to_email="pat@example.com", subject=f"subject {step}",
        body="body", send_at=(_now() + timedelta(days=step)).isoformat(),
        status=status, campaign_id=campaign_id, step=step, kind=kind,
    ))


def _seed(sm):
    company_id = _run(sm.add_company(Company(name="Example Co", domain="example.com",
                                             industry="segment_a", location="Denver, CO")))
    pid = _prospect(sm, "pat@example.com", company_id=company_id)
    _prospect(sm, "sam@example.org", score=80)
    _run(sm.add_campaign(Campaign(id="", name="offer_a", prospect_ids=[pid],
                                  sequence=[EmailStep(step=1, subject="s", body="b")])))
    _run(sm.add_conversation(Conversation(id="", prospect_id=pid, stage="engaged", status="open")))
    _run(sm.log_action("seeded", "test", {"n": 1}))
    _run(seed_signal_catalog(sm))
    _run(sm.add_observation("NO_WEBSITE", company_id=company_id, collector="test"))
    _run(sm.finish_run(_run(sm.start_run("discover", "osm")), records=1))
    return company_id, pid


# ── Operator context and errors ──


def test_context_rejects_unknown_clients_and_scopes():
    with pytest.raises(ValueError):
        OperatorContext.local("browser")
    with pytest.raises(ValueError):
        OperatorContext(client="mcp", scopes=frozenset({"root"}))
    assert DASH.actor == "dashboard" and "approve" in DASH.scopes


def test_existing_control_errors_share_the_base():
    from mercury.control.demos import DemoError
    from mercury.csv_import import ImportFileError
    from mercury.personas import PersonaError

    for error in (PersonaError("not_found", "x"), DemoError("retired", "x"),
                  ImportFileError("empty", "x", rows=1)):
        assert isinstance(error, ControlError) and isinstance(error, ValueError)
    assert ImportFileError("empty", "x", rows=1).details == {"rows": 1}
    assert PersonaError("not_found", "gone").code == "not_found"


def test_missing_scope_is_refused_before_anything_changes(client):
    pid = _prospect(client.sm, "pat@example.com")
    item = _queue(client.sm, pid)
    reader = OperatorContext(client="mcp", scopes=frozenset({"read"}))
    with pytest.raises(Forbidden) as error:
        _run(OutboxService(reader, client.sm).approve(item))
    assert error.value.code == "missing_scope"
    with pytest.raises(Forbidden):
        _run(SendingService(reader, client.sm).pause())
    assert _run(client.sm.get_outbox_item(item))["status"] == "pending_review"
    assert _run(client.sm.get_setting("sending_paused")) == ""
    # Reading is allowed.
    assert _run(OutboxService(reader, client.sm).get(item))["id"] == item


# ── Queries ──


@pytest.mark.parametrize("path, call", [
    ("/api/stats", lambda q, ids: q.stats()),
    ("/api/companies", lambda q, ids: q.companies()),
    ("/api/companies/{company}/contacts", lambda q, ids: q.company_contacts(ids[0])),
    ("/api/prospects", lambda q, ids: q.prospects()),
    ("/api/campaigns", lambda q, ids: q.campaigns()),
    ("/api/conversations", lambda q, ids: q.conversations()),
    ("/api/activity", lambda q, ids: q.activity()),
    ("/api/runs", lambda q, ids: q.runs()),
    ("/api/signals", lambda q, ids: q.signals()),
])
def test_dashboard_reads_match_the_query_service(client, path, call):
    ids = _seed(client.sm)
    served = client.get(path.format(company=ids[0])).json()
    direct = _run(call(QueryService(CLI, client.sm), ids))
    # Every load re-seeds the signal catalog, which stamps updated_at anew.
    assert _without(served, "updated_at") == _without(_plain(direct), "updated_at")
    assert served  # the seed is visible, so equality is not two empty answers


def test_cohort_matches_and_reads_tolerate_a_missing_database(client, tmp_path):
    company_id, _pid = _seed(client.sm)
    _run(client.sm.set_signal_status("NO_WEBSITE", "confirmed"))
    served = client.post("/api/cohort", json={"require": ["NO_WEBSITE"]}).json()
    assert served == _plain(_run(QueryService(CLI, client.sm).cohort(["NO_WEBSITE"])))
    assert served["size"] == 1 and served["companies"][0]["id"] == company_id

    missing = StateManager(db_path=str(tmp_path / "nothing-here.db"))
    assert _run(QueryService(CLI, missing).companies()) == []
    assert not (tmp_path / "nothing-here.db").exists()


# ── Outbox ──


def test_outbox_overview_matches(client, monkeypatch):
    monkeypatch.setattr(dash, "_demo_config", lambda: None)
    pid = _prospect(client.sm, "pat@example.com")
    _queue(client.sm, pid, step=1)
    _queue(client.sm, pid, step=2, status="approved")
    served = client.get("/api/outbox").json()
    direct = _run(OutboxService(CLI, client.sm).overview())
    assert served == _plain(direct)
    assert len(served["pending"]) == 1 and len(served["approved"]) == 1


def test_approve_through_either_interface(client):
    pid = _prospect(client.sm, "pat@example.com")
    a = _queue(client.sm, pid, campaign_id="c1")
    b = _queue(client.sm, pid, campaign_id="c2")

    assert client.post(f"/api/outbox/{a}/approve").json() == {"success": True, "followups_approved": 0}
    assert _run(OutboxService(CLI, client.sm).approve(b)) == {"id": b, "approved": 1, "followups_approved": 0}
    for item in (a, b):
        assert _run(client.sm.get_outbox_item(item))["status"] == "approved"

    # Approving again: the dashboard keeps its old answer, the service says why.
    assert client.post(f"/api/outbox/{a}/approve").json() == {"success": False, "followups_approved": 0}
    with pytest.raises(Conflict) as error:
        _run(OutboxService(CLI, client.sm).approve(b))
    assert error.value.code == "not_pending"
    with pytest.raises(NotFound):
        _run(OutboxService(CLI, client.sm).approve("nope"))


def test_approving_promotes_follow_ups_through_the_service(client):
    pid = _prospect(client.sm, "pat@example.com")
    opener = _queue(client.sm, pid, step=1)
    follow = _queue(client.sm, pid, step=2)
    config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(auto_approve_followups=True)))
    result = _run(OutboxService(CLI, client.sm, config).approve(opener))
    assert result["followups_approved"] == 1
    assert _run(client.sm.get_outbox_item(follow))["status"] == "approved"


def test_reject_cascades_the_same_way(client):
    pid = _prospect(client.sm, "pat@example.com")
    one = [_queue(client.sm, pid, step=s, campaign_id="c1") for s in (1, 2, 3)]
    two = [_queue(client.sm, pid, step=s, campaign_id="c2") for s in (1, 2, 3)]

    assert client.post(f"/api/outbox/{one[0]}/reject").json() == {"success": True, "rejected": 3}
    assert _run(OutboxService(CLI, client.sm).reject(two[0]))["rejected"] == 3
    assert client.post("/api/outbox/nope/reject").json() == {"success": True, "rejected": 0}
    with pytest.raises(Conflict) as error:
        _run(OutboxService(CLI, client.sm).reject(two[0]))
    assert error.value.code == "not_queued"


def test_edit_and_its_refusals_match(client):
    pid = _prospect(client.sm, "pat@example.com")
    a, b = _queue(client.sm, pid, campaign_id="c1"), _queue(client.sm, pid, campaign_id="c2")

    assert client.put(f"/api/outbox/{a}", json={"subject": "New", "body": "Text"}).json() == {"success": True}
    _run(OutboxService(CLI, client.sm).edit(b, "New", "Text"))
    for item in (a, b):
        row = _run(client.sm.get_outbox_item(item))
        assert (row["subject"], row["body"], row["manually_edited"]) == ("New", "Text", 1)

    r = client.put(f"/api/outbox/{a}", json={"subject": "", "body": "Text"})
    assert r.status_code == 400
    with pytest.raises(Invalid):
        _run(OutboxService(CLI, client.sm).edit(b, "", "Text"))

    _run(client.sm.update_outbox_item(a, status="sent"))
    r = client.put(f"/api/outbox/{a}", json={"subject": "S", "body": "B"})
    assert r.status_code == 409
    with pytest.raises(Conflict) as error:
        _run(OutboxService(CLI, client.sm).edit(a, "S", "B"))
    assert error.value.code == "not_editable"
    # A missing draft keeps the dashboard's old 409; the service names it.
    assert client.put("/api/outbox/nope", json={"subject": "S", "body": "B"}).status_code == 409
    with pytest.raises(NotFound):
        _run(OutboxService(CLI, client.sm).edit("nope", "S", "B"))


def test_reschedule_matches_and_is_logged_as_the_caller(client):
    pid = _prospect(client.sm, "pat@example.com")
    a, b = _queue(client.sm, pid, campaign_id="c1"), _queue(client.sm, pid, campaign_id="c2")
    when = (_now() + timedelta(days=4)).replace(microsecond=0)

    served = client.post(f"/api/outbox/{a}/reschedule", json={"send_at": when.isoformat()}).json()
    direct = _run(OutboxService(CLI, client.sm).reschedule(b, when))
    assert served == {"success": True, "send_at": direct["send_at"]}
    agents = {r["agent"] for r in _run(QueryService(CLI, client.sm).activity())
              if r["action_type"] == "outbox_reschedule"}
    assert agents == {"dashboard", "cli"}

    past = _now() - timedelta(hours=2)
    assert client.post(f"/api/outbox/{a}/reschedule", json={"send_at": past.isoformat()}).status_code == 400
    with pytest.raises(Invalid):
        _run(OutboxService(CLI, client.sm).reschedule(b, past))
    assert client.post("/api/outbox/nope/reschedule", json={"send_at": when.isoformat()}).status_code == 404


def test_batch_applies_to_explicit_ids_only(client):
    pid = _prospect(client.sm, "pat@example.com")
    a, b, c = (_queue(client.sm, pid, campaign_id=f"c{i}") for i in range(3))

    served = client.post("/api/outbox/batch", json={"action": "approve", "ids": [a, "nope", a]}).json()
    assert served["success"] and served["succeeded"] == 1 and served["failed"] == 1
    assert [r["ok"] for r in served["results"]] == [True, False]
    assert served["results"][1]["code"] == "not_found"
    direct = _run(OutboxService(CLI, client.sm).batch("reject", [b]))
    assert direct["succeeded"] == 1 and direct["results"][0]["rejected"] == 1
    # c was never named, so nothing touched it.
    assert _run(client.sm.get_outbox_item(c))["status"] == "pending_review"

    assert client.post("/api/outbox/batch", json={"action": "send", "ids": [c]}).status_code == 400
    assert client.post("/api/outbox/batch", json={"action": "approve", "ids": []}).status_code == 400
    assert _run(client.sm.get_outbox_item(c))["status"] == "pending_review"


def test_get_one_item(client):
    pid = _prospect(client.sm, "pat@example.com")
    item = _queue(client.sm, pid)
    served = client.get(f"/api/outbox/{item}").json()
    assert served["id"] == item and served["from_mailbox"] == ""
    assert client.get("/api/outbox/nope").status_code == 404


# ── Sending switch ──


def test_pause_and_resume_through_either_interface(client):
    sm = client.sm
    service = SendingService(CLI, sm, dash._demo_config())
    paused = client.post("/api/sending/pause").json()
    status = _run(service.status())
    assert paused == {"success": True, **_plain(status)}
    assert status["paused"] and status["blocked"] and status["reason"] == "paused from dashboard"
    assert client.get("/api/sending/status").json() == _plain(status)
    assert client.get("/api/outbox").json()["paused"] == status["reason"]

    _run(sm.set_setting("bounce_count", "7"))
    resumed = client.post("/api/sending/resume").json()
    assert resumed["success"] and resumed["paused"] is False
    # The template has no postal address: that hold outlives the resume.
    assert [h["kind"] for h in resumed["holds"]] == ["compliance"] and resumed["blocked"]
    assert _run(sm.get_setting("bounce_count")) == "7"  # resume never touches the count

    assert _run(service.pause())["reason"] == "paused manually"
    assert _run(service.resume())["paused"] is False
    assert client.post("/api/sending/explode").status_code == 400


# ── Discovery ──


def _fake_prospecting(calls):
    async def fake(state, config, provider, queries, max_spend=1.0, profile=True, **_):
        calls.append({"provider": provider, "queries": len(queries), "max_spend": max_spend,
                      "profile": profile})
        return PipelineReport(discover={"found": 3, "queries": len(queries)},
                              profiled_companies=2, profile_observations=5)
    return fake


def test_estimate_matches(client):
    body = {"provider": "osm", "cities": ["Denver, CO", "Austin, TX"], "depth": 20, "limit": 50}
    served = client.post("/api/discover/estimate", json=body).json()
    direct = _run(DiscoveryService(CLI, client.sm).estimate(
        "osm", ["Denver, CO", "Austin, TX"], depth=20, limit=50))
    assert served == _plain(direct)
    assert served["query_count"] == 4 and served["free"] is True

    r = client.post("/api/discover/estimate", json={"provider": "nope"})
    assert r.status_code == 400 and "unknown provider" in r.json()["error"]
    with pytest.raises(Invalid) as error:
        DiscoveryService(CLI, client.sm).plan("nope")
    assert error.value.code == "unknown_provider"


def test_submitted_run_reports_back_and_a_second_is_refused(client, monkeypatch):
    calls = []
    monkeypatch.setattr("mercury.pipeline.run_prospecting", _fake_prospecting(calls))
    _run(client.sm.set_setting("discovery_paused", "stopped earlier"))

    r = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Denver, CO"], "max_spend": 0.5})
    assert r.json() == {"success": True, "queries": 2}
    for _ in range(100):
        menu = client.get("/api/discover/providers").json()
        if not menu["running"]:
            break
        time.sleep(0.02)
    assert menu["last_report"] == {"found": 3, "queries": 2, "profiled_companies": 2,
                                   "profile_observations": 5, "errors": []}
    assert menu["selected"] == "osm" and menu["paused"] == ""
    assert calls == [{"provider": "osm", "queries": 2, "max_spend": 0.5, "profile": True}]

    monkeypatch.setattr(discovery_mod, "running", lambda: True)
    assert client.post("/api/discover/run", json={"provider": "osm"}).status_code == 409
    with pytest.raises(Conflict) as error:
        _run(DiscoveryService(CLI, client.sm).submit("osm"))
    assert error.value.code == "job_running"


def test_direct_run_and_stop(client, monkeypatch):
    calls = []
    monkeypatch.setattr("mercury.pipeline.run_prospecting", _fake_prospecting(calls))
    service = DiscoveryService(CLI, client.sm)
    report = _run(service.run(service.plan("osm", ["Denver, CO"]), max_spend=2.0, profile=False))
    assert report.discover["found"] == 3 and calls[0]["profile"] is False

    assert client.post("/api/discover/stop").json() == {"success": True}
    assert _run(client.sm.get_setting("discovery_paused")) == "stopped from dashboard"
    _run(service.stop())
    assert _run(client.sm.get_setting("discovery_paused")) == "stopped from cli"


# ── Runtime ──


class FakePopen:
    def __init__(self, *args, **kwargs):
        self.pid = 424242

    def poll(self):
        return None


def test_runtime_status_start_and_refusals_match(client, monkeypatch):
    service = RuntimeService(CLI, dash.PROJECT_ROOT, dash.PID_FILE, dash.LOG_FILE)
    assert client.get("/api/mercury/status").json() == _plain(_run(service.status()))
    assert client.post("/api/mercury/stop").json() == {"success": False, "message": "Mercury is not running."}
    with pytest.raises(Conflict) as error:
        _run(service.stop())
    assert error.value.code == "not_running"

    monkeypatch.setattr(runtime_mod.subprocess, "Popen", FakePopen)
    assert client.post("/api/mercury/start").json() == {"success": True, "pid": 424242}
    assert dash.PID_FILE.read_text() == "424242"
    served = client.get("/api/mercury/status").json()
    assert served == _plain(_run(service.status())) and served["running"] is True
    assert client.post("/api/mercury/start").json() == {"success": False, "message": "Mercury is already running."}
    with pytest.raises(Conflict):
        _run(service.start())


def test_runtime_stop_ends_a_real_process(client):
    child = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=child.wait, daemon=True).start()  # reap it, or it stays a zombie
    dash.PID_FILE.write_text(str(child.pid))
    stopped = client.post("/api/mercury/stop").json()
    assert stopped["success"] and stopped["stopped"] and not stopped["forced"]
    assert child.wait(timeout=5) is not None and not dash.PID_FILE.exists()


def test_logs_match(client):
    dash.LOG_FILE.write_text("".join(f"line {i}\n" for i in range(150)))
    served = client.get("/api/mercury/logs").json()
    direct = _run(RuntimeService(CLI, dash.PROJECT_ROOT, dash.PID_FILE, dash.LOG_FILE).logs())
    assert served == direct and len(served["lines"]) == 100 and served["lines"][-1] == "line 149"


# ── Supported configuration ──


def _files(client):
    return (client.config_path.read_bytes(),
            client.local_path.read_bytes() if client.local_path.exists() else None)


def test_config_read_matches(client):
    served = client.get("/api/config").json()
    direct = _run(ConfigService(CLI).get())
    assert served == _plain(direct)
    assert set(served["fields"]) == set(EDITABLE)
    assert served["fields"]["channels.email.require_approval"] is True


def test_config_change_goes_to_the_private_file(client):
    before = client.config_path.read_bytes()
    r = client.patch("/api/config", json={"channels.email.require_approval": False,
                                          "usage.quiet_hours.start": "21:30"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] and data["changed"] == {"channels.email.require_approval": False,
                                                   "usage.quiet_hours.start": "21:30"}
    assert data["restart_required"] is False
    assert client.config_path.read_bytes() == before        # the template is untouched
    saved = config_module.load_config(str(client.local_path))
    assert saved.channels.email.require_approval is False and saved.usage.quiet_hours.start == "21:30"
    # The service now reads the change back.
    assert _run(ConfigService(CLI).get())["fields"]["usage.quiet_hours.start"] == "21:30"


@pytest.mark.parametrize("changes, status, code", [
    ({"SMTP_PASSWORD": "hunter2"}, 403, "secret_field"),
    ({"channels.email.mailboxes.0.password_env": "X"}, 403, "secret_field"),
    ({"channels.email.require_approval": False, "treg_token": "t"}, 403, "secret_field"),
    ({"persona.email": "someone@example.com"}, 403, "unknown_field"),
    ({"channels.email.require_approval": False, "channels.email.provider": "smtp"}, 403, "unknown_field"),
    ({"channels.email.max_daily_sends": "50"}, 400, "invalid_value"),
    ({"channels.email.require_approval": 1}, 400, "invalid_value"),
    ({"usage.quiet_hours.timezone": "Mars/Base_One"}, 400, "invalid_value"),
    ({"usage.max_daily_claude_percent": 250}, 400, "invalid_value"),
    ({}, 400, "invalid"),
])
def test_refused_config_changes_write_nothing(client, changes, status, code):
    before = _files(client)
    r = client.patch("/api/config", json=changes)
    assert r.status_code == status and r.json()["success"] is False and r.json()["code"] == code
    assert "hunter2" not in r.text
    with pytest.raises(ControlError) as error:
        _run(ConfigService(CLI).update(changes))
    assert error.value.code == code
    assert _files(client) == before


def test_prohibited_fields_are_named(client):
    with pytest.raises(ProhibitedField) as error:
        _run(ConfigService(CLI).update({"usage.heartbeat_interval_minutes": 5, "SERPER_API_KEY": "k",
                                        "persona.name": "x"}))
    # Any secret in the request makes it a secret refusal, naming only the secrets.
    assert error.value.code == "secret_field" and error.value.fields == ["SERPER_API_KEY"]


def test_config_edit_needs_the_edit_scope(client):
    before = _files(client)
    reader = OperatorContext(client="mcp", scopes=frozenset({"read"}))
    with pytest.raises(Forbidden):
        _run(ConfigService(reader).update({"channels.email.require_approval": False}))
    assert _files(client) == before


def test_explicit_config_is_written_in_place(tmp_path, monkeypatch):
    path = tmp_path / "state-config.yaml"
    path.write_text("# keep me\n" + TEMPLATE.read_text())
    monkeypatch.setenv("MERCURY_CONFIG", str(path))
    _run(ConfigService(CLI).update({"channels.email.max_daily_sends": 12}))
    assert path.read_text().startswith("# keep me\n")
    assert yaml.safe_load(path.read_text())["channels"]["email"]["max_daily_sends"] == 12
    assert not (tmp_path / "mercury.local.yaml").exists()


# ── CLI ──


def test_cli_commands_go_through_the_services(client, monkeypatch, capsys):
    from mercury import cli

    monkeypatch.setattr("mercury.state.DB_PATH", Path(client.sm.db_path))
    pid = _prospect(client.sm, "pat@example.com")
    item = _queue(client.sm, pid)

    cli.cmd_sending(SimpleNamespace(sending_action="pause"))
    assert _run(SendingService(CLI, client.sm).status())["reason"] == "paused manually"
    cli.cmd_sending(SimpleNamespace(sending_action="resume"))
    assert _run(client.sm.get_setting("sending_paused")) == ""

    args = SimpleNamespace(approve=item, approve_all=False, reject="")
    cli.cmd_outbox(args)
    assert "Approved." in capsys.readouterr().out
    cli.cmd_outbox(args)
    assert "No pending item with that id." in capsys.readouterr().out
    cli.cmd_outbox(SimpleNamespace(approve="", approve_all=False, reject="nope"))
    assert "No queued item with that id." in capsys.readouterr().out

    cli.cmd_discover(SimpleNamespace(providers=False, provider="osm", city="Denver, CO;Austin, TX",
                                     depth=30, limit=100, max_spend=1.0, estimate=True, no_profile=False))
    out = capsys.readouterr().out
    assert "4 queries via" in out and "Estimated cost: $0.0000" in out
    cli.cmd_discover(SimpleNamespace(providers=False, provider="nope", city="", depth=30, limit=100,
                                     max_spend=1.0, estimate=True, no_profile=False))
    assert "Unknown provider 'nope'" in capsys.readouterr().out
