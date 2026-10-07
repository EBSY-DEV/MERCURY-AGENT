"""CSV contact imports: parsing, classification, commit, verification, release.

Every test runs offline against a throwaway SQLite database. The same
ImportService backs the CLI and the dashboard, so the interface tests at
the bottom only check that each surface reaches it with identical outcomes.
"""

import asyncio
import base64
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.csv_import as ci
import mercury.dashboard as dash
from mercury.control.imports import ImportService
from mercury.csv_import import ImportFileError, read_csv
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.state import StateManager

BASIC = (
    "Company Name,Work Email,First Name,Last Name,Title,Website,Email Status,Notes\n"
    '"Acme, Inc",Jane@ACME.com,Jane,Doe,CEO,https://www.acme.com/,verified,"line one\nline two"\n'
    "Acme,bob@acme.com,Bob,,,,,\n"
    ",not-an-email,X,Y,Z,,,\n"
    ",joe@gmail.com,Joe,Smith,Owner,,,\n"
    "Acme,jane@acme.com,Jane,Doe,CEO,,,\n"
)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "mercury.db")


@pytest.fixture
def service(db_path):
    return asyncio.run(ImportService(StateManager(db_path)).ready())


def run(coro):
    return asyncio.run(coro)


def rows(db_path, sql, params=()):
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, params).fetchall()


def by_row(result):
    return {r["row"]: r for r in result["rows"]}


# ── Reading the file ──


def test_bom_quoted_commas_and_multiline_cells():
    parsed = read_csv(("﻿" + BASIC).encode())
    assert parsed.headers[0] == "Company Name"  # BOM stripped
    first = dict(parsed.rows[0][1])
    assert first["Company Name"] == "Acme, Inc"
    assert first["Notes"] == "line one\nline two"
    # Row numbers count records, not lines: the multiline cell is still row 2.
    assert [n for n, _ in parsed.rows] == [2, 3, 4, 5, 6]


def test_non_utf8_is_refused_with_a_fix():
    with pytest.raises(ImportFileError) as e:
        read_csv("Email,Name\njosé@x.com,José\n".encode("latin-1"))
    assert e.value.code == "not_utf8" and "UTF-8" in str(e.value)


def test_semicolon_and_tab_are_detected():
    assert read_csv(b"Email;Name\na@b.co;A\n").delimiter == ";"
    assert read_csv(b"Email\tName\na@b.co\tA\n").delimiter == "\t"


def test_ambiguous_delimiter_asks_and_explicit_choice_wins():
    data = b"Email,Name;Title\na@b.co,A;CEO\nc@d.co,C;CTO\n"
    with pytest.raises(ImportFileError) as e:
        read_csv(data)
    assert e.value.code == "ambiguous_delimiter"
    assert set(e.value.details["candidates"]) == {"comma", "semicolon"}
    assert read_csv(data, "semicolon").headers == ["Email,Name", "Title"]


def test_missing_email_column_names_the_headers(service):
    with pytest.raises(ImportFileError) as e:
        run(service.preview(b"Name,Company\nA,B\n"))
    assert e.value.code == "missing_email_column"
    assert e.value.details["headers"] == ["Name", "Company"]


def test_explicit_mapping_to_a_missing_column_is_refused(service):
    with pytest.raises(ImportFileError) as e:
        run(service.preview(b"Mail\na@b.co\n", mapping={"email": "Nope"}))
    assert e.value.code == "unknown_column"


def test_limits(monkeypatch):
    monkeypatch.setattr(ci, "MAX_ROWS", 2)
    with pytest.raises(ImportFileError) as e:
        read_csv(b"Email\na@b.co\nc@d.co\ne@f.co\n")
    assert e.value.code == "too_many_rows"
    monkeypatch.setattr(ci, "MAX_BYTES", 10)
    with pytest.raises(ImportFileError) as e:
        read_csv(b"Email\na@b.co\nc@d.co\n")
    assert e.value.code == "too_large"


def test_header_aliases_and_full_name_split(service):
    result = run(service.preview(b"E-mail Address,Name,Organisation,Job Title\nana@ruiz.es,Ana Ruiz,Ruiz SL,CEO\n"))
    assert result["mapping"] == {"email": "E-mail Address", "full_name": "Name",
                                 "company_name": "Organisation", "title": "Job Title"}
    assert result["rows"][0]["name"] == "Ana Ruiz"
    assert result["rows"][0]["outcome"] == "new"


