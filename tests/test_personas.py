"""Persona revisions, prompt fidelity, email attribution and preview isolation."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

import mercury.config as config_module
import mercury.dashboard as dash
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.config import MercuryConfig, EnvConfig
from mercury.models.campaign import Campaign
from mercury.models.prospect import Prospect
from mercury.personas import AVATAR_SEEDS, JSON_INSTRUCTION, PersonaStore
from mercury.state import StateManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, monkeypatch):
    template = Path(__file__).resolve().parent.parent / "mercury.yaml"
    config = MercuryConfig(**yaml.safe_load(template.read_text()))
    config.channels.email.provider = "smtp"
    config.compliance.postal_address = "1 Main Street"
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(config_module, "_find_config_file", lambda: str(template))
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "mercury.db")
    state = StateManager(str(dash.DB_PATH))
    run(state.init_db())
    with TestClient(dash.app) as c:
        c.state, c.config = state, config
        yield c


def create(client, **changes):
    body = {"name": "Warm & Local", "description": "For local owners", "tone": "warm and direct",
            "instructions": "Use short sentences", "examples": "How do you handle quotes today?",
            "avatar_seed": AVATAR_SEEDS[2], **changes}
    response = client.post("/api/personas", json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def profile(client, persona_id):
    return next(p for p in client.get("/api/personas").json()["personas"] if p["id"] == persona_id)


def update_body(p, **changes):
    return {key: p[key] for key in ("name", "description", "tone", "instructions", "examples", "avatar_seed")} | {
        "expected_revision": p["revision"], **changes,
    }


def contact(client):
    return run(client.state.add_prospect(Prospect(first_name="Pat", title="Owner", company="Local Shop",
        email="pat@shop.example", email_status="verified", personalization_notes="Quotes arrive through WhatsApp")))


def fake_model(monkeypatch):
    model = AsyncMock(return_value={"subject": "your quote process", "body": "Quotes arrive through WhatsApp. How do you keep track of the ones still waiting?"})
    monkeypatch.setattr(Brain, "think_json", model)
    return model


def queue(client, draft, pid, status="pending_review", step=1, campaign_id="campaign"):
    return run(client.state.add_outbox_item(prospect_id=pid, to_email="pat@shop.example",
        subject=draft["subject"], body=draft["body"], generation_id=draft.get("generation_id", ""),
        send_at="2026-10-08T12:00:00", campaign_id=campaign_id, step=step, status=status))


def test_existing_config_imported_without_credentials(client):
    data = client.get("/api/personas").json()
    assert data["default_id"] == "workspace"
    assert data["personas"][0]["tone"] == client.config.persona.tone
    assert len(data["avatars"]) == 24
    assert "sender" in data["current"] and "product" in data["current"]
    assert "SMTP_PASSWORD" not in json.dumps(data)
    for avatar in data["avatars"]:
        response = client.get(avatar["url"])
        assert response.status_code == 200 and "image/svg+xml" in response.headers["content-type"]


def test_only_voice_edits_create_revisions_and_default_is_guarded(client):
    pid = create(client)
    original = profile(client, pid)
    response = client.post(f"/api/personas/{pid}/save", json=update_body(original, name="Friendly", avatar_seed=AVATAR_SEEDS[3]))
    assert response.status_code == 200
    visual = profile(client, pid)
    assert visual["revision"] == 1 and visual["version_id"] == original["version_id"]
    assert client.post(f"/api/personas/{pid}/save", json=update_body(visual, tone="more direct")).status_code == 200
    assert profile(client, pid)["revision"] == 2
    assert client.post(f"/api/personas/{pid}/save", json=update_body(visual)).status_code == 409
    assert client.post(f"/api/personas/{pid}/default").status_code == 200
    assert client.post(f"/api/personas/{pid}/archive", json={"archived": True}).status_code == 409
    assert client.post("/api/personas/workspace/default").status_code == 200
    assert client.post(f"/api/personas/{pid}/archive", json={"archived": True}).status_code == 200
    assert client.post(f"/api/personas/{pid}/default").status_code == 409
    assert client.post(f"/api/personas/{pid}/archive", json={"archived": False}).status_code == 200
    assert len(client.get(f"/api/personas/{pid}/versions").json()["versions"]) == 2


def test_avatar_and_input_validation(client):
    assert client.post("/api/personas", json={"name":"", "tone":"warm"}).status_code == 422
    assert client.post("/api/personas", json={"name":"x", "tone":"warm", "avatar_seed":"../../outside"}).status_code == 422
    assert client.post("/api/personas/prompt", json={"prospect_id":"missing"}).status_code == 404


def test_prompt_inspection_and_preview_never_queue_email(client, monkeypatch):
    model = fake_model(monkeypatch)
    pid = contact(client)
    persona = profile(client, create(client))
    args = {"prospect_id": pid, "version_id": persona["version_id"], "instruction":"Make the question concrete"}
    inspection = client.post("/api/personas/prompt", json=args)
    assert inspection.status_code == 200
    prompt = inspection.json()["prompt"]
    assert "warm and direct" in prompt and "Quotes arrive through WhatsApp" in prompt
    assert "Make the question concrete" in prompt and "FOUNDATIONAL KNOWLEDGE" in prompt
    assert prompt.endswith(JSON_INSTRUCTION)
    model.assert_not_awaited()
    preview = client.post("/api/personas/preview", json=args)
    assert preview.status_code == 200
    assert preview.json()["prompt"] == model.await_args.args[0] + JSON_INSTRUCTION
    assert run(client.state.get_outbox()) == []
    assert run(client.state.get_campaigns_by_status("draft")) == []
    model.assert_awaited_once()


def test_regeneration_keeps_original_version_and_every_prompt(client, monkeypatch):
    fake_model(monkeypatch)
    pid = contact(client)
    persona_id = create(client)
    original = profile(client, persona_id)
    draft = client.post("/api/personas/preview", json={"prospect_id":pid, "version_id":original["version_id"]}).json()
    item_id = queue(client, draft, pid, status="approved")
    assert client.put(f"/api/outbox/{item_id}", json={"subject":"edited subject", "body":"Edited by a person"}).status_code == 200
    assert run(client.state.get_outbox_item(item_id))["manually_edited"] == 1
    client.post(f"/api/personas/{persona_id}/save", json=update_body(original, tone="formal", avatar_seed=AVATAR_SEEDS[7]))
    client.post("/api/personas/workspace/default")
    regenerated = client.post(f"/api/outbox/{item_id}/regenerate", json={"instruction":"Shorter please"})
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()["writing_persona"]["version_id"] == original["version_id"]
    assert regenerated.json()["status"] == "pending_review" and not regenerated.json()["manually_edited"]
    history = client.get(f"/api/outbox/{item_id}/generation-history").json()["generations"]
    assert len(history) == 2
    assert history[1]["original_subject"] == draft["subject"]
    assert history[0]["persona"]["tone"] == history[1]["persona"]["tone"] == "warm and direct"
    assert history[0]["persona"]["avatar_seed"] == AVATAR_SEEDS[2]
    assert "Shorter please" in history[0]["prompt"]
    assert "Shorter please" not in history[1]["prompt"]
    run(client.state.update_outbox_item(item_id, status="sent", sent_at="2026-10-08T12:00:00"))
    assert client.post(f"/api/outbox/{item_id}/regenerate", json={}).status_code == 409
    sent = client.get("/api/outbox").json()["sent"][0]
    assert sent["writing_persona"]["version_id"] == original["version_id"]


def test_shared_sequence_and_personal_opener_use_same_pinned_persona(client, monkeypatch):
    from mercury.agents.sender import Sender
    from tests.test_outbox_native import FakeProvider, Env
    pid = contact(client)
    persona_id = create(client)
    client.post(f"/api/personas/{persona_id}/default")
    original = profile(client, persona_id)
    model = AsyncMock(return_value=[{"step":i, "subject":"your quotes", "body":"Hello {{first_name}}. How do you track quotes?", "delay_days":i-1} for i in (1,2,3)])
    monkeypatch.setattr(Brain, "think_json", model)
    writer = Writer(Brain(client.state), client.state, client.config)
    prospect = run(client.state.get_prospect(pid))
    steps = run(writer._write_sequence([prospect]))
    assert len(steps) == 3 and len({s.generation_id for s in steps}) == 1
    cid = run(client.state.add_campaign(Campaign(id="", sequence=steps, prospect_ids=[pid])))
    client.post("/api/personas/workspace/default")
    model.return_value = {"subject":"your quotes", "body":"Quotes arrive on WhatsApp. How do you track them?"}
    store = PersonaStore(client.state)
    pinned = run(store.for_generation(client.config, steps[0].generation_id))
    run(writer._personalize_first_emails(cid, [prospect], pinned))
    campaign = run(client.state.get_campaigns_by_status("draft"))[0]
    sender = Sender(None, client.state, client.config, Env())
    sender.provider = FakeProvider()
    run(sender._stage_campaign_native(campaign))
    items = run(store.enrich(run(client.state.get_outbox())))
    assert len(items) == 3
    assert {item["writing_persona"]["version_id"] for item in items} == {original["version_id"]}
    assert items[0]["generation_id"] != items[1]["generation_id"]
    for item in items:
        assert len(run(store.history(item["id"]))) == 1


def test_reply_inherits_thread_voice_after_default_changes(client, monkeypatch):
    from mercury.agents.handler import Handler
    from mercury.models.conversation import Conversation
    fake_model(monkeypatch)
    pid = contact(client)
    original = profile(client, create(client))
    draft = client.post("/api/personas/preview", json={"prospect_id":pid, "version_id":original["version_id"]}).json()
    item_id = queue(client, draft, pid, status="sent")
    run(client.state.update_outbox_item(item_id, sent_at="2026-10-08T12:00:00"))
    response_model = AsyncMock(return_value="We can start with your quote process. Would Tuesday work?")
    monkeypatch.setattr(Brain, "think", response_model)
    handler = Handler(Brain(client.state), client.state, client.config, EnvConfig())
    prospect = run(client.state.get_prospect(pid))
    convo = Conversation(id="thread", prospect_id=pid, campaign_id="campaign", intent="interested")
    run(client.state.add_conversation(convo))
    response = run(handler._generate_response("interested", "Tell me more", prospect, convo))
    run(handler._queue_native_reply(response, prospect, convo, {"subject":"your quotes"}, "interested"))
    reply = next(row for row in run(client.state.get_outbox()) if row["kind"] == "reply")
    history = run(PersonaStore(client.state).history(reply["id"]))
    assert history[0]["persona"]["version_id"] == original["version_id"]
    assert history[0]["prompt"] == response_model.await_args.args[0]
    regeneration = client.post(f"/api/outbox/{reply['id']}/regenerate", json={"instruction":"Make the next step specific"})
    assert regeneration.status_code == 200, regeneration.text
    assert regeneration.json()["subject"] == "Re: your quotes"
    assert regeneration.json()["kind"] == "reply"
    updated = client.get(f"/api/outbox/{reply['id']}/generation-history").json()["generations"]
    assert len(updated) == 2
    assert updated[0]["persona"]["version_id"] == original["version_id"]
    assert "Make the next step specific" in updated[0]["prompt"]
    assert updated[0]["task"] == "generate_response"


def test_unknown_legacy_email_and_late_regeneration_are_safe(client):
    pid = contact(client)
    item_id = queue(client, {"subject":"old", "body":"old draft"}, pid)
    assert client.get(f"/api/outbox/{item_id}/generation-history").json()["generations"] == []
    assert client.get("/api/outbox").json()["pending"][0]["writing_persona"] is None
    run(client.state.update_outbox_item(item_id, status="sent"))
    with pytest.raises(ValueError, match="no longer"):
        run(PersonaStore(client.state).replace_draft(item_id, {"subject":"new", "body":"new draft"}))
    assert run(client.state.get_outbox_item(item_id))["subject"] == "old"
