# FAQ

Short answers to the questions that come up most, with links to the detailed pages. If something here disagrees with the code, the code wins; please open an issue.

## What does it cost to run?

Mercury's model calls go through the `claude` CLI, so they count against the Claude Pro or Max plan the CLI is logged into, not a per-token bill. Mercury stops model work when either quota window reaches `usage.max_daily_claude_percent` (80% by default), leaving the rest for you.

Everything else is optional and mostly has free tiers:

| Item | Cost |
|---|---|
| Sending mailbox | Roughly the price of one Google Workspace seat on a secondary domain, plus the domain. |
| Email verification | Reoon, ZeroBounce and Hunter all have monthly free allowances. |
| Discovery | OpenStreetMap is free. DataForSEO, Serper and Semrush are paid, with free trial credit or queries. Every run shows an estimate first and has a hard spend cap. See [Discovery providers](prospecting.md#discovery-providers). |
| Search | Free scraping works poorly at volume; Serper gives 2,500 free queries. |

## Can it run without Claude Pro or Max?

It needs a working `claude` CLI. If you log the CLI in with an Anthropic API key instead of a subscription, calls should still work but are billed per token, and the quota gauge is unavailable, so Mercury falls back to counting its own calls (160 a day at the default 80%). The subscription is the tested path.

## Is it safe to leave running?

The guardrails, all on by default:

- Every outgoing email waits for your approval (`require_approval: true`).
- A deterministic [pre-send gate](email-and-deliverability.md#the-pre-send-gate) checks every email in code: recipient, length, links, leftover merge tags, banned phrases, no HTML.
- Only verified addresses are emailed. `guess` addresses never are.
- Daily send caps, per-mailbox warm-up caps, at most 8 sends per cycle, quiet hours.
- Stop-on-reply, opt-out handling that does not depend on the model, and escalation to you for angry or legal replies.
- A global bounce kill switch and per-mailbox health gates.
- Discovery estimates spend first and stops at a hard cap.

Two caveats to weigh honestly:

- Mercury runs locally as your user, and its model calls use `claude -p --dangerously-skip-permissions` so the CLI never stops to ask. Mercury's prompts ask only for text or JSON and all actions are taken in Python, but text scraped from prospects' websites does go into prompts, and with permissions skipped the CLI would not ask before using a tool. If that concerns you, run Mercury as a dedicated unprivileged user or in the provided Docker image.
- The dashboard has no login. Keep it on localhost or a private network ([details](dashboard.md#running-it-on-another-machine)).

## Why no open or click tracking?

Tracking pixels and rewritten links hurt deliverability, and Apple Mail Privacy Protection pre-fetches images so open rates are mostly noise anyway. Mercury sends plain text with at most one link and measures what matters: reply rate, positive reply rate and bounce rate. They are on the Today tab.

## Gmail or SMTP?

| | Gmail | SMTP + IMAP |
|---|---|---|
| Setup | Google Cloud OAuth client, then `mercury gmail auth` once in a browser | Environment variables only |
| Network | HTTPS only; works where mail ports are blocked (many cloud runners) | Needs outbound 587/465 and 993 |
| Several mailboxes with rotation and warm-up ramps | No, one mailbox | Yes, `channels.email.mailboxes` |
| Works with | Gmail / Google Workspace | Fastmail, AgentMail, a Workspace app password, self-hosted |

For a single mailbox on a laptop or server, either is fine. For several mailboxes, use SMTP. For an HTTPS-only environment, use Gmail. See [Providers](email-and-deliverability.md#providers).

## Why do my prospects stay `guess`?

Because nothing could confirm the address exists, and Mercury never sends to a guess. Usual causes:

- **No verifier key.** Add `REOON_API_KEY`, `ZEROBOUNCE_API_KEY` or `HUNTER_API_KEY`. Direct SMTP checks only work for small self-hosted mail servers, and outbound port 25 is usually blocked anyway.
- **Credits used up.** The inbox sweep pauses when no verifier has credits; the log says so.
- **The server answered "unknown".** Slow or greylisting servers do this. Mercury re-checks a few `guess` addresses on later cycles.

Catch-all domains come back `risky`, not `guess`. Those are sent only with `send_to_risky: true`. See [Address verification](email-and-deliverability.md#address-verification).

## Why isn't anything sending?

Check in this order:

1. `mercury sending`: is the kill switch on?
2. The log or Today: "compliance hold" means `compliance.postal_address` is empty.
3. `mercury outbox`: are emails waiting for approval?
4. `mercury mail test`: are the provider credentials and ports working?
5. Quiet hours: nothing sends between `quiet_hours.start` and `end`.
6. Caps: the Outbox tab shows each mailbox's cap today and what is left. A warming mailbox may be at 5 a day, or 0 if its `warmup_start` is in the future.
7. Mailboxes tab: is a mailbox paused by the health gate?

## How do I pause all sending?

```bash
mercury sending pause
```

Or press **Pause all sending** on the Outbox tab. Nothing leaves the outbox until you resume; drafts keep accumulating for review. `mercury sending resume` (or **Resume sending**) turns it back on and resets the bounce counter. Mercury pauses itself the same way when bounces pass `max_bounce_rate`.

## How do I resume a paused mailbox?

A mailbox paused by the health gate (more than 5% bounces over at least 20 sends in 7 days) or by hand stays paused until you press **Resume** in its drawer on the Mailboxes tab. Resuming restarts its 7-day health window. Clean up the cause first, usually unverified or catch-all addresses. There is no CLI command for this.

## How do I stop emailing someone?

Move their card to Meeting, Won or Lost on the Pipeline tab; their queued emails are cancelled. Rejecting an email in the Outbox also rejects the later steps of that sequence. A prospect who replies, opts out or bounces is stopped automatically.

## Where is my data?

On your machine, in the checkout:

| Path | Contents |
|---|---|
| `data/mercury.db` | Everything: companies, prospects, observations, outbox, conversations, usage. Any SQLite tool can open it. |
| `data/gmail_token.json` | Gmail refresh token. |
| `data/analytics.json` | The Analyst's latest report. |
| `data/mercury.log` | Output of processes started from the dashboard. |
| `.env` | Credentials. |
| `mercury.local.yaml` | Your configuration. |
| `skills/product_knowledge.md`, `skills/competitive_intel.md` | What the trainer learned. |

All of these are gitignored. Data leaves your machine only to the services you configured (Claude, your mail provider, verifiers, discovery providers, search). See [Backups](deployment.md#backups).

## Is this legal? (CAN-SPAM, GDPR)

This is general information, not legal advice; the rules depend on where you and your recipients are.

- **CAN-SPAM (US)** requires truthful headers and subject lines, a valid physical postal address, a working opt-out honoured promptly, and no further mail after an opt-out. Mercury adds the address and an opt-out line to every sequence email, refuses to send without the address, and suppresses opted-out prospects permanently. Accurate sender identity and honest subject lines are on you.
- **GDPR (EU/UK)** and similar laws treat a named person's work email as personal data. B2B cold outreach is commonly justified under "legitimate interest", which generally means targeting people whose role makes the message relevant, keeping data to what you need, telling people where you got their details if they ask, and honouring objections and deletion requests. Some countries (for example Germany) are stricter about unsolicited email. Mercury does not decide any of this for you.
- **Canada's CASL** and other regimes have their own consent rules.

If you are unsure, get advice for your situation before sending.

## Should I use LinkedIn?

It is optional and off unless `LINKEDIN_EMAIL` is set. Mercury logs in with Playwright and browses as that account, which violates LinkedIn's terms of service and can get the account restricted or banned. If you use it, use an account you can afford to lose and keep the default limits (`max_daily_connections: 20`, `max_daily_messages: 10`). Mercury prospects fine without it, through discovery, company websites and search.

## Can I change what the emails say?

Yes. Edit `prompts/writer.md` for structure, length and style rules, and the files in `skills/` for frameworks and product knowledge. Changes apply on the next call. For a single email, edit it in the Outbox or use Regenerate with an instruction. See [Customizing what Mercury says](configuration.md#customizing-what-mercury-says).

## Can I use Mercury just to build lists?

Yes. Leave approval on and never approve, or set `channels.email.enabled: false` (the Writer still drafts sequences, which uses Claude quota), then export: `mercury export` writes verified and risky prospects to `prospects.csv` in a format sequencers import directly. See [Exporting the list](prospecting.md#exporting-the-list).

## It used to be called Harvey. Do I need to do anything?

Pull, `pip uninstall -y harvey`, `pip install -e .`. Old files are renamed on first start and the `harvey` command still works as an alias. See [Upgrading from Harvey](deployment.md#upgrading-from-harvey).
