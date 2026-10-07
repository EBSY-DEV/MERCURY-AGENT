"""Inbox editing must preserve credentials, identities and existing threads."""

import os
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values
from fastapi.testclient import TestClient

import mercury.config as config_module
import mercury.dashboard as dash
from mercury import inbox_settings
from mercury.config import load_config, load_env
from mercury.integrations.smtp_mail import SmtpImapProvider


@pytest.fixture
def client(tmp_path, monkeypatch):
    template = Path(__file__).parents[1] / "mercury.yaml"
    raw = yaml.safe_load(template.read_text())
    raw["persona"]["email"] = "original@example.com"
    raw["channels"]["email"].update(provider="smtp", mailboxes=[], max_daily_sends=50)
    config = tmp_path / "mercury.yaml"
    config.write_text(yaml.safe_dump(raw))
    env = tmp_path / ".env"
    env.write_text("# Keep this comment\nSMTP_HOST='smtp.example.com'\nUNRELATED_KEY='a b # c'\n")
    local = tmp_path / "mercury.local.yaml"
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(config_module, "_find_config_file", lambda: str(local if local.exists() else config))
    monkeypatch.setattr(dash, "ENV_FILE", env)
    monkeypatch.setattr(dash, "_check_mercury_pid", lambda: None)
    with TestClient(dash.app) as c:
        c.config_path, c.env_path, c.local_path, c.raw = config, env, local, raw
        yield c


def boxes(client):
    return client.get("/api/settings/mailboxes").json()["inboxes"]


def put_boxes(client, entries):
    raw = dict(client.raw)
    raw["channels"]["email"]["mailboxes"] = entries
    client.config_path.write_text(yaml.safe_dump(raw))


def test_add_inbox_uses_private_config_and_literal_password(client):
    before = client.config_path.read_text()
    password = "word's # \\\t ${OTHER} = yes"
    res = client.post("/api/settings/mailboxes", json={"email": " NEW@Example.com ", "password": password})
    assert res.status_code == 200 and res.json()["success"]
    assert password not in res.text and "password_env" not in res.text
    assert client.config_path.read_text() == before
    assert client.local_path.exists()
    cfg = load_config(str(client.local_path))
    box = cfg.channels.email.mailboxes[0]
    assert box.email == "new@example.com" and box.warmup_start is not None
    assert load_env(inbox_settings.env_values(client.env_path)).secret(box.password_env) == password
    text = client.env_path.read_text()
    assert "# Keep this comment" in text
    assert dotenv_values(client.env_path, interpolate=False)["UNRELATED_KEY"] == "a b # c"
    assert client.env_path.stat().st_mode & 0o777 == 0o600
    assert boxes(client)[0]["password_set"]
    assert cfg.channels.email.max_daily_sends == 50
    assert password not in client.local_path.read_text()


def test_add_without_password_then_edit_without_erasing_it(client):
    assert client.post("/api/settings/mailboxes", json={"email": "a@example.com"}).status_code == 200
    assert boxes(client)[0]["password_set"] is False
    res = client.patch("/api/settings/mailboxes/a@example.com", json={"password": "new-secret", "daily_cap": 12})
    assert res.json()["inbox"]["configured"]
    client.patch("/api/settings/mailboxes/a@example.com", json={"password": "", "enabled": False})
    cfg, pool = dash._mail_context()
    assert pool.primary.provider.smtp_pass == "new-secret"
    assert pool.primary.accepts_new is False and pool.resolve("a@example.com") is pool.primary
    assert pool.primary.daily_cap == 12
    assert "new-secret" not in client.get("/api/settings/mailboxes").text


@pytest.mark.parametrize("password", [r"two\\slashes", r"literal\nsequence", "slash\\'quote", '${TOKEN} # space'])
def test_password_special_characters_roundtrip(client, password):
    res = client.post("/api/settings/mailboxes", json={"email": "a@example.com", "password": password})
    assert res.status_code == 200
    _, pool = dash._mail_context()
    assert pool.primary.provider.smtp_pass == password


def test_general_settings_save_preserves_inbox_credentials_and_comments(client):
    password = r"inbox\\credential's # ${TOKEN}"
    client.post("/api/settings/mailboxes", json={"email": "a@example.com", "password": password})
    assert client.post("/api/settings/env", json={"SERPER_API_KEY": "new-key"}).json()["success"]
    _, pool = dash._mail_context()
    assert pool.primary.provider.smtp_pass == password
    assert "# Keep this comment" in client.env_path.read_text()