# ── Preview ──


def test_preview_classifies_rows_and_changes_nothing(service, db_path, monkeypatch):
    import httpx

    def no_network(*a, **k):
        raise AssertionError("preview must not call the network")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)

    before = rows(db_path, "SELECT COUNT(*) FROM prospects")
    result = run(service.preview(BASIC.encode()))
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == before
    assert rows(db_path, "SELECT COUNT(*) FROM import_batches") == [(0,)]

    c = result["counts"]
    assert (c["new"], c["incomplete"], c["duplicate"], c["invalid"]) == (2, 1, 1, 1)
    r = by_row(result)
    assert r[3]["outcome"] == "incomplete" and r[3]["missing"] == ["last name", "title"]
    assert r[4]["outcome"] == "invalid" and "not a valid email" in r[4]["reason"]
    assert r[6]["outcome"] == "duplicate" and r[6]["reason"] == "same email as row 2"
    assert result["ignored_columns"] == ["Email Status"]


# ── Commit ──


def test_commit_imports_held_unverified_contacts(service, db_path):
    result = run(service.commit(BASIC.encode(), filename="list.csv", skip_invalid=True, origin="test"))
    b = result["batch"]
    assert (b["created"], b["skipped"], b["excluded"]) == (3, 1, 1)
    got = rows(db_path, "SELECT email, status, email_status, email_verified, source, import_batch_id, "
                        "import_row, personalization_notes FROM prospects ORDER BY import_row")
    assert [g[0] for g in got] == ["jane@acme.com", "bob@acme.com", "joe@gmail.com"]
    for email, status, email_status, verified, source, batch, _, _ in got:
        # A CSV claiming "verified" is data, never a verdict.
        assert (status, email_status, verified, source, batch) == ("imported", "guess", 0, "csv_import", b["id"])
    assert got[0][7] == "line one\nline two"
    # Every row, including skipped and invalid ones, has a recorded outcome.
    outcomes = rows(db_path, "SELECT row_number, action FROM import_rows ORDER BY row_number")
    assert outcomes == [(2, "created"), (3, "created"), (4, "excluded"), (5, "created"), (6, "skipped")]


def test_imported_contacts_are_not_enrolled(service, db_path):
    run(service.commit(BASIC.encode(), skip_invalid=True))
    state = StateManager(db_path)
    assert run(state.get_prospects_by_status("new")) == []  # the Writer reads only 'new'
    assert rows(db_path, "SELECT COUNT(*) FROM outbox") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM campaigns") == [(0,)]


def test_company_identity_uses_normalised_domains(service, db_path):
    run(StateManager(db_path).add_company(Company(name="Acme", domain="acme.com")))
    run(service.commit(BASIC.encode(), skip_invalid=True))
    companies = rows(db_path, "SELECT name, domain, external_id FROM companies ORDER BY name")
    # www.acme.com and the @acme.com addresses resolve to the existing company.
    assert companies == [("Acme", "acme.com", "")]
    acme_id = rows(db_path, "SELECT id FROM companies WHERE domain = 'acme.com'")[0][0]
    assert rows(db_path, "SELECT company_id FROM prospects WHERE email = 'bob@acme.com'") == [(acme_id,)]


def test_public_email_providers_are_not_one_company(service, db_path):
    data = b"Email,Name\nann@gmail.com,Ann\nbo@gmail.com,Bo\ncy@yahoo.com,Cy\n"
    run(service.commit(data))
    assert rows(db_path, "SELECT COUNT(*) FROM companies") == [(0,)]
    assert rows(db_path, "SELECT DISTINCT company_id FROM prospects") == [("",)]


def test_company_name_without_domain_dedups_by_name(service, db_path):
    data = b"Email,Company\nann@gmail.com,Joe's Plumbing\nbo@yahoo.com,joe's  plumbing\n"
    run(service.commit(data))
    assert rows(db_path, "SELECT external_id FROM companies") == [("import:joe s plumbing",)]


