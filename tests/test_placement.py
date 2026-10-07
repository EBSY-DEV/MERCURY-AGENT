"""Inbox placement test: the decision rule, reading seed inboxes over IMAP
(against a fake server), a full run with fake providers, and the CLI.
Nothing here opens a network connection or sends real email."""

import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from mercury import placement as pl
from mercury.config import (
    EmailChannelConfig,
    EnvConfig,
    PlacementConfig,
    PlacementControlConfig,
    PlacementSeedConfig,
    load_env,
)
from mercury.integrations.mail_provider import SendResult
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.state import StateManager
from tests.test_mailboxes import make_config
from tests.test_outbox_native import FakeProvider


def _run(coro):
    return asyncio.run(coro)


def row(sender, seed, folder, role="fleet", domain=None):
    return {"sender": sender, "seed": seed, "folder": folder, "role": role,
            "domain": domain if domain is not None else sender.split("@")[1]}


SEEDS = ("s@gmail.com", "s@outlook.com", "s@yahoo.com")


def fleet(*folders, sender="a@x.co"):
    return [row(sender, s, f) for s, f in zip(SEEDS, folders)]


def control(*folders):
    return [row("me@gmail.com", s, f, role="control") for s, f in zip(SEEDS, folders)]


# ── The decision rule ─────────────────────────────────────────────────


def test_fleet_in_spam_and_control_in_inbox_is_a_domain_problem():
    s = pl.summarize(fleet("spam", "spam", "inbox") + control("primary", "inbox", "inbox"))
    assert s["outcome"] == "domain" and s["tone"] == "bad"
    assert "2 of 3" in s["text"] and s["fleet"]["spam"] == 2


def test_both_in_spam_is_a_copy_problem():
    s = pl.summarize(fleet("spam", "spam", "spam") + control("spam", "spam", "inbox"))
    assert s["outcome"] == "copy" and "control sender too" in s["text"]


def test_no_control_means_assume_the_copy():
    s = pl.summarize(fleet("spam", "spam", "primary"))
    assert s["outcome"] == "copy_assumed" and "No control sender is configured" in s["text"]
    lost = pl.summarize(fleet("spam", "spam", "spam") + control("missing", "missing", "unchecked"))
    assert lost["outcome"] == "copy_assumed" and "control email wasn't found" in lost["text"]


def test_fleet_in_the_inbox_points_at_copy_or_list():
    s = pl.summarize(fleet("primary", "promotions", "spam") + control("primary", "inbox", "inbox"))
    assert s["outcome"] == "fleet_inbox" and s["tone"] == "good"
    assert "2 of 3" in s["text"] and "Promotions" in s["text"] and "1 in spam" in s["text"]


def test_missing_copies_dont_count_toward_the_shares():
    # One found, in the inbox; two still in transit.
    s = pl.summarize(fleet("inbox", "missing", "unchecked"))
    assert s["outcome"] == "fleet_inbox" and s["fleet"]["found"] == 1


def test_nothing_found_is_inconclusive():
    assert pl.summarize(fleet("missing", "unchecked", "missing"))["outcome"] == "inconclusive"
    failed = pl.summarize(fleet("send_failed", "send_failed", "send_failed"))
    assert failed["outcome"] == "inconclusive" and "failed to send" in failed["text"]


def test_each_domain_is_judged_on_its_own():
    rows = (fleet("inbox", "primary", "inbox", sender="a@good.co")
            + fleet("spam", "spam", "inbox", sender="b@bad.co")
            + control("primary", "inbox", "inbox"))
    overall, domains = pl.summarize_run(rows)
    assert domains["good.co"]["outcome"] == "fleet_inbox"
    assert domains["bad.co"]["outcome"] == "domain"
    # 2 of 6 in spam overall would read as fine; the worst domain wins instead.
    assert overall["outcome"] == "domain"
    assert overall["text"].startswith("Results differ by domain. bad.co lands in spam")


