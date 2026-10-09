# Configuration

Mercury reads two kinds of configuration: a YAML file that describes what you sell, who you sell to and how Mercury behaves, and environment variables (usually a `.env` file) that hold credentials. This page explains which YAML file is used, lists every key with its default, lists every environment variable, and shows how to change Mercury's voice through `prompts/` and `skills/`. Defaults below are taken from `mercury/config.py`.

## Which config file is used

Mercury looks for its YAML config in this order and uses the first one that exists:

1. The file named by the `MERCURY_CONFIG` environment variable. If it is set but the file does not exist, Mercury stops with an error rather than falling back.
2. `mercury.local.yaml` in the current directory, then in the project root.
3. `mercury.yaml` in the current directory, its parent, then the project root.

| File | Tracked in git | Purpose |
|---|---|---|
| `mercury.yaml` | yes | An untrained template. Leave it generic in a public fork. |
| `mercury.local.yaml` | no (gitignored) | Your real configuration. `mercury train` writes this file. |
| `MERCURY_CONFIG=/path/file.yaml` | n/a | Point at any file explicitly, e.g. a demo config or a per-environment config. |

The split exists so a public checkout can run a private business configuration without ever committing your positioning, pricing or targeting.

Errors are reported by field, for example `channels.email.max_daily_sends: must be >= 0`. Mercury will not start with an invalid config.

Most settings are read when a process starts. After editing the YAML, restart `mercury run` and the dashboard. Skills and prompts are the exception: they are read from disk on each use.

## YAML reference

### `persona`

Who the emails come from. All fields are required.

| Key | Description |
|---|---|
| `name` | Sender's display name. Also the default From name for each mailbox. |
| `company` | Your company name. Appears in the compliance footer. If it is still `Your Company`, `mercury run` treats Mercury as unconfigured and opens the setup wizard. |
| `role` | Sender's role, used in prompts. |
| `email` | Sender address. With a single SMTP mailbox it is the From address; with rotation it identifies which mailbox owns threads started before rotation. |
| `linkedin` | LinkedIn profile URL, used in prompts. |
| `tone` | Free text, e.g. `professional, consultative, confident`. |

The **Voice & Personas** dashboard tab imports `persona.tone` into a saved
**Workspace voice** on first use. After that, the saved default persona supplies
the writing tone; edit it in the tab. Sender name, company, role and email still
come from this YAML section. Persona changes take effect on the next generation
without restarting the agent. YAML business settings still require a restart.

Tone, writing preferences and style examples are versioned. Names, descriptions
and Critters avatars are visual metadata and do not create a writing version.
Campaign sequences and their personalized openers keep their originating persona
version. Regenerations and replies retain their email/thread's saved voice.
Shared prompts and knowledge continue to load from disk on each generation;
each email's history stores the exact assembled prompt that was used.

### `product`

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | yes | | Product or service name. |
| `description` | yes | | One or two sentences. |
| `pricing` | yes | | Free text. |
| `key_benefits` | yes | | List of strings. |
| `objection_responses` | yes | | Map of objection to response, e.g. `"too expensive": "..."`. May be `{}`. |
| `offer` | no | see below | What the outreach is trying to get. |

`product.offer`:

| Key | Default | Description |
|---|---|---|
| `primary` | `""` | Main offer, e.g. `Monthly plan from $99/mo`. |
| `entry` | `""` | Low-commitment entry, e.g. `Free audit`. |
| `goal` | `book_call` | `book_call`, `start_trial` or `get_reply`. |
| `booking_method` | `calendar_link` | `calendar_link`, `suggest_times` or `ask_preference`. |
| `booking_url` | `""` | Calendar link, used with `calendar_link`. |
| `meeting_duration` | `15 minutes` | Free text. |
| `meeting_owner` | `""` | Who takes the meeting. |

### `icp`

| Key | Required | Default | Description |
|---|---|---|---|
| `industries` | yes | | List. Discovery searches each industry in each place. |
| `company_size` | yes | | Free text, e.g. `10-200 employees`. |
| `titles` | yes | | Target job titles. |
| `geography` | yes | | List of places, e.g. `"Denver, CO"`. Discovery uses these as cities. |
| `hiring_signals` | no | `[]` | Role keywords that mean a company is buying now (careers pages, job boards). Empty falls back to `titles`. |
| `geo_coordinates` | no | `{}` | Maps a `geography` entry to `"lat,lng,radius_km"`. Listings providers search a radius, not a name. Cities without an entry are geocoded once through OpenStreetMap Nominatim and cached. |
| `markets` | no | `[]` | Market-aware discovery: each market searches its own terms in its own places and language. When set, discovery uses markets instead of `industries` x `geography`. |

