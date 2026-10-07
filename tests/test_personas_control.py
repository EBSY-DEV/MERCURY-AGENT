"""The persona service shared by the dashboard, the CLI and MCP."""

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

import mercury.config as config_module
from mercury import cli
from mercury.config import MercuryConfig
from mercury.control.personas import PersonaService, avatar_seed
from mercury.personas import AVATAR_SEEDS, PersonaError
from mercury.state import StateManager

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def config():
    return MercuryConfig(**yaml.safe_load(TEMPLATE.read_text()))


@pytest.fixture
def service(tmp_path, config):
    return run(PersonaService(StateManager(str(tmp_path / "mercury.db")), config).ready())


def new(service, name="Alex, founder", **changes):
    return run(service.create({"name": name, "tone": "direct and warm", **changes}))


def test_find_by_id_name_and_prefix(service):
    persona = new(service)
    assert run(service.find(persona["id"]))["id"] == persona["id"]
    assert run(service.find("ALEX, founder"))["id"] == persona["id"]
    assert run(service.find(persona["id"][:6]))["id"] == persona["id"]
    with pytest.raises(PersonaError) as missing:
        run(service.find("nobody"))
    assert missing.value.code == "not_found"


def test_names_stay_unique(service):
    first = new(service)
    with pytest.raises(PersonaError) as duplicate:
        new(service, name="alex, FOUNDER")
    assert duplicate.value.code == "invalid"
    other = new(service, name="Operator")
    with pytest.raises(PersonaError):
        run(service.update(other["id"], {"name": "Alex, founder"}))
    assert run(service.update(first["id"], {"name": "Alex, founder"}))["name"] == "Alex, founder"


def test_partial_update_versions_only_writing(service):
    persona = new(service, instructions="Short sentences")
    renamed = run(service.update(persona["id"], {"description": "Founder voice", "avatar_seed": AVATAR_SEEDS[4]}))
    assert renamed["revision"] == 1 and renamed["instructions"] == "Short sentences"
    rewritten = run(service.update("Alex, founder", {"tone": "plain"}))
    assert rewritten["revision"] == 2 and rewritten["description"] == "Founder voice"
    assert [v["revision"] for v in run(service.versions(persona["id"]))] == [2, 1]


def test_stale_revision_and_unknown_fields_are_rejected(service):
    persona = new(service)
    run(service.update(persona["id"], {"tone": "plain"}))
    with pytest.raises(PersonaError) as stale:
        run(service.update(persona["id"], {"tone": "warmer"}, expected_revision=1))
    assert stale.value.code == "revision_conflict"
    with pytest.raises(PersonaError) as unknown:
        run(service.update(persona["id"], {"password": "x"}))
    assert unknown.value.code == "invalid"
    with pytest.raises(PersonaError) as empty:
        run(service.update(persona["id"], {"tone": ""}))
    assert empty.value.code == "invalid"


def test_default_and_archive_rules(service):
    persona = new(service)
    run(service.set_default(persona["id"]))
    assert run(service.find(persona["id"]))["is_default"]
    with pytest.raises(PersonaError):
        run(service.set_archived(persona["id"], True))
    run(service.set_default("workspace"))
    assert run(service.set_archived(persona["id"], True))["archived"]
    assert persona["id"] not in [p["id"] for p in run(service.list(include_archived=False))["personas"]]
    with pytest.raises(PersonaError):
        run(service.set_default(persona["id"]))


def test_avatar_numbers():
    assert avatar_seed("7") == AVATAR_SEEDS[6]
    assert avatar_seed(AVATAR_SEEDS[0]) == AVATAR_SEEDS[0]
    with pytest.raises(PersonaError):
        avatar_seed("99")


def test_prompt_by_revision_and_contact_email(service):
    from mercury.models.prospect import Prospect
    prospect = Prospect(first_name="Maria", last_name="Chen", email="maria@lakewood.example",
                        company="Lakewood Dental")
    run(service.state.add_prospect(prospect))
    persona = new(service, instructions="Mention one thing about their business")
    run(service.update(persona["id"], {"instructions": "Never mention pricing"}))
    first = run(service.prompt("Alex, founder", "maria@lakewood.example", revision=1))
    assert "Mention one thing" in first["prompt"] and first["persona"]["revision"] == 1
    latest = run(service.prompt(persona["id"], prospect.id))
    assert "Never mention pricing" in latest["prompt"]
    with pytest.raises(PersonaError) as missing:
        run(service.prompt(persona["id"], prospect.id, revision=9))
    assert missing.value.code == "not_found"


def test_cli_round_trip(tmp_path, monkeypatch, capsys, config):
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("mercury.state.DB_PATH", tmp_path / "mercury.db")

    def mercury(*argv):
        monkeypatch.setattr(sys, "argv", ["mercury", "personas", *argv])
        cli.main()
        return capsys.readouterr().out

    examples = tmp_path / "examples.txt"
    examples.write_text("Hi Maria,\nQuick question.")
    assert "as v1" in mercury("create", "--name", "Alex", "--tone", "warm", "--avatar", "3", "--default")
    assert "as v2" in mercury("edit", "alex", "--examples-file", str(examples))
    assert "still v2" in mercury("edit", "Alex", "--description", "Founder")
    monkeypatch.setattr(sys, "stdin", io.StringIO("Keep it short."))
    assert "as v3" in mercury("edit", "Alex", "--instructions-file", "-")
    assert json.loads(mercury("show", "Alex", "--json"))["instructions"] == "Keep it short."
    listed = json.loads(mercury("list", "--json"))
    alex = next(p for p in listed["personas"] if p["name"] == "Alex")
    assert alex["is_default"] and alex["avatar_seed"] == AVATAR_SEEDS[2] and alex["examples"].startswith("Hi Maria")
    with pytest.raises(SystemExit):
        mercury("archive", "Alex")
    captured = capsys.readouterr()
    assert "Choose another default" in captured.err and captured.out == ""


def test_cli_text_sources_fail_cleanly(tmp_path, monkeypatch, capsys, config):
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("mercury.state.DB_PATH", tmp_path / "mercury.db")

    def mercury(*argv):
        monkeypatch.setattr(sys, "argv", ["mercury", "personas", *argv])
        cli.main()
        return capsys.readouterr()

    assert "as v1" in mercury("create", "--name", "Alex", "--tone", "warm").out
    monkeypatch.setattr(sys, "stdin", io.StringIO("Only once."))
    with pytest.raises(SystemExit) as twice:
        mercury("edit", "Alex", "--instructions-file", "-", "--examples-file", "-")
    captured = capsys.readouterr()
    assert twice.value.code == 1 and captured.out == ""
    assert "stdin can only be used for one of --instructions-file/--examples-file" in captured.err
    with pytest.raises(SystemExit) as missing:
        mercury("edit", "Alex", "--examples-file", str(tmp_path / "nope.txt"))
    captured = capsys.readouterr()
    assert missing.value.code == 1 and "Cannot read" in captured.err and "nope.txt" in captured.err
    assert json.loads(mercury("show", "Alex", "--json").out)["revision"] == 1