def test_outcome_copy_is_plain():
    rows_sets = [fleet("spam", "spam", "spam"), fleet("inbox", "inbox", "inbox"),
                 fleet("missing", "missing", "missing"),
                 fleet("spam", "spam", "spam") + control("inbox", "inbox", "inbox"),
                 fleet("spam", "spam", "spam") + control("spam", "spam", "spam")]
    for rows in rows_sets:
        s = pl.summarize(rows)
        assert "—" not in s["text"] and "—" not in s["label"]


# ── Seeds and providers from config ───────────────────────────────────


def test_provider_and_hosts_are_guessed_from_the_address():
    assert pl.guess_provider("x@gmail.com") == "gmail"
    assert pl.guess_provider("x@hotmail.es") == "outlook"
    assert pl.guess_provider("x@yahoo.co.uk") == "yahoo"
    assert pl.guess_provider("x@acme.com") == "other"
    env = EnvConfig(placement_secrets={"PLACEMENT_GMAIL": "app-pw"})
    seed = pl.Seed(PlacementSeedConfig(email="Seed@Gmail.com", password_env="PLACEMENT_GMAIL"), env)
    assert seed.email == "seed@gmail.com" and seed.imap_host == "imap.gmail.com"
    assert seed.readable and seed.provider == "gmail"
    ws = pl.Seed(PlacementSeedConfig(email="s@acme.com", provider="gmail"), env)
    assert ws.imap_host == "imap.gmail.com" and not ws.readable      # no password: by hand
    other = pl.Seed(PlacementSeedConfig(email="s@acme.com", password_env="PLACEMENT_GMAIL"), env)
    assert other.imap_host == "" and not other.readable


def test_placement_config_and_env():
    cfg = EmailChannelConfig(placement={
        "seeds": [{"email": "s@gmail.com", "password_env": "PLACEMENT_SEED"}],
        "control": {"email": "me@gmail.com", "password_env": "PLACEMENT_CONTROL"},
        "wait_seconds": 60})
    assert isinstance(cfg.placement, PlacementConfig) and cfg.placement.wait_seconds == 60
    assert EmailChannelConfig().placement.seeds == []
    env = load_env({"PLACEMENT_SEED": "a", "MAILBOX_X": "b", "OTHER": "c"})
    assert env.secret("PLACEMENT_SEED") == "a" and env.secret("MAILBOX_X") == "b"
    assert env.secret("OTHER") == ""
    with pytest.raises(Exception):
        PlacementSeedConfig(email="not-an-address")


def _cfg(seeds=SEEDS, control=None, **email):
    cfg = make_config(**email)
    cfg.channels.email.placement = PlacementConfig(
        seeds=[PlacementSeedConfig(email=s, password_env="PLACEMENT_SEED") for s in seeds],
        control=control, wait_seconds=0)
    return cfg


def test_control_needs_its_own_password():
    env = EnvConfig(smtp_password="the-sending-mailbox-password",
                    placement_secrets={"PLACEMENT_CONTROL": "pw"})
    unset = _cfg(control=PlacementControlConfig(email="me@gmail.com"))
    email, provider, ready = pl.control_provider(unset, env)
    assert email == "me@gmail.com" and not ready   # never borrows SMTP_PASSWORD
    cfg = _cfg(control=PlacementControlConfig(email="me@gmail.com", password_env="PLACEMENT_CONTROL"))
    _email, provider, ready = pl.control_provider(cfg, env)
    assert ready and provider.smtp_host == "smtp.gmail.com" and provider.sender_address == "me@gmail.com"
    odd = _cfg(control=PlacementControlConfig(email="me@acme.com", password_env="PLACEMENT_CONTROL"))
    assert pl.control_provider(odd, env)[2] is False                 # no host to send through
    assert pl.control_provider(_cfg(), env) is None


# ── Reading a seed over IMAP (fake server) ────────────────────────────