def test_repeated_import_creates_no_duplicates(service, db_path):
    run(service.commit(BASIC.encode(), skip_invalid=True))
    # Same contacts, different file (case and website spelling changed).
    again = BASIC.replace("Jane@ACME.com", "JANE@acme.COM").replace("https://www.acme.com/", "acme.com")
    result = run(service.commit(again.encode(), skip_invalid=True))
    assert result["batch"]["created"] == 0
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == [(3,)]


def test_retry_after_commit_is_a_no_op(service, db_path):
    first = run(service.commit(BASIC.encode(), skip_invalid=True))
    second = run(service.commit(BASIC.encode(), skip_invalid=True))
    assert second["already_committed"] is True
    assert second["batch"]["id"] == first["batch"]["id"]
    assert rows(db_path, "SELECT COUNT(*) FROM import_batches") == [(1,)]
    assert len(second["rows"]) == 5


def test_invalid_rows_must_be_left_out_explicitly(service, db_path):
    with pytest.raises(ImportFileError) as e:
        run(service.commit(BASIC.encode()))
    assert e.value.code == "invalid_rows" and e.value.details["rows"] == [4]
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == [(0,)]
    # Excluding the invalid row by number is as explicit as skip_invalid.
    result = run(service.commit(BASIC.encode(), exclude_rows=[4, 5]))
    r = by_row(result)
    assert r[4]["action"] == "excluded"
    assert r[5]["action"] == "excluded" and r[5]["reason"] == "left out by you"
    assert result["batch"]["created"] == 2


def test_commit_rolls_back_on_failure(service, db_path, monkeypatch):
    calls = {"n": 0}
    real = StateManager.insert_prospect

    async def flaky(db, prospect):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return await real(db, prospect)
    monkeypatch.setattr(StateManager, "insert_prospect", staticmethod(flaky))
    with pytest.raises(RuntimeError):
        run(service.commit(BASIC.encode(), skip_invalid=True))
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM companies") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM import_batches") == [(0,)]


def test_commit_revalidates_after_preview(service, db_path):
    preview = run(service.preview(BASIC.encode()))
    assert by_row(preview)[5]["outcome"] == "new"
    # Someone adds joe@gmail.com between preview and commit.
    run(StateManager(db_path).add_prospect(Prospect(email="joe@gmail.com", first_name="Joe")))
    result = run(service.commit(BASIC.encode(), skip_invalid=True))
    assert by_row(result)[5]["outcome"] == "duplicate"
    assert rows(db_path, "SELECT COUNT(*) FROM prospects WHERE email = 'joe@gmail.com'") == [(1,)]


def test_concurrent_insert_is_reported_not_duplicated(service, db_path, monkeypatch):
    """If a row's address appears after classification, the unique index wins
    and the row is reported as a duplicate rather than failing the batch."""
    run(StateManager(db_path).add_prospect(Prospect(email="joe@gmail.com", first_name="Joe")))

    async def blind(db, values):
        return None, ""
    monkeypatch.setattr(ImportService, "_existing", staticmethod(blind))
    result = run(service.commit(BASIC.encode(), skip_invalid=True))
    row = by_row(result)[5]
    assert row["action"] == "skip" and "another process" in row["reason"]
    assert rows(db_path, "SELECT COUNT(*) FROM prospects WHERE email = 'joe@gmail.com'") == [(1,)]


def test_parallel_commits_import_each_contact_once(db_path):
    async def both():
        a = await ImportService(StateManager(db_path)).ready()
        b = ImportService(StateManager(db_path))
        data_b = BASIC.replace("Notes", "Notes ").encode()  # a different file, same people
        return await asyncio.gather(a.commit(BASIC.encode(), skip_invalid=True),
                                    b.commit(data_b, skip_invalid=True))
    first, second = run(both())
    assert first["batch"]["created"] + second["batch"]["created"] == 3
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == [(3,)]


# ── Duplicates: policies and suppression ──


