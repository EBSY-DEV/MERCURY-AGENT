# Changelog

Notable changes to Mercury Agent (formerly Harvey). Dates are release dates; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and versions
follow [semver](https://semver.org/), with the caveat that Mercury is pre-1.0 and
minor versions can still change behaviour.

## [Unreleased]

### Added

- **Exclusions and company limits.** Exact-address and domain exclusions
  (subdomains only when you ask), with a reason, a source and an append-only
  history. Opt-outs and bounces are recorded as exclusions on the address, so
  deleting, re-importing or rediscovering a contact never reactivates it.
  Exclusions are re-checked in the transaction that claims each email, and
  queued email they cover is blocked until you send it back to review. New
  per-company limits (`max_new_contacts_per_company_per_day`,
  `max_active_contacts_per_company`) and a company hold when someone replies
  (`pause_company_on_reply`, on by default). Dashboard **Exclusions** tab,
  reasons in the Outbox, and `mercury exclusions` / `mercury holds` commands.
  Requeued exclusions require a fresh approval even with automatic follow-ups;
  blocked sequences can be discarded without lifting their exclusion. Active
  company limits count each contact once across campaigns.

### Changed

- **`max_bounce_rate` now defaults to 2%** (was 5%), and the rate check starts
  after 50 sends (was 10). Configs that set the value keep it. `mercury sending
  resume` and the dashboard's Resume now clear the per-bucket counters too.

- **Harvey is now Mercury Agent.** The package (`mercury/`), CLI
  (`mercury ...`), config (`mercury.yaml`, `mercury.local.yaml`) and database
  (`data/mercury.db`) are renamed. Existing checkouts migrate automatically:
  `harvey.local.yaml` and `data/harvey.db*` / `harvey.log` are renamed in place
  on first start, never overwriting a newer file. Re-run `pip install -e .`
  so the `mercury` command exists. The distribution is now `mercury-agent`
  (the `mercury` command and the deprecated `harvey` alias are unchanged).
- **License is now AGPL-3.0-or-later.** Code that originates from Harvey stays
  available under its original MIT license; see `NOTICE`.
- **Dashboard redesign.** Light, lavender-tinted theme with a single violet
  accent; dark mode re-tokenised as deep aubergine. Sans-only type (Geist +
  Geist Mono). Tokens live in `mercury/web/app.css`; the matching Pencil
  source is `design/mercury-brand.pen` (rebuild with
  `design/build_brand_pen.py`).
- **Warm-up follows the mailbox config.** The Warm-up tab shows every mailbox
  in `channels.email.mailboxes` (or the single configured inbox) with the ramp
  the sender enforces. Caps and start dates can be edited in Settings →
  Sending inboxes; the tab keeps pause/resume, the checklist, notes and DNS checks. A
  per-mailbox health gate sits on top of the ramp: over 5% bounces (after 20
  sends in 7 days) pauses that mailbox until resumed, 3-5% holds it at
  yesterday's cap. A paused mailbox still sends replies. Bounce events now
  record the mailbox they came back to. `MERCURY_SENDER_EMAIL` is gone.
- The deprecated `harvey` command still works as an alias of `mercury`, and
  conversation threads recorded with sender `harvey` still read as ours.
- `scripts/cloud_run.sh` / `local_dashboard.sh` read `MERCURY_STATE_REPO`
  (falling back to `HARVEY_STATE_REPO`), keep state in `.mercury-state/`, and
  rename a pre-rename state repo's `harvey.*` files on the next run.

### Added

- **Bounces are classified by SMTP code** (#54). The Handler reads the enhanced
  status code (`5.1.1`, `5.7.26`) from each bounce and stores `dsn_code` and a
  `bucket` on the `bounce` event. A bad address (`LIST`) invalidates the prospect
  as before; a sender block (`SENDER`, any 5.7.x) pauses that mailbox; a `BURNED`
  code (5.7.606 to 5.7.614) pauses every mailbox on the domain and flags it
  `CANCEL_CANDIDATE`; a 4.x.x throttle halves the mailbox's cap for 7 days; and
  `NOISE` (mailbox full and similar) is ignored by every rate. The global kill
  switch now also trips when sender plus burned bounces pass 20% of the
  classified ones (after 30). The Mailboxes drawer shows the breakdown. No schema
  change.

- **CSV contact import** (#3). Dashboard (Contacts → Import CSV), `mercury import`
  share one service. It has a read-only preview
  with per-row outcomes (new, needs enrichment, duplicate, invalid), column
  mapping with header aliases, delimiter detection, a skip or fill-blanks policy
  for duplicates, and a single-transaction commit that is safe to retry.
  Imported contacts are held at status `imported` with `guess` addresses.
  Verifying (`mercury imports verify`) and releasing to outreach
  (`mercury imports release`) are separate, explicit steps. Schema v13 adds
  `import_batches`, `import_rows` and the prospects' `import_batch_id` /
  `import_row`.

- **Inbox management in Settings.** Add SMTP/IMAP inboxes, set or replace
  passwords, edit per-inbox servers, caps and warm-up dates, stop new outreach
  while finishing existing threads, and test connections without sending.
  Credentials are stored privately and never returned to the browser. The
  existing single sender is preserved when enabling rotation; configuration
  saves use the private local file by default. Restart a running agent after saving.
- **Mailbox rotation.** `channels.email.mailboxes` lists several SMTP
  mailboxes, each with its own daily cap and an optional warm-up ramp
  (`warmup_start`, `warmup_initial_cap`, `warmup_weekly_increase`).
  `max_daily_sends` still caps the total. A thread keeps its mailbox: the
  first email rotates, follow-ups go out from the same address, and replies
  from the inbox they answer. Every inbox is polled, and one unreachable
  inbox no longer hides the others. `enabled: false` stops new threads but
  keeps reading the inbox. Mail of a thread whose mailbox was removed from
  the config is held, never re-routed to another address.
- **`auto_approve_followups`.** Approving a first email approves its
  follow-ups, in the dashboard immediately and in the agent loop each cycle.
- **`spread_sends`.** Paces the day's remaining sends over the cycles left
  before quiet hours.
- **Dashboard.** A Sending mailboxes card on the Outbox (sends in the last 24
  hours against today's cap, warm-up stage, missing passwords), and the From
  address on every queued and sent email. `mercury mail` tests every mailbox.

### Fixed

- **Fresh databases could get stuck on "duplicate column name".** The
  dashboard's parallel first-load requests each ran schema migrations at
  once; a half-applied migration left the schema version behind for good.
  Migrations now take the write lock first and apply atomically.
- The usage tab's daily bar chart referenced undefined colour tokens and
  rendered invisible bars.
- The heartbeat backed off an hour on `budget_exhausted`, a value the cycle
  never returns; a spent Claude quota now backs off on `over_budget` as meant.
- A follow-up waits its delay after the previous email actually went out,
  not after the day the sequence was staged. Approving an old opener no
  longer sends its follow-ups right behind it.
- Replies are sent before follow-ups and first emails, and are not held by a
  mailbox's warm-up cap.

## [0.2.0] — 2026-09-13

The release that made Harvey worth running. The previous version could write
and send email but could not reliably find anyone to send it to, and it needed
a paid third-party platform to send at all. Both are fixed.

### Added

- **A prospecting pipeline that works.** Four stages, each independently
  runnable and re-runnable: DISCOVER (who exists) → PROFILE (what they are) →
  ENRICH (who decides) → VERIFY (can you reach them).
- **DISCOVER, with a provider menu.** Four sources behind one adapter
  interface, shown side by side in the dashboard with what each does, what it
  costs, its free tier and which keys it needs. The default — OpenStreetMap —
  needs no account, no card and no key, so the whole pipeline can be run
  before paying anyone. `harvey discover`.
- **Cost discipline in the collector.** Every discovery run estimates before it
  spends, refuses to start over your cap, re-checks the cap between batches,
  reads a kill switch, and flushes observations per batch so a run that dies
  keeps what it learned. `harvey discover --estimate` prints the projection and
  exits.
- **PROFILE.** Free — three HTTP requests per business, no browser, no model
  call. Reads the incumbent agency out of the site footer, ad pixels, missing
  schema, abandoned blogs, open roles and named people. Chained onto DISCOVER
  and run on every heartbeat. `harvey profile`.
- **A signal catalog you confirm.** Harvey knows how to collect 23 signals and
  collects none of them until a human says so. Each shows a plain-language
  description, what it costs, and how many companies already carry it.
  `harvey signals`.
- **The cohort builder.** Pick what a prospect must have and what disqualifies
  them; see the matching companies live. A prospect list is a query you can
  explain, not a file.
- **Native mail providers.** Gmail (REST + OAuth) and any SMTP+IMAP mailbox.
- **The outbox approval ladder.** Every outgoing email waits for approval by
  default (`pending_review → approved → sent`), reviewed one at a time in a
  decisions desk with `A`/`R`/`J`/`K`. A deterministic pre-send gate — no model
  involved — rejects unrendered merge tags, banned phrases, over-length bodies,
  too many links, HTML, undeliverable addresses and recipient mismatches.
- **Bounce handling and a kill switch.** A bounce marks the address invalid,
  cancels that prospect's queued sends, and trips a global pause past a
  configured bounce rate. `harvey sending pause|resume`.
- **Claude usage tracking.** Per-call attribution by agent, task and model, plus
  live subscription-quota gauges read the same way `/usage` does, so Harvey
  throttles itself and leaves headroom for your own interactive work.
  `harvey usage`.
- **CSV export.** `harvey export` writes a sequencer-ready list. Harvey is worth
  running as a list-builder even if you never let it send.
- **A dashboard worth looking at.** Opens on Today — what needs a human — with
  an activity feed and a rail of pipeline figures, collector runs and setup.
  Light and dark are the same design re-tokenised; appearance follows your OS by
  default. Fonts are vendored so it renders with the network off.
- **A Python version guard.** `python3 -m venv` on macOS builds from the system
  3.9, which cannot run Harvey. It now fails immediately with the fix instead of
  surfacing later as an unrelated-looking ImportError.

### Changed

- **The data model.** Every fact Harvey learns is now an observation — a row in
  `observations`, never a column. A new signal needs no migration, re-observing
  over time is a free time series, and confidence and provenance travel with the
  fact. The vocabulary is governed: a database trigger rejects any signal code
  not in `signal_codes`.
- **Email finding is pattern-first.** Harvey derives the domain's address format
  and verifies one candidate rather than brute-forcing name variations, and
  grades every address honestly as `verified` / `risky` / `guess` / `invalid`.
  Only deliverable addresses are ever sent to.
- **The default mail provider** is now `gmail`. Instantly is still supported as
  a legacy option.
- **Discovery depth defaults to 30, not 100.** Google removed
  100-results-per-page in September 2025, so depth 100 is now billed as ten
  pages nearly everywhere — and below rank 30 it is mostly directories.
- **The dashboard's HTML, CSS and JS** are real files in `harvey/web/` instead
  of a 1,500-line string literal in `dashboard.py`. Still no build step.

### Fixed

- **Guessed emails were being reported as verified** whenever the domain merely
  had an MX record. The migration downgrades every legacy `email_verified=1` row
  to `guess`.
- **Cohorts counted the wrong companies.** PROFILE records a false boolean when
  it checks and finds nothing — which is correct — but `cohort()` matched on the
  row existing, so "companies running Google Ads" silently meant "companies we
  checked for Google Ads", which was all of them. Cohorts now read the newest
  observation per company and signal, and require it to be true.
- **Usage tracking counted every Claude session on the machine**, not just
  Harvey's. The transcript reconciler is gone; the ledger contains only rows
  Harvey's own Brain wrote.
- **Page titles were being extracted as people** ("Request Service", "Commercial
  Services"), and titles bled between team members.
- **Confirming a signal before anything had loaded the catalog** reported success
  while changing nothing.
- **The free OSM provider answered unmapped search terms with every registered
  office in the area** — coffee roasters and churches for a roofing query. It now
  refuses the query and names the trades it can search.

### Security

- Test fixtures no longer carry real businesses' phone numbers or street
  addresses.
- `harvey.local.yaml` (gitignored) overrides the tracked `harvey.yaml`, so a
  fork can hold real product configuration without it reaching a public repo.

## [0.1.0]

Initial release: the heartbeat loop, five sub-agents, the skills library, the
website trainer, Instantly-based sending, and the first dashboard.