GMAIL_LIST = [b'(\\HasNoChildren) "/" "INBOX"',
              b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
              b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
              b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']


def test_find_junk_folder():
    assert pl.find_junk_folder(GMAIL_LIST) == "[Gmail]/Spam"
    assert pl.find_junk_folder([b'(\\HasNoChildren) "/" "Bulk Mail"', b'() "/" INBOX']) == "Bulk Mail"
    assert pl.find_junk_folder([b'(\\HasNoChildren) "." Junk']) == "Junk"
    assert pl.find_junk_folder([b'(\\HasNoChildren) "/" INBOX']) is None
    assert pl.find_junk_folder(None) is None


class FakeImap:
    """Answers SEARCH from {folder: {message_id: gmail_category}}."""

    def __init__(self, folders, listing=GMAIL_LIST):
        self.folders, self.listing = folders, listing
        self.selected = None
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def list(self):
        return "OK", self.listing

    def select(self, name, readonly=False):
        assert readonly, "seed folders must be opened read-only"
        self.selected = name.strip('"')
        return ("OK", [b"1"]) if self.selected in self.folders else ("NO", [b""])

    def search(self, charset, *criteria):
        box = self.folders.get(self.selected, {})
        if criteria[0] == "HEADER":
            mid = criteria[2].strip('"')
            return "OK", [b"7" if mid in box else b""]
        assert criteria[0] == "X-GM-RAW"
        query = criteria[1].strip('"')
        hits = [m for m, cat in box.items() if f"rfc822msgid:{m.strip('<>')}" in query
                and f"category:{cat}" in query]
        return "OK", [b"7" if hits else b""]

    def logout(self):
        self.calls.append(("logout",))


def _seed(email, provider):
    env = EnvConfig(placement_secrets={"PLACEMENT_SEED": "pw"})
    return pl.Seed(PlacementSeedConfig(email=email, provider=provider,
                                       password_env="PLACEMENT_SEED"), env)


def test_gmail_seed_reads_tabs_and_spam():
    imap = FakeImap({"INBOX": {"<p@x>": "primary", "<q@x>": "promotions", "<r@x>": "updates"},
                     "[Gmail]/Spam": {"<s@x>": "spam"}})
    reader = pl.ImapSeedReader(_seed("s@gmail.com", "gmail"), connect=lambda h, p: imap)
    found = reader.locate(["<p@x>", "<q@x>", "<r@x>", "<s@x>", "<gone@x>"])
    assert found == {"<p@x>": "primary", "<q@x>": "promotions", "<r@x>": "other_tab",
                     "<s@x>": "spam"}
    assert imap.calls[0] == ("login", "s@gmail.com", "pw") and imap.calls[-1] == ("logout",)


def test_other_seeds_report_inbox_not_primary():
    imap = FakeImap({"INBOX": {"<p@x>": ""}, "Bulk Mail": {"<s@x>": ""}},
                    listing=[b'(\\HasNoChildren) "/" "Inbox"', b'(\\HasNoChildren) "/" "Bulk Mail"'])
    reader = pl.ImapSeedReader(_seed("s@yahoo.com", "yahoo"), connect=lambda h, p: imap)
    assert reader.locate(["<p@x>", "<s@x>"]) == {"<p@x>": "inbox", "<s@x>": "spam"}


# ── A full run (fake providers, fake readers, no sleeping) ───────────


@pytest.fixture
def sm(tmp_path):
    s = StateManager(str(tmp_path / "p.db"))
    _run(s.init_db())
    return s


class FakeReader:
    def __init__(self, answers):
        self.answers = answers        # {sender_domain_or_id: folder} looked up by message id
        self.calls = 0

    def locate(self, mids):
        self.calls += 1
        return {m: self.answers[m] for m in mids if m in self.answers}


def _pool(*emails):
    return MailboxPool([Mailbox(email=e, provider=FakeProvider(), daily_cap=30) for e in emails])


async def _no_sleep(_s):
    return None


def test_run_sends_from_every_mailbox_to_every_seed_and_never_touches_the_outbox(sm):
    pool = _pool("a@x.co", "b@y.co")
    cfg = _cfg()
    env = EnvConfig(placement_secrets={"PLACEMENT_SEED": "pw"})
    sent_before = _run(sm.count_outbox_sent_today_by_mailbox())

    # FakeProvider message ids are <m1@x>, <m2@x>, ... per provider; answer by id.
    gmail = FakeReader({"<m1@x>": "spam"})
    outlook = FakeReader({"<m2@x>": "inbox"})
    res = _run(pl.run(sm, cfg, env, pool, subject="Quick question", body="Hi Pat, worth a look?",
                      readers={"s@gmail.com": gmail, "s@outlook.com": outlook},
                      sleep=_no_sleep, wait_seconds=0))
    rows = res["rows"]
    assert len(rows) == 6                                          # 2 mailboxes x 3 seeds
    assert {(r["sender"], r["seed"]) for r in rows} == {(m, s) for m in ("a@x.co", "b@y.co")
                                                         for s in SEEDS}
    by = {(r["sender"], r["seed"]): r["folder"] for r in rows}
    assert by[("a@x.co", "s@gmail.com")] == "spam"
    assert by[("b@y.co", "s@gmail.com")] == "spam"                # both providers' first id is <m1@x>
    assert by[("a@x.co", "s@outlook.com")] == "inbox"
    assert by[("a@x.co", "s@yahoo.com")] == "unchecked"           # no reader: record by hand

    # The real email 1, with the legal footer, went to the seeds and nowhere else.
    sent = pool.mailboxes[0].provider.sent
    assert [s["to"] for s in sent] == list(SEEDS)
    assert sent[0]["subject"] == "Quick question"
    assert sent[0]["body"].startswith("Hi Pat") and "1 Main St" in sent[0]["body"]
    assert "Reply unsubscribe." in sent[0]["body"]

    # Outbox, daily caps and metrics are untouched.
    con = sqlite3.connect(sm.db_path)
    assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert _run(sm.count_outbox_sent_today_by_mailbox()) == sent_before
    assert con.execute("SELECT COUNT(*) FROM placement_tests").fetchone()[0] == 6

    last = json.loads(_run(sm.get_setting(pl.LAST_RUN_KEY)))
    assert last["run_id"] == res["run_id"] and last["line"].startswith(f"Placement {res['run_id']}:")


def test_run_with_control_reads_as_a_domain_problem(sm):
    pool = _pool("a@x.co")
    cfg = _cfg(seeds=("s@gmail.com",),
               control=PlacementControlConfig(email="me@gmail.com", password_env="PLACEMENT_CONTROL"))
    env = EnvConfig(placement_secrets={"PLACEMENT_SEED": "pw", "PLACEMENT_CONTROL": "pw"})
    control_provider = FakeProvider()
    control_provider.send_email = _fixed_id_sender(control_provider, "<control@gmail>")
    import mercury.placement as mod
    real = mod.control_provider
    mod.control_provider = lambda c, e: ("me@gmail.com", control_provider, True)
    try:
        res = _run(pl.run(sm, cfg, env, pool, subject="s", body="b",
                          readers={"s@gmail.com": FakeReader({"<m1@x>": "spam",
                                                              "<control@gmail>": "primary"})},
                          sleep=_no_sleep, wait_seconds=0))
    finally:
        mod.control_provider = real
    assert res["summary"]["outcome"] == "domain"
    roles = sorted(r["role"] for r in res["rows"])
    assert roles == ["control", "fleet"]
    rep = _run(pl.report(sm))
    assert rep["domains"]["x.co"]["outcome"] == "domain" and len(rep["control"]) == 1
    assert "Domain problem" in pl.format_report(rep)


def _fixed_id_sender(provider, mid):
    async def send(to_email, subject, body, thread_ref="", in_reply_to=""):
        provider.sent.append({"to": to_email, "subject": subject, "body": body})
        return SendResult(ok=True, message_id=mid)
    return send


def test_a_failed_send_is_recorded_and_the_rest_continue(sm):
    pool = _pool("a@x.co")
    pool.mailboxes[0].provider.fail_next = True
    res = _run(pl.run(sm, _cfg(), EnvConfig(), pool, subject="s", body="b",
                      readers={}, sleep=_no_sleep, wait_seconds=0))
    folders = [r["folder"] for r in sorted(res["rows"], key=lambda r: r["seed"])]
    assert folders.count("send_failed") == 1 and folders.count("unchecked") == 2
    failed = next(r for r in res["rows"] if r["folder"] == "send_failed")
    assert "smtp boom" in failed["detail"]


def test_it_keeps_reading_until_late_mail_arrives(sm):
    pool = _pool("a@x.co")
    reader = FakeReader({})
    slept = []

    async def sleep(s):
        slept.append(s)
        if len(slept) == 2:                          # arrives before the second read
            reader.answers["<m1@x>"] = "primary"

    res = _run(pl.run(sm, _cfg(seeds=("s@gmail.com",)), EnvConfig(), pool, subject="s", body="b",
                      readers={"s@gmail.com": reader}, sleep=sleep, wait_seconds=90,
                      poll_seconds=30, gap_seconds=0))
    assert res["rows"][0]["folder"] == "primary"
    assert reader.calls == 2 and slept == [30, 30]


def test_check_and_mark_after_the_run(sm):
    pool = _pool("a@x.co")
    res = _run(pl.run(sm, _cfg(), EnvConfig(), pool, subject="s", body="b",
                      readers={}, sleep=_no_sleep, wait_seconds=0))
    run_id = res["run_id"]
    assert res["summary"]["outcome"] == "inconclusive"
    # A late read finds the gmail copy.
    _run(pl.check(sm, run_id, {"s@gmail.com": FakeReader({"<m1@x>": "spam"})}))
    # Outlook can't be read over IMAP: recorded by hand.
    assert _run(pl.mark(sm, run_id, "S@Outlook.com", "a@x.co", "spam")) == 1
    assert _run(pl.mark(sm, run_id, "s@outlook.com", "nobody@x.co", "spam")) == 0
    with pytest.raises(pl.PlacementError):
        _run(pl.mark(sm, run_id, "s@outlook.com", "a@x.co", "trash"))
    rep = _run(pl.report(sm, run_id[:4]))
    folders = {r["seed"]: r["folder"] for r in rep["rows"]}
    assert folders == {"s@gmail.com": "spam", "s@outlook.com": "spam", "s@yahoo.com": "unchecked"}
    assert rep["summary"]["outcome"] == "copy_assumed"
    assert json.loads(_run(sm.get_setting(pl.LAST_RUN_KEY)))["outcome"] == "copy_assumed"
    text = pl.format_report(rep)
    assert "Not checked" in text and "mercury mail placement mark" in text


def test_plan_lists_what_is_missing(sm):
    problems = pl.plan(make_config(), EnvConfig(), None)["problems"]
    assert any("native mail provider" in p for p in problems)
    assert any("No seed inboxes" in p for p in problems)
    cfg = _cfg()
    cfg.compliance.postal_address = ""
    p = pl.plan(cfg, EnvConfig(), _pool("a@x.co"), only=["y.co"])
    assert p["fleet"] == [] and any("postal_address" in x for x in p["problems"])
    assert any("No sending mailbox" in x for x in p["problems"])
    with pytest.raises(pl.PlacementError):
        _run(pl.run(sm, cfg, EnvConfig(), _pool("a@x.co"), subject="s", body="b", readers={}))
    only = pl.plan(_cfg(), EnvConfig(), _pool("a@x.co", "b@y.co"), only=["y.co"])
    assert [f["email"] for f in only["fleet"]] == ["b@y.co"] and not only["problems"]


def test_pick_email_takes_the_newest_email_one(sm):
    assert _run(pl.pick_email(sm)) is None
    for i, (step, status) in enumerate([(1, "sent"), (2, "approved"), (1, "pending_review"),
                                        (1, "rejected")]):
        _run(sm.add_outbox_item(prospect_id=f"p{i}", to_email=f"p{i}@a.co", subject=f"s{i}",
                                body=f"b{i}", send_at="2026-10-01T10:00:00", status=status,
                                campaign_id=f"c{i}", step=step))
    assert _run(pl.pick_email(sm))["subject"] == "s2"
    first = _run(sm.get_outbox(status="sent"))[0]
    assert _run(pl.pick_email(sm, first["id"][:8]))["subject"] == "s0"
    assert _run(pl.pick_email(sm, "nope")) is None


def test_report_before_any_run(sm):
    assert _run(pl.report(sm)) is None
    assert _run(pl.resolve_run(sm, "abc")) == ""


# ── mercury mail placement ────────────────────────────────────────────


@pytest.fixture
def cli_env(monkeypatch, sm):
    import mercury.config as config_mod
    import mercury.state as state_mod

    pool = _pool("a@x.co")
    cfg = _cfg()
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(config_mod, "load_env",
                        lambda *a, **k: EnvConfig(placement_secrets={"PLACEMENT_SEED": "pw"}))
    monkeypatch.setattr(MailboxPool, "from_config", classmethod(lambda cls, c, e: pool))
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: sm)
    # Belt and braces: no IMAP connection may be opened from these tests.
    monkeypatch.setattr(pl, "default_readers", lambda seeds: {})
    _run(sm.add_outbox_item(prospect_id="p1", to_email="p1@a.co", subject="Roof on page two",
                            body="Hi Pat, worth a look?", send_at="2026-10-01T10:00:00",
                            status="pending_review", campaign_id="c1", step=1))
    return SimpleNamespace(pool=pool, cfg=cfg)