def test_fill_policy_fills_blanks_only(service, db_path):
    state = StateManager(db_path)
    run(state.add_prospect(Prospect(email="bob@acme.com", first_name="Bob", title="",
                                    personalization_notes="my own note", status="contacted",
                                    email_status="verified", email_verified=True)))
    data = b"Email,First Name,Last Name,Title,Notes\nBOB@acme.com,Robert,Jones,CFO,csv note\n"
    skip = run(service.preview(data))
    assert skip["rows"][0]["action"] == "skip"
    result = run(service.commit(data, policy="fill"))
    assert result["rows"][0]["action"] == "filled"
    assert set(result["rows"][0]["fill"]) == {"last_name", "title"}
    got = rows(db_path, "SELECT first_name, last_name, title, personalization_notes, status, "
                        "email_status FROM prospects")
    assert got == [("Bob", "Jones", "CFO", "my own note", "contacted", "verified")]


def test_opted_out_and_invalid_contacts_are_never_touched(service, db_path):
    state = StateManager(db_path)
    run(state.add_prospect(Prospect(email="gone@x.com", status="opted_out", email_status="verified")))
    run(state.add_prospect(Prospect(email="bounce@x.com", status="new", email_status="invalid")))
    data = b"Email,Title,Email Status\ngone@x.com,CEO,verified\nbounce@x.com,CTO,verified\n"
    result = run(service.commit(data, policy="fill"))
    for r in result["rows"]:
        assert r["action"] == "skipped" and r["suppressed"]
    assert rows(db_path, "SELECT email, status, email_status, title FROM prospects ORDER BY email") == [
        ("bounce@x.com", "new", "invalid", ""), ("gone@x.com", "opted_out", "verified", "")]


def test_same_name_at_same_company_is_a_duplicate(service, db_path):
    run(StateManager(db_path).add_prospect(
        Prospect(email="jd@acme.com", first_name="Jane", last_name="Doe", company="Acme")))
    result = run(service.preview(b"Email,First Name,Last Name,Company\njane@acme.com,jane,DOE,acme\n"))
    assert result["rows"][0]["reason"] == "same name at the same company as an existing contact"


def test_linkedin_urls_match_in_any_spelling(service, db_path):
    run(StateManager(db_path).add_prospect(Prospect(
        first_name="Jane", last_name="Doe", company="Acme", email="",
        linkedin_url="https://www.linkedin.com/in/janedoe")))
    result = run(service.preview(
        b"Email,First Name,Last Name,Company,LinkedIn\n"
        b"jane@acme.io,Jane,Doe,Acme Inc,http://linkedin.com/in/JaneDoe/?trk=x\n"
        b"pt@acme.io,P,T,Acme,pt.linkedin.com/in/pt\n"
        b"pt2@acme.io,P,T,Acme,https://PT.linkedin.com/in/PT/\n"))
    r = by_row(result)
    assert r[2]["reason"] == "same LinkedIn profile as an existing contact"
    assert r[3]["action"] == "create"
    assert r[4]["reason"] == "same LinkedIn profile as row 3"


def test_leaving_out_the_first_duplicate_lets_the_next_one_in(service, db_path):
    data = b"Email,First Name,Last Name\nx@foo.io,A,B\nx@foo.io,A,B\n"
    r = by_row(run(service.preview(data, exclude_rows=[2])))
    assert (r[2]["action"], r[3]["action"]) == ("exclude", "create")
    run(service.commit(data, exclude_rows=[2]))
    assert rows(db_path, "SELECT import_row FROM prospects") == [(3,)]


def test_batch_prefix_lookup_has_no_wildcards(service):
    result = run(service.commit(BASIC.encode(), skip_invalid=True))
    batch_id = result["batch"]["id"]
    assert run(service.batch(batch_id[:4]))["batch"]["id"] == batch_id
    for bad in ("%", "_", ""):
        with pytest.raises(ImportFileError):
            run(service.batch(bad))


def test_stored_reasons_carry_no_row_contents(service, db_path):
    run(service.commit(b"Email\nsecret-person@example\n", skip_invalid=True))
    (reason,) = rows(db_path, "SELECT reason FROM import_rows")[0]
    assert "secret" not in reason and reason == "not a valid email address"


# ── Verification and release ──


class Env:
    reoon_api_key = "k"
    zerobounce_api_key = ""
    hunter_api_key = ""