Each entry in `markets`:

| Key | Default | Description |
|---|---|---|
| `name` | required | Label. |
| `places` | required | List of places. |
| `terms` | required | Search terms used in those places. |
| `lang` | `en` | Language hint for providers that localise results. |
| `language_line` | `""` | The whole "Language and register" instruction the Writer gets for prospects in this market. Empty keeps the built-in line. |
| `language_rules` | `[]` | Extra rules for this market, one sentence each. They are added to every prompt for these prospects (first emails, follow-ups and the shared sequence). |
| `terminology` | `{}` | `{word to avoid: word to say instead}`. Rendered as "say X, never Y". |

```yaml
icp:
  industries: ["Roofing contractor", "HVAC contractor"]
  company_size: "5-50 employees"
  titles: ["Owner", "General Manager"]
  geography: ["Denver, CO", "Boulder, CO"]
  geo_coordinates:
    "Denver, CO": "39.7392,-104.9903,50"
  markets:
    - name: "Colorado"
      places: ["Denver, CO", "Boulder, CO"]
      terms: ["roofing contractor", "hvac contractor"]
      lang: "en"
```

### `channels.email`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Set `false` to stop the sender entirely. |
| `provider` | `instantly` | `gmail`, `smtp` or `instantly`. The tracked template sets `gmail`; the code default and the trainer's output are `instantly`, so set this explicitly. |
| `max_daily_sends` | `50` | Hard cap on all native sends (first emails, follow-ups and replies) in a rolling 24 hours. On Instantly, the number of leads added per day. |
| `send_to_risky` | `false` | Also send to `risky` (catch-all) addresses. Off means verified addresses only. |
| `require_approval` | `true` | Native providers: every outgoing email, including replies, waits in the outbox until approved. |
| `max_bounce_rate` | `0.02` | Global kill switch threshold, checked once 50 emails have been sent. `0` disables this rate check (the sender/burned share check stays on). See [Bounces](email-and-deliverability.md#bounces-and-the-kill-switch). |
| `mailboxes` | `[]` | SMTP only: rotate sends across several mailboxes. Empty uses the single `SMTP_*` mailbox. Ignored (with a warning) for other providers. |
| `warmup_initial_cap` | `5` | Day-one cap for a mailbox with a `warmup_start`. |
| `warmup_weekly_increase` | `5` | Added to the cap every 7 days, up to the mailbox's `daily_cap`. |
| `auto_approve_followups` | `false` | With approval on, approving a first email also approves its follow-ups. Replies still need approval. |
| `thread_followups` | `true` | Native providers: send follow-ups as replies in the first email's thread (`In-Reply-To` / `References`, Gmail `threadId`, subject `Re: <first subject>`). `false` sends each step as a new email with its own subject. Also on the dashboard's Settings tab. See [Follow-ups in the same thread](email-and-deliverability.md#the-approval-outbox). |
| `spread_sends` | `false` | Pace the day's remaining cold sends evenly over the cycles left before quiet hours, instead of up to 8 per cycle. |
| `max_new_contacts_per_company_per_day` | `0` | Native providers: first emails to a known company in a rolling 24 hours (the same window as `max_daily_sends`, so no timezone applies). `0` = no limit. See [Exclusions and company limits](email-and-deliverability.md#exclusions-and-company-limits). |
| `max_active_contacts_per_company` | `0` | Native providers: contacts at one company with an unfinished cold sequence at once. A paused sequence keeps its slot until its remaining steps are rejected or cancelled. `0` = no limit. |
| `pause_company_on_reply` | `true` | Native providers: when a person at a company replies, hold cold mail to their colleagues until you resume it. Auto-replies, read receipts and bounces never trigger it. |
| `placement` | none | Seed inboxes and a control sender for the [placement test](email-and-deliverability.md#placement-test) (`mercury mail placement`). |

`placement`:

| Key | Default | Description |
|---|---|---|
| `seeds` | `[]` | Inboxes you own that the test sends to and reads: ideally one Gmail, one Outlook and one Yahoo. |
| `seeds[].email` | required | The seed address. |
| `seeds[].provider` | guessed | `gmail`, `outlook`, `yahoo`, `icloud` or `other`, guessed from the address. Set `gmail` for a Google Workspace seed on its own domain so Primary and Promotions are told apart. |
| `seeds[].password_env` | `""` | Env var with the seed's IMAP app password (`PLACEMENT_*` or `MAILBOX_*`). Empty: Mercury doesn't read this seed; record its folders with `mercury mail placement mark`. |
| `seeds[].imap_host` / `imap_port` / `username` | guessed / `993` / `email` | Only needed for providers Mercury can't guess. |
| `control` | none | A mailbox with a known-good reputation (usually a personal Gmail) that sends the same text as the control. |
| `control.email` | required | The control address. |
| `control.password_env` | `""` | Env var with its SMTP app password (`PLACEMENT_*` or `MAILBOX_*`). Never falls back to `SMTP_PASSWORD`. |
| `control.smtp_host` / `smtp_port` / `username` / `name` | guessed / `587` / `email` / `persona.name` | Only needed for providers Mercury can't guess. |
| `wait_seconds` | `180` | How long to keep looking for the test emails in the seeds. |

```yaml
channels:
  email:
    placement:
      seeds:
        - email: me.seed@gmail.com
          password_env: PLACEMENT_GMAIL_SEED      # a Google app password
        - email: me.seed@yahoo.com
          password_env: PLACEMENT_YAHOO_SEED
        - email: me.seed@outlook.com              # no password: record by hand
      control:
        email: me@gmail.com
        password_env: PLACEMENT_CONTROL
```

Each entry in `mailboxes`:

| Key | Default | Description |
|---|---|---|
| `email` | required | Sending address. Lowercased; must be unique in the list. |
| `name` | `""` | From display name. Empty uses `persona.name`. |
| `username` | `""` | SMTP/IMAP login. Empty uses `email`. |
| `password_env` | `SMTP_PASSWORD` | Name of the environment variable holding this mailbox's password. Must be a `MAILBOX_*` variable, `SMTP_PASSWORD` or `IMAP_PASSWORD`. Passwords never go in YAML. |
| `smtp_host` | `""` | Empty uses `SMTP_HOST`. |
| `smtp_port` | `0` | `0` uses `SMTP_PORT`. |
| `imap_host` | `""` | Empty uses `IMAP_HOST`, then the SMTP host. |
| `imap_port` | `0` | `0` uses `IMAP_PORT`. |
| `imap_username` | `""` | Empty uses the SMTP login. Set when IMAP uses a different username. |
| `imap_password_env` | `""` | Empty uses this mailbox's SMTP password. Otherwise names a mailbox password variable using the same allowlist as `password_env`. |
| `daily_cap` | `30` | Steady-state ceiling once warm-up is done. Must be >= 0. |
| `warmup_start` | none | First day this mailbox sends cold mail (`YYYY-MM-DD`). Omit for an already-warm mailbox. A future date means it sends nothing until then. |
| `enabled` | `true` | `false`: start no new threads here, but keep reading its inbox and finishing its existing threads. |

How rotation, warm-up and health gates use these is covered in [Email and deliverability](email-and-deliverability.md#mailbox-rotation).

**Settings → Sending inboxes** manages these inboxes without editing YAML or `.env` manually. New configurations are saved to `mercury.local.yaml` by default; explicitly selected configs and existing private configs are updated in place. Restart a running agent after saving.

### `channels.linkedin`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | LinkedIn prospecting only runs when this is true and `LINKEDIN_EMAIL` is set. |
| `max_daily_connections` | `20` | |
| `max_daily_messages` | `10` | |

### `usage`

| Key | Default | Description |
|---|---|---|
| `max_daily_claude_percent` | `80` | Stop model work when either Claude quota window (5-hour or weekly) reaches this utilization. Must be in (0, 100]. When the quota can't be read, Mercury falls back to its own call count, capped at 200 x this percent (160 calls a day by default). |
| `heartbeat_interval_minutes` | `15` | Sleep between cycles. Minimum 1. |
| `quiet_hours.start` | `22:00` | 24h `HH:MM`. |
| `quiet_hours.end` | `07:00` | Ranges may cross midnight. |
| `quiet_hours.timezone` | `America/New_York` | IANA name. Also defines "today" for mailbox warm-up days. |

During quiet hours the loop sleeps: no sending, no inbox polling, no discovery. `mercury run --once` skips the cycle unless you pass `--ignore-quiet-hours`.

### `compliance`

| Key | Default | Description |
|---|---|---|
| `postal_address` | `""` | Physical postal address for the footer. **The native sender holds the outbox while this is empty.** |
| `opt_out_line_en` | `Not relevant? Reply "unsubscribe" and you won't hear from me again.` | English opt-out line. |
| `opt_out_line_es` | `¿No es para ti? Responde "baja" y no te escribo más.` | Spanish opt-out line, used when the email body reads as Spanish. |

The opt-out lines also help the reply handler strip Mercury's own quoted text out of inbound replies, so keep them distinctive. See [Compliance](email-and-deliverability.md#compliance-footer-and-opt-outs).

### `offers`

Optional. Without it, every prospect is written from the `product` description, as before. With it, the Writer routes each prospect to one offer and writes from that offer's brief only. Keep real offers in `mercury.local.yaml`; the examples here are placeholders.

**Routing.** At write time each prospect gets the first offer, in the order listed, whose rule matches. A rule is any of:

- `markets`: names from `icp.markets`. A company is in a market when its location or domain contains one of the market's `places` (the same match the Writer uses to pick a language).
- `segments`: the prospect's industry, or its company's, compared case-insensitively.
- `signals.require` / `signals.exclude`: signal codes, read like a cohort. Only the newest observation of each signal counts, and a `0` means "checked, not there".

When no rule matches, the offer with `default: true` is used. With no default, the prospect is written without an offer. An offer with no rule is only ever used as the default, so a list written only for the [demo gate](email-and-deliverability.md) (`key`, `requires_demo`, `demo_kind`) routes nothing and changes nothing.

Prospects are grouped so one campaign carries one offer. The campaign and every outbox row (personalized first emails included) carry its `offer_key`, and the reason for each prospect's offer is kept. `mercury offers` lists the rules in order and flags signal codes that are unknown or not confirmed (a rule over a signal Mercury does not collect never matches). `mercury offers route EMAIL` explains one prospect's offer; add `--brief` to print what the Writer would get.

**The brief.** For each email the Writer gets a brief for the selected offer and nothing about any other offer:

- the offer's `content`: its name, what it is (`summary`) and the only claims it may make (`claims`);
- the call to action and angle for the step being written (`steps`). Follow-ups get their own;
- verified facts: the newest observation of each code in `facts` (default: the codes in `signals.require`). An existing `SERP_RANK` observation is given here when you list it; nothing new is collected;
- aggregate evidence, when `evidence` is set and enough companies were checked (below);
- the case studies in scope for this prospect (below);
- the one confirmed pain from the [pain library](pains.md) that fits the prospect and this offer, with the owner's words, the scene and the cost. Mercury never makes one up; when none fits, the brief says none was supplied (see [Pains in the Writer](pains.md#pains-in-the-writer));
- the claim restrictions in `restrictions`, plus a standing rule against invented numbers, clients and results.

When an offer has a `content.summary`, the brief is authoritative: the product description, benefits and pricing in the prompt point to it, and the `product_knowledge` skill (the trainer's description of everything you sell) is left out. Without a summary, the brief adds its calls to action, case studies and restrictions to the usual product description.

**Stating the offer.** The first email makes an observation and asks one question, and does not pitch. A step whose `state_offer` is `true` is the exception: that one email states the offer in one plain sentence, in the words of `content.sentence` (else `content.summary`), and nothing else about it. Mercury has no wording of its own for this. Later emails never repeat the sentence: each follow-up is shown the emails before it and told to add something new.

**Case studies.** Each entry under `case_studies` has a `name`, optional `aliases`, the approved `summary`, and a `scope` of `markets` and `segments` (empty means anywhere). Only the selected offer's case studies in scope for the prospect reach the prompt. A draft naming any other configured case study is discarded by the Writer, and the [pre-send gate](email-and-deliverability.md#the-pre-send-gate) blocks it if it gets into the outbox another way, such as a manual edit.

**Evidence.** `evidence` describes a statistic computed from observations, never typed in: of the companies checked for every `require` signal (only those in `segments`, when set), how many carry them all and none of `exclude`. The brief quotes it only when at least `min_sample` companies were checked and at least one matched, only for prospects in its `segments`, and only in its `steps` (empty means all).

**Supporting materials.** `materials` is metadata for people (a sample, a one-pager). It is listed by `mercury offers`, is not given to the Writer, and nothing waits for it.

| Key | Default | Description |
|---|---|---|
| `key` | required | Letters, digits, `-` or `_`. What campaigns and outbox rows carry. |
| `default` | `false` | The fallback offer. At most one. |
| `markets` | `[]` | `icp.markets` names. An unknown name is a config error. |
| `segments` | `[]` | Industries. |
| `signals.require` / `signals.exclude` | `[]` | Signal codes. |
| `content.name` / `content.summary` / `content.claims` | `""` / `""` / `[]` | The approved description. |
| `steps` | `{}` | `{1: {cta, angle, state_offer}, 2: ..., 3: ...}`, steps 1 to 5. `state_offer: true` has that email state the offer in one plain sentence (see below). |
| `content.sentence` | `""` | The offer as one plain sentence, in approved words. A step with `state_offer` uses it, else `content.summary`. One of them is required for such a step. |
| `routing_role` | `""` | Who a message to a shared inbox with no known contact name asks to be passed to. Empty uses `writer.routing_role`. |
| `facts` | `[]` | Signal codes given as verified facts. Empty uses `signals.require`. |
| `case_studies` | `[]` | `name`, `aliases`, `summary`, `scope.markets`, `scope.segments`. |
| `restrictions` | `[]` | Claim restrictions, stated in the brief as written. |
| `evidence` | none | `description`, `require`, `exclude`, `segments`, `min_sample` (20), `steps`. |
| `materials` | `[]` | `name`, `kind`, `url`, `notes`. Metadata only. |
| `requires_demo` / `demo_kind` | `false` / `""` | The [demo gate](email-and-deliverability.md). |

```yaml
icp:
  markets:
    - name: "market_a"
      places: ["Exampleville"]
      terms: ["service_a"]

offers:
  - key: "offer_a"
    markets: ["market_a"]
    segments: ["segment_a"]
    signals:
      require: ["SIGNAL_A"]
      exclude: ["SIGNAL_B"]
    content:
      name: "Offer A"
      summary: "One plain sentence on what offer A is."
      claims: ["An approved claim about offer A."]
    facts: ["SERP_RANK", "SIGNAL_A"]
    steps:
      1: {angle: "The observation to open on.", cta: "The one question to ask."}
      2: {angle: "A different angle for the follow-up.", cta: "An interest-based question."}
      3: {cta: "A low-pressure close."}
    case_studies:
      - name: "Example Client Co"
        summary: "What happened, in approved words."
        scope: {markets: ["market_a"], segments: ["segment_a"]}
    restrictions: ["Never quote a price."]
    evidence:
      description: "carried SIGNAL_A"
      require: ["SIGNAL_A"]
      segments: ["segment_a"]
      min_sample: 30
  - key: "offer_b"
    default: true
    content:
      summary: "One plain sentence on what offer B is."
```

### `writer`

How the Writer's drafts are held to the rules its prompts state. All keys are optional.

| Key | Default | Description |
|---|---|---|
| `word_limits` | `{1: 90, 2: 80, 3: 50}` | Words allowed per sequence step (1 to 5), counted over the whole email body with the greeting and the sign-off. Set only the steps you want to change. |
| `routing_role` | `the person who handles this` | Who a shared inbox with no known contact name is asked to pass the message to, when the offer sets no `routing_role`. |
| `default_language_line` | `""` | The language instruction for prospects in no `icp.markets` entry. Empty keeps the built-in line. |
| `language_rules` | `[]` | Extra language rules for prospects in no market. |

```yaml
writer:
  word_limits: {1: 90, 2: 80, 3: 50}
  routing_role: "the person who handles this"
icp:
  markets:
    - name: "market_a"
      places: ["Exampleville"]
      terms: ["service_a"]
      language_rules: ["Address the reader formally."]
      terminology: {"avoided_term": "preferred_term"}
offers:
  - key: "offer_a"
    routing_role: "the person who schedules service_a"
    content: {sentence: "One plain sentence stating offer A."}
    steps: {1: {state_offer: true, cta: "The one question to ask."}}
```

**One source of truth for length.** The number in every prompt (the template's rule, the first-email task, each follow-up and the shared sequence) is rendered from `word_limits`, and the same numbers are enforced in code. The static markdown in `prompts/` and `skills/` refers to "the word limit for this step" and states no body length of its own. A word is a whitespace-separated token with at least one letter or digit; a merge variable such as `{{first_name}}` is one word; the subject and the legal footer added at send time are not counted (`mercury/draft_rules.py`).

**Over the limit.** A draft over its step's limit is asked for again once, with an explicit "at most N words including greeting and sign-off", and the shorter of the two is kept. If it is still over it is staged *flagged*: the Outbox row carries `word_count`, `word_limit` and `flags`, it is never auto-approved (not by `require_approval: false`, not by `auto_approve_followups`, not by "Approve all"), and approving it takes an explicit choice. An edit or a regeneration is measured again. The sequence's follow-ups are measured on the rendered email when the Sender stages them, so a template that still runs long is flagged per prospect. See [Flagged drafts](email-and-deliverability.md#flagged-drafts).

**Language rules per market.** `language_line`, `language_rules` and `terminology` on an `icp.markets` entry (and the `writer` defaults for everyone else) carry language and register rules in private configuration instead of in the shared prompts. They are added to, or with `language_line` replace, the built-in language line, and apply to first emails, follow-ups and the shared sequence alike.

**Names and facts.** The Writer is given the business's short name (legal suffixes such as LLC, Inc, Corp, Co, Ltd, PLLC and a location after a dash or pipe removed: "Acme Roofing LLC - Springfield" becomes "Acme Roofing") next to its full name, is told to use the full name at most once and the short name after that and in subjects, and a draft that repeats the full name has the repeats shortened in code. Review counts and star ratings never reach the Writer's facts (the `REVIEW_COUNT` and `REVIEW_RATING` observations, and any sentence of a description or note that quotes them); scoring still reads them.

**Greeting.** A contact with a name of their own, or a registry name a person accepted ([Public registry lookup](prospecting.md#public-registry-lookup)), is greeted by first name. A registry name still waiting for review is not used. A shared inbox (`info@`, `office@`, ...) with no known name gets no greeting at all, generic or by business name, and the email's one ask is a short request to pass the message to the offer's `routing_role` (else `writer.routing_role`). If the model writes a generic greeting anyway, it is removed in code for such a reader and flagged for a named one. The Sender drops a `Hi {{first_name}},` line from the template for contacts with no usable name.

## Environment variables (`.env`)

Copy `.env.example` to `.env`. Mercury loads the project's `.env` into the process environment (variables already set in the environment win), so a container or a scheduled job can inject the same variables without a file. `.env` is gitignored.

### Email provider (pick one)

| Variable | Default | Used for |
|---|---|---|
| `GMAIL_CLIENT_ID` | | Gmail API OAuth client (type "Desktop app"). Then run `mercury gmail auth`. |
| `GMAIL_CLIENT_SECRET` | | |
| `SMTP_HOST` | | SMTP server. |
| `SMTP_PORT` | `587` | STARTTLS on 587; implicit TLS on 465. |
| `SMTP_USERNAME` | | Login, and the From address fallback for a single mailbox. |
| `SMTP_PASSWORD` | | |
| `IMAP_HOST` | `SMTP_HOST` | Where replies and bounces are read (IMAP over TLS). |
| `IMAP_PORT` | `993` | |
| `IMAP_USERNAME` | `SMTP_USERNAME` | |
| `IMAP_PASSWORD` | `SMTP_PASSWORD` | |
| `MAILBOX_*` | | Any variable starting with `MAILBOX_` can hold a rotation mailbox's password, named by `password_env`, e.g. `MAILBOX_ALEX_PASSWORD`. |
| `PLACEMENT_*` | | App passwords of the placement test's seed inboxes and control sender, named by their `password_env`, e.g. `PLACEMENT_GMAIL_SEED`. |
| `INSTANTLY_API_KEY` | | Legacy Instantly provider. Requires Instantly's Growth plan or higher. |

### Email verification (add at least one)

| Variable | Notes |
|---|---|
| `REOON_API_KEY` | Main verifier. Free monthly allowance. Also used to size the inbox sweep to the credits you have left. |
| `ZEROBOUNCE_API_KEY` | Tried first for Google Workspace and Microsoft 365 domains. |
| `HUNTER_API_KEY` | Email-pattern lookup (domain search) and a fallback verifier. |
| `TREG_TOKEN` | Optional. treg.to prepaid balance; when set, the inbox sweep verifies through it before Reoon. Not in `.env.example`. |

### Discovery and search

| Variable | Notes |
|---|---|
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | DataForSEO Business Listings and SERP providers. The API password is generated in their dashboard; it is not your account password. |
| `DATAFORSEO_SANDBOX` | Any value routes DataForSEO calls to their free sandbox (dummy data, never charged). |
| `SERPER_API_KEY` | Serper discovery provider, and the first backend for Scout's web searches. Close to mandatory from a datacenter IP. |
| `TAVILY_API_KEY` | Optional second search backend for Scout, tried after Serper. Not in `.env.example`. |
| `SEMRUSH_API_KEY` | Semrush competitors discovery provider. Not in `.env.example`. |
| `GMAPS_SCRAPER_URL` | Base URL of a self-hosted google-maps-scraper for the `google_maps` provider. Default `http://127.0.0.1:8085`. |

### Other integrations

| Variable | Notes |
|---|---|
| `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` | Optional browser-automation prospecting. Automating LinkedIn violates its terms; use an account you can afford to lose. |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | Optional JS-rendered crawling during `mercury train` (token needs "Browser Rendering: Edit"). Without them the trainer uses a plain HTTP crawler. |
| `CLAUDE_CONFIG_DIR` | Where the Claude CLI keeps its credentials. Mercury reads the OAuth token from here for the quota gauge. Default `~/.claude`. |

### Runtime overrides

| Variable | Read by | Effect |
|---|---|---|
| `MERCURY_CONFIG` | everything | Explicit config file path (see above). |
| `MERCURY_DB_PATH` | the dashboard only | Point the dashboard at another SQLite file, e.g. the demo database. `mercury run` and the other CLI commands always use `data/mercury.db`. |
| `MERCURY_STATE_REPO` | `scripts/cloud_run.sh`, `scripts/local_dashboard.sh` | URL of the private state repository for scheduled cloud runs. `HARVEY_STATE_REPO` is still read when it is unset. See [Cloud runs](cloud.md). |

`bash scripts/check_env.sh` prints which of the cloud-run variables are set, without network calls or printing secrets.

The dashboard's Settings tab can write most credentials into `.env` for you. It shows only whether a secret is set, never its value.

## Customizing what Mercury says

Both directories are plain Markdown, read from disk each time they are used. Edits take effect on the next call; no restart needed.

### `prompts/`

| File | Used by |
|---|---|
| `system.md` | Shared system instructions. |
| `scout.md` | Scoring and personalizing found prospects. |
| `writer.md` | Writing sequences: structure, length, banned phrases, spam rules. |
| `handler.md` | Classifying and answering replies. |

Templates use `{{variable}}` placeholders. Mercury logs a warning if a placeholder is left unfilled.

### `skills/`

Skills are knowledge files injected into an agent's prompt. The mapping lives in `AGENT_SKILLS` in `mercury/brain.py`:

| Agent | Skills |
|---|---|
| Scout | `prospecting_tactics`, `lead_qualification`, `account_navigation`, `signal_playbook`, `product_knowledge` |
| Writer | `email_frameworks`, `signal_playbook`, `sales_methodology`, `offer_strategy`, `product_knowledge`, `competitive_intel` |
| Handler | `objection_handling`, `sales_methodology`, `offer_strategy`, `product_knowledge`, `competitive_intel` |
| Sender | `email_frameworks`, `product_knowledge` |
| LinkedIn | `linkedin_outreach`, `prospecting_tactics`, `product_knowledge` |

`product_knowledge.md` and `competitive_intel.md` are generated by `mercury train` and gitignored. When a prospect's [offer](#offers) has a `content.summary`, the Writer leaves `product_knowledge` out for that email: the offer brief is the only offer description it gets. To add a new skill, create the file and add its name to the map in `brain.py`. `skills/README.md` describes each built-in skill.

Prompts are instructions, not guarantees. The [pre-send gate](email-and-deliverability.md#the-pre-send-gate) enforces the hard rules (length, links, banned phrases, merge tags) in code no matter what a prompt says.