def test_first_add_preserves_old_sender_and_separate_imap_credentials(client):
    inbox_settings.write_env(client.env_path, {"SMTP_USERNAME": "smtp-login@example.com", "SMTP_PASSWORD": "old",
                                             "IMAP_USERNAME": "imap-login@example.com", "IMAP_PASSWORD": "imap-old"})
    client.post("/api/settings/mailboxes", json={"email": "new@example.com", "password": "new"})
    cfg, pool = dash._mail_context()
    assert [b.email for b in pool.mailboxes] == ["original@example.com", "new@example.com"]
    old = pool.resolve("")
    assert old.email == "original@example.com"
    assert old.provider.smtp_user == "smtp-login@example.com" and old.provider.smtp_pass == "old"
    assert old.provider.imap_user == "imap-login@example.com" and old.provider.imap_pass == "imap-old"


def test_password_change_does_not_change_another_inboxs_shared_secret(client):
    put_boxes(client, [{"email": e, "password_env": "MAILBOX_SHARED"} for e in ("a@example.com", "b@example.com")])
    inbox_settings.write_env(client.env_path, {"MAILBOX_SHARED": "shared"})
    client.patch("/api/settings/mailboxes/a@example.com", json={"password": "changed"})
    _, pool = dash._mail_context()
    assert pool.resolve("a@example.com").provider.smtp_pass == "changed"
    assert pool.resolve("b@example.com").provider.smtp_pass == "shared"


@pytest.mark.parametrize("payload", [
    [], {"email": "bad"}, {"email": "a@example.com", "daily_cap": -1},
    {"email": "a@example.com", "smtp_port": 65536},
    {"email": "a@example.com", "enabled": "false"},
    {"email": "a@example.com", "password_env": "TAVILY_API_KEY"},
    {"email": "a@example.com", "password": "pw\nINJECTED=yes"},
    {"email": "a@example.com", "warmup_start": "never"},
    {"email": [], "password": "secret"},
])
def test_invalid_inbox_never_changes_files(client, payload):
    before = (client.config_path.read_text(), client.env_path.read_text())
    assert client.post("/api/settings/mailboxes", json=payload).status_code == 400
    assert not client.local_path.exists()
    assert (client.config_path.read_text(), client.env_path.read_text()) == before


def test_duplicates_and_address_changes_are_rejected(client):
    client.post("/api/settings/mailboxes", json={"email": "a@example.com"})
    assert client.post("/api/settings/mailboxes", json={"email": "A@EXAMPLE.COM"}).status_code == 409
    assert client.patch("/api/settings/mailboxes/a@example.com", json={"email": "b@example.com"}).status_code == 400
    assert client.patch("/api/settings/mailboxes/missing@example.com", json={"password": "pw"}).status_code == 404
    assert len(boxes(client)) == 1


def test_provider_switch_requires_explicit_confirmation(client):
    client.raw["channels"]["email"]["provider"] = "gmail"
    client.config_path.write_text(yaml.safe_dump(client.raw))
    assert client.post("/api/settings/mailboxes", json={"email": "a@example.com"}).status_code == 400
    res = client.post("/api/settings/mailboxes", json={"email": "a@example.com", "activate_smtp": True})
    assert res.status_code == 200
    assert load_config(str(client.local_path)).channels.email.provider == "smtp"


def test_credential_write_failure_rolls_back_config(client, monkeypatch):
    before = client.env_path.read_text()
    monkeypatch.setattr(inbox_settings, "write_env", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    res = client.post("/api/settings/mailboxes", json={"email": "a@example.com", "password": "secret"})
    assert res.status_code == 500 and "secret" not in res.text
    assert not client.local_path.exists() and client.env_path.read_text() == before


def test_symlinked_private_config_is_updated_in_place(client):
    target = client.config_path.with_name("state-config.yaml")
    target.write_text(client.config_path.read_text())
    client.local_path.symlink_to(target)
    client.post("/api/settings/mailboxes", json={"email": "a@example.com"})
    assert client.local_path.is_symlink()
    assert load_config(str(target)).channels.email.mailboxes[0].email == "a@example.com"


def test_test_connection_uses_saved_inbox_and_redacts_provider_errors(client, monkeypatch):
    client.post("/api/settings/mailboxes", json={"email": "a@example.com", "password": "private"})
    tested = []

    async def check(provider):
        tested.append(provider.smtp_user)
        return False, "Server echoed private"

    monkeypatch.setattr(SmtpImapProvider, "test_connection", check)
    res = client.post("/api/settings/mailboxes/a@example.com/test")
    assert tested == ["a@example.com"] and res.json()["success"] is False
    assert "private" not in res.text


def test_setup_status_recognizes_per_inbox_credentials(client):
    client.post("/api/settings/mailboxes", json={"email": "a@example.com", "password": "pw"})
    checks = client.get("/api/setup-status").json()
    # This route's setup check uses the same mailbox pool as the sender.
    assert next(c for c in checks["checks"] if c["id"] == "email_provider")["done"]