def test_verification_is_separate_and_release_moves_only_verified(db_path, monkeypatch):
    svc = run(ImportService(StateManager(db_path), Env()).ready())
    result = run(svc.commit(BASIC.encode(), skip_invalid=True))
    batch = result["batch"]["id"]
    estimate = run(svc.verify_estimate(batch))
    assert estimate["addresses"] == 3 and estimate["providers"][0]["name"] == "Reoon"

    verdicts = {"jane@acme.com": "verified", "bob@acme.com": "invalid", "joe@gmail.com": None}

    async def fake(self, email):
        return verdicts[email] or "guess"
    monkeypatch.setattr(ImportService, "_verify_one", fake)

    seen = []

    async def progress(done, total):
        seen.append((done, total))
    step = run(svc.verify(batch, limit=2, progress=progress))
    assert step["checked"] == 2 and step["remaining"] == 1 and seen == [(1, 2), (2, 2)]
    step = run(svc.verify(batch, limit=2, after_row=step["next_after_row"]))
    assert step["checked"] == 1 and step["remaining"] == 0

    released = run(svc.release(batch))
    assert released == {"batch_id": batch, "released": 1, "still_held": 2}
    assert rows(db_path, "SELECT email, status FROM prospects ORDER BY import_row") == [
        ("jane@acme.com", "new"), ("bob@acme.com", "imported"), ("joe@gmail.com", "imported")]


def test_verify_needs_a_configured_provider(service):
    result = run(service.commit(BASIC.encode(), skip_invalid=True))
    with pytest.raises(ImportFileError) as e:
        run(service.verify(result["batch"]["id"]))
    assert e.value.code == "no_verifier"


# ── Interfaces: CLI and dashboard ──


def _cli(monkeypatch, capsys, db_path, *argv):
    import mercury.cli as cli
    import mercury.state as state_mod
    monkeypatch.setattr(state_mod, "DB_PATH", db_path)
    monkeypatch.setattr(sys, "argv", ["mercury", *argv])
    cli.main()
    return capsys.readouterr().out


@pytest.fixture
def csv_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "list.csv"
        path.write_text(BASIC)
        yield path


@pytest.fixture
def client(monkeypatch, db_path):
    monkeypatch.setattr(dash, "DB_PATH", Path(db_path))
    with TestClient(dash.app) as c:
        yield c


def test_cli_dry_run_changes_nothing(monkeypatch, capsys, db_path, csv_file):
    out = _cli(monkeypatch, capsys, db_path, "import", str(csv_file), "--dry-run", "--json")
    assert json.loads(out)["counts"]["new"] == 2
    assert rows(db_path, "SELECT COUNT(*) FROM prospects") == [(0,)]


def test_cli_and_dashboard_give_identical_outcomes(monkeypatch, capsys, csv_file, client):
    data = csv_file.read_bytes()
    body = {"filename": "list.csv", "content_b64": base64.b64encode(data).decode()}
    preview = client.post("/api/imports/preview", json=body).json()

    with tempfile.TemporaryDirectory() as tmp:
        other = str(Path(tmp) / "cli.db")
        out = _cli(monkeypatch, capsys, other, "import", str(csv_file), "--skip-invalid", "--json")
        cli_rows = [(r["row"], r["outcome"], r["action"], r["reason"]) for r in json.loads(out)["rows"]]

    committed = client.post("/api/imports/commit", json={**body, "skip_invalid": True}).json()
    web_rows = [(r["row"], r["outcome"], r["action"], r["reason"]) for r in committed["rows"]]
    assert cli_rows == web_rows
    assert [r["outcome"] for r in preview["rows"]] == [r[1] for r in web_rows]

    listed = client.get("/api/imports").json()["batches"]
    assert listed[0]["origin"] == "dashboard" and listed[0]["held"] == 3
    contacts = client.get("/api/prospects").json()
    assert {p["import_batch_id"] for p in contacts} == {committed["batch"]["id"]}


def test_dashboard_errors_carry_a_code(client):
    body = {"content_b64": base64.b64encode(b"Email,Name;Title\na@b.co,A;CEO\n").decode()}
    res = client.post("/api/imports/preview", json=body)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "ambiguous_delimiter"
    res = client.post("/api/imports/commit", json={
        "content_b64": base64.b64encode(BASIC.encode()).decode()})
    assert res.json()["detail"] == {"code": "invalid_rows", "message": res.json()["detail"]["message"],
                                    "rows": [4]}
    assert client.get("/api/imports/nope").status_code == 404

