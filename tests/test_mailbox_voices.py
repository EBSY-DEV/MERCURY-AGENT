"""A voice and a sign-off name per sending mailbox."""

from unittest.mock import AsyncMock

import pytest

from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.config import MailboxConfig
from mercury.models.prospect import Prospect
from mercury.personas import AVATAR_SEEDS, PersonaError, voice_instructions
from mercury.voices import MailboxVoices
from tests.test_personas import client, contact, create, profile, run  # noqa: F401  (fixture)


def mailboxes(client, *boxes):
    client.config.channels.email.mailboxes = [MailboxConfig(**box) for box in boxes]
    return MailboxVoices(client.state, client.config)


TWO = ({"email": "harvey@one.example", "name": "Harvey Ruiz", "daily_cap": 20},
       {"email": "hello@two.example", "name": "EBSY", "daily_cap": 10})


def test_sign_off_comes_from_mailbox_then_persona_then_config(client):
    voices = mailboxes(client, *TWO)
    harvey, hello = run(voices.assignments())
    assert harvey["signer"] == hello["signer"] == client.config.persona.name and not harvey["sign_name"]
    assert harvey["follows_default"] and harvey["persona"]["id"] == "workspace"
    pid = create(client, sign_name="Alex")
    run(voices.assign("hello@two.example", pid))
    run(voices.assign("harvey@one.example", "", "Harvey"))
    harvey, hello = run(voices.assignments())
    assert (harvey["signer"], harvey["suggested"]) == ("Harvey", client.config.persona.name)
    assert (hello["signer"], hello["persona"]["id"], hello["follows_default"]) == ("Alex", pid, False)


def test_assign_rejects_unknown_mailboxes_and_archived_personas(client):
    voices = mailboxes(client, *TWO)
    with pytest.raises(PersonaError) as unknown:
        run(voices.assign("nobody@x.example"))
    assert unknown.value.code == "not_found"
    pid = create(client)
    client.post(f"/api/personas/{pid}/archive", json={"archived": True})
    with pytest.raises(PersonaError):
        run(voices.assign("harvey@one.example", pid))
    with pytest.raises(PersonaError):
        run(voices.assign("harvey@one.example", "", "Harvey\nBcc: x"))


def test_archived_assignment_falls_back_to_default(client):
    voices = mailboxes(client, *TWO)
    pid = create(client)
    run(voices.assign("harvey@one.example", pid))
    client.post(f"/api/personas/{pid}/archive", json={"archived": True})
    harvey = run(voices.assignments())[0]
    assert harvey["persona"]["id"] == "workspace"
    assert harvey["persona_id"] == "" and harvey["follows_default"]
    # Saving a sign-off from the UI re-posts the reported persona id, so the
    # stale archived one must not come back and be rejected.
    r = client.post("/api/voices/harvey@one.example", json={"persona_id": harvey["persona_id"], "sign_name": "Harv"})
    assert r.status_code == 200 and r.json()["signer"] == "Harv"


def test_rotation_stays_at_send_time_until_mailboxes_differ(client):
    voices = mailboxes(client, *TWO)
    run(voices.assign("harvey@one.example", "", "Harvey"))
    run(voices.assign("hello@two.example", "", "Harvey"))
    plan = run(voices.plan())
    assert not plan["pinned"] and plan["profile"]["signer"] == "Harvey" and plan["profile"]["mailbox"] == ""
    run(voices.assign("hello@two.example", "", "Team EBSY"))
    assert run(voices.plan())["pinned"]
    client.config.channels.email.mailboxes[1].enabled = False
    assert not run(voices.plan())["pinned"], "a mailbox that opens no new threads does not count"


def test_spread_fills_the_mailbox_with_most_room(client):
    voices = mailboxes(client, *TWO)
    eligible = run(voices.assignments())
    picks = run(voices.spread(6, eligible))
    assert picks.count("harvey@one.example") == 4 and picks.count("hello@two.example") == 2


def test_persona_name_never_enters_the_prompt():
    text = voice_instructions({"name": "Alex, founder", "revision": 3, "tone": "warm", "instructions": "", "examples": ""})
    assert "Alex" not in text and "warm" in text