def _cli(monkeypatch, *argv):
    import mercury.cli as cli
    monkeypatch.setattr(sys, "argv", ["mercury", *argv])
    cli.main()


def test_cli_dry_run_sends_nothing(cli_env, monkeypatch, capsys):
    _cli(monkeypatch, "mail", "placement", "--dry-run")
    out = capsys.readouterr().out
    assert "Roof on page two" in out and "From:    a@x.co" in out
    assert "Seed:    s@gmail.com  (gmail, read over IMAP)" in out
    assert "Control: none configured" in out
    assert "3 test emails" in out and "Dry run: nothing sent." in out
    assert cli_env.pool.mailboxes[0].provider.sent == []


def test_cli_run_show_and_mark(cli_env, monkeypatch, capsys, sm):
    _cli(monkeypatch, "mail", "placement", "--wait", "0")
    out = capsys.readouterr().out
    assert len(cli_env.pool.mailboxes[0].provider.sent) == 3
    assert "Placement test" in out and "Not checked" in out and "Inconclusive" in out
    run_id = json.loads(_run(sm.get_setting(pl.LAST_RUN_KEY)))["run_id"]

    _cli(monkeypatch, "mail", "placement", "mark", run_id, "--seed", "s@gmail.com",
         "--sender", "a@x.co", "--folder", "primary")
    out = capsys.readouterr().out
    assert "Primary" in out and "Lands in the inbox" in out

    _cli(monkeypatch, "mail", "placement", "show", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == run_id and data["summary"]["outcome"] == "fleet_inbox"
    # The outbox email it tested is still where it was.
    assert [r["status"] for r in _run(sm.get_outbox(status="pending_review"))] == ["pending_review"]


def test_cli_show_without_runs(cli_env, monkeypatch, capsys, sm):
    with pytest.raises(SystemExit) as e:
        _cli(monkeypatch, "mail", "placement", "show")
    assert e.value.code == 1
    assert "No placement test found" in capsys.readouterr().out