def test_writer_splits_campaigns_and_pins_every_step_when_voices_differ(client, monkeypatch):
    from mercury.agents.sender import Sender
    from tests.test_outbox_native import Env, FakeProvider
    voices = mailboxes(client, *TWO)
    founder = create(client, name="Alex, founder", sign_name="Alex", avatar_seed=AVATAR_SEEDS[1])
    run(voices.assign("harvey@one.example", "", "Harvey"))
    run(voices.assign("hello@two.example", founder))
    for i in range(4):
        run(client.state.add_prospect(Prospect(first_name=f"P{i}", title="Owner", company="Shop", industry="Retail",
                                               email=f"p{i}@shop{i}.example", email_status="verified")))

    prompts = []

    async def think(prompt, **kwargs):
        prompts.append((kwargs.get("task"), prompt))
        if kwargs.get("task") == "write_sequence":
            return [{"step": i, "subject": "your quotes", "body": "Hi {{first_name}}, how do quotes go?", "delay_days": i - 1} for i in (1, 2, 3)]
        return {"subject": "your quotes", "body": "How do you track quotes today?"}

    monkeypatch.setattr(Brain, "think_json", AsyncMock(side_effect=think))
    run(Writer(Brain(client.state), client.state, client.config).run())

    campaigns = run(client.state.get_campaigns_by_status("draft"))
    assert sorted(c.mailbox for c in campaigns) == ["harvey@one.example", "hello@two.example"]
    sequence_prompts = [p for task, p in prompts if task == "write_sequence"]
    assert any("no other: Harvey." in p for p in sequence_prompts)
    assert any("no other: Alex." in p for p in sequence_prompts)
    assert not any("Alex, founder" in p for _task, p in prompts), "persona labels stay out of every prompt"

    sender = Sender(None, client.state, client.config, Env())
    sender.provider = FakeProvider()
    for campaign in campaigns:
        run(sender._stage_campaign_native(campaign))
    rows = run(client.state.get_outbox())
    by_campaign = {c.id: c.mailbox for c in campaigns}
    assert rows and all(row["mailbox"] == by_campaign[row["campaign_id"]] for row in rows)


def test_writer_keeps_rotation_when_every_mailbox_writes_alike(client, monkeypatch):
    voices = mailboxes(client, *TWO)
    for email in ("harvey@one.example", "hello@two.example"):
        run(voices.assign(email, "", "Harvey"))
    run(client.state.add_prospect(Prospect(first_name="Pat", email="pat@shop.example", email_status="verified")))
    model = AsyncMock(side_effect=lambda prompt, **kw: (
        [{"step": 1, "subject": "s", "body": "Hi {{first_name}}", "delay_days": 0}] if kw.get("task") == "write_sequence"
        else {"subject": "s", "body": "Hello there"}))
    monkeypatch.setattr(Brain, "think_json", model)
    run(Writer(Brain(client.state), client.state, client.config).run())
    assert [c.mailbox for c in run(client.state.get_campaigns_by_status("draft"))] == [""]
    assert all(row["mailbox"] == "" for row in run(client.state.get_outbox()))
    assert "no other: Harvey." in model.await_args_list[0].args[0]


def test_voices_api_and_prompt_preview_sign_off(client):
    mailboxes(client, *TWO)
    pid = create(client, sign_name="Alex")
    response = client.post("/api/voices/HELLO@two.example", json={"persona_id": pid, "sign_name": ""})
    assert response.status_code == 200 and response.json()["signer"] == "Alex"
    assert client.post("/api/voices/hello@two.example", json={"sign_name": "x" * 81}).status_code == 422
    assert client.post("/api/voices/nobody@x.example", json={}).status_code == 404
    data = client.get("/api/voices").json()
    assert [m["email"] for m in data["mailboxes"]] == ["harvey@one.example", "hello@two.example"]
    assert {p["id"] for p in data["personas"]} >= {pid, "workspace"}
    prospect = contact(client)
    version = profile(client, pid)["version_id"]
    prompt = client.post("/api/personas/prompt", json={"prospect_id": prospect, "version_id": version,
                                                        "mailbox": "hello@two.example"}).json()
    assert "no other: Alex." in prompt["prompt"] and prompt["persona"]["mailbox"] == "hello@two.example"


def test_voices_api_leaves_out_fields_unchanged(client):
    mailboxes(client, *TWO)
    pid = create(client, sign_name="Alex")
    assert client.post("/api/voices/hello@two.example", json={"persona_id": pid, "sign_name": "Alexandra"}).status_code == 200
    only_sign = client.post("/api/voices/hello@two.example", json={"sign_name": "Lex"}).json()
    assert (only_sign["persona"]["id"], only_sign["signer"], only_sign["follows_default"]) == (pid, "Lex", False)
    only_voice = client.post("/api/voices/hello@two.example", json={"persona_id": "workspace"}).json()
    assert (only_voice["persona"]["id"], only_voice["signer"]) == ("workspace", "Lex")
    cleared = client.post("/api/voices/hello@two.example", json={"persona_id": "", "sign_name": ""}).json()
    assert cleared["follows_default"] and cleared["signer"] == client.config.persona.name
