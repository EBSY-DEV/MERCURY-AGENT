# Email and deliverability

This page covers everything between "a prospect has a verified address" and "the email landed and the reply was handled": the mail providers, the approval outbox, the pre-send gate, rotating across several mailboxes, the warm-up ramp and its health gates, reply handling, bounces and the kill switch, the legal footer, DNS authentication, and how addresses get verified in the first place. Mercury's sending behaviour is conservative by default. Read this before you loosen any of it.

## Providers

Set `channels.email.provider` in your config and the matching credentials in `.env` (see [Configuration](configuration.md#email-provider-pick-one)).

| Provider | Sends via | Replies read via | Approval outbox | Rotation and warm-up ramp | Notes |
|---|---|---|---|---|---|
| `gmail` | Gmail REST API over HTTPS | Gmail API | yes | single mailbox; health gates apply | Recommended. Works from networks that block mail ports. One-time `mercury gmail auth`; token in `data/gmail_token.json`. |
| `smtp` | SMTP (587 STARTTLS or 465 TLS) | IMAP over TLS (993) | yes | yes, via `channels.email.mailboxes` | Any mailbox: Fastmail, AgentMail, a Workspace app password, self-hosted. Pure environment variables. |
| `instantly` | Instantly API | Instantly API | no | Instantly's own | Legacy. Campaigns and leads are pushed to Instantly and activated there; Mercury's replies go out immediately through Instantly with no approval step. Needs Instantly's Growth plan. |

Test the connection on the machine that will actually send:

```bash
mercury mail test      # gmail or smtp; with rotation, every mailbox and its cap today
mercury gmail test     # gmail only
```

A timeout from `mercury mail test` means blocked ports, not bad credentials. Many cloud runners allow only HTTPS; use `gmail` there. See [Cloud runs](cloud.md#choosing-a-provider-check-the-egress-first).

The rest of this page describes the native providers (`gmail`, `smtp`).

## The approval outbox

Every native email, whether a first touch, a follow-up or a reply, becomes a row in the `outbox` table and moves through these statuses:

```
pending_review ──approve──> approved ──(due, passes checks)──> sent
      │                         │
      └──reject──> rejected     ├──> cancelled  (prospect replied, bounced, opted out,
                                │                 moved to Meeting/Won/Lost, or an
                                │                 earlier step died)
                                └──> failed     (pre-send gate, permanent SMTP error)
```

With `require_approval: false`, rows start as `approved`.

**Staging.** When the Writer produces a draft sequence (an opener, a follow-up about 3 days later, and a short break-up about 4 days after that), the Sender renders the merge variables for each prospect and stages one row per step. Each row gets a scheduled `send_at`: now for step 1, then the cumulative delays. The prospect moves to `queued`. Only prospects with a sendable address are staged (`verified`, plus `risky` when `send_to_risky` is on).

**Draining.** Each heartbeat the Sender picks up due, approved rows:

1. If you paused sending, or a health hold is on (the bounce kill switch), nothing goes out. See [Pauses and holds](#pauses-and-holds).
2. If `compliance.postal_address` is empty, nothing goes out ("compliance hold").
3. Rows are ordered replies first, then follow-ups, then new first emails, so a backlog of openers never starves step 2.
4. A sequence row is cancelled if the prospect's status is `replied`, `opted_out`, `lost`, `meeting` or `closed`.
5. Step N never leaves before step N-1 was sent. If the earlier step was rejected, cancelled or failed, this one is cancelled too. A follow-up is rescheduled to the earlier step's actual send time plus its delay, so an opener approved a week late does not drag its follow-ups out right behind it.
6. A mailbox is chosen (see [Mailbox rotation](#mailbox-rotation)) and the [pre-send gate](#the-pre-send-gate) runs.
7. The pause and hold check runs again, then the row is claimed (`sending`) and sent with the [compliance footer](#compliance-footer-and-opt-outs) appended (not on replies). The prospect moves to `contacted`.

At most 8 emails leave per cycle, with 4 to 15 seconds of random delay between sends. `max_daily_sends` caps all native sends in a rolling 24-hour window. With `spread_sends: true`, the day's remaining cold budget is divided over the cycles left before quiet hours.

**Demo gate.** Some offers promise something already built for that one business: a phone line that answers in its name, a draft homepage with its photos. Sent before the demo exists, that email makes a false claim. Give such an offer `requires_demo: true` under `offers:` in mercury.yaml, and every sequence email that carries its key (`offer_key`, on the outbox row or its campaign) waits until the prospect's demo is marked ready. The row stays approved and shows "Waiting for demo" in the Outbox; nothing is cancelled. The gate fails closed: an offer key missing from `offers:`, a row with no contact, or an error while checking holds the email. Rows with no offer key are not gated.

```bash
mercury demos                                   # who is waiting, and every demo
mercury demos ready EMAIL --url https://...     # or --recording PATH, --agent-id ID, --by NAME
mercury demos request EMAIL --offer voice       # register one to build
mercury demos retire EMAIL                      # its emails wait for a new demo
```

The Outbox tab has the same: a **Waiting for a demo** list and a **Mark ready** drawer to attach the link or recording. A demo that is ready is retired `demos.retire_after_days` (default 14) after the last email to a contact who never replied, once nothing is queued for them. A break-up email that says the demo "stays ready" should quote that number. Instantly deploys a whole sequence at once, so there a demo offer's campaign stays in draft until every contact in it has a ready demo. Building the demo itself is not Mercury's job; this is the bookkeeping and the gate.

Temporary failures (timeouts, connection errors, 4xx deferrals, rate limiting) keep the row approved and retry up to 3 times, 30, 60 and 90 minutes later. Permanent errors mark it `failed`.

**Reviewing.** In the dashboard's Outbox tab you can, for each pending draft:

- **Edit** the subject and body. Pending and approved rows can be edited; an edited approved row goes back to `pending_review`. Approving saves unsaved edits first.
- **Regenerate** with an optional instruction ("shorter", "mention their reviews"). The new draft goes back to `pending_review`.
- **Approve** or **reject**. Rejecting a sequence step also rejects every later step of that sequence for that prospect.

`mercury outbox` does the same from the terminal (see [Getting started](getting-started.md#8-review-the-outbox)). The Calendar tab can reschedule a pending or approved email to a future time; a rescheduled approved email needs approval again.

**Revisions.** Every outbox row has a revision. Changing what a reviewer reads (the text, the recipient, the sending mailbox, a send time you pick, a regenerated draft) makes a new revision and sends an approved email back to review. Approve, reject and every edit name the revision they were decided on, and fail with `stale_revision` if the email changed since, so two people reviewing at once can't overwrite each other or approve text they haven't seen. An approval records the revision and a hash of the content it covered; the sender re-checks both when it claims the email, so nothing goes out on an approval for an earlier version. **Approve all** approves exactly the list on screen, at the revisions shown. The sender's own timing (follow-up spacing, retries, out-of-office resumes) does not change the revision.

**Audit.** Every review and config command is recorded in the `audit_log` table: who (operator and client), which email or file, the revisions before and after, the action, and how it ended, failures included. Secrets are redacted from these records and from error messages. A client may send an `Idempotency-Key` header (dashboard) or request id (MCP); repeating a command with the same key returns the first answer without running it again.

**Follow-up auto-approval.** With `auto_approve_followups: true`, a pending follow-up is approved as soon as the step before it is approved or sent. That happens when you approve in the dashboard and again on every cycle, so a sequence you signed off on is not stuck waiting for two more clicks. Replies always need their own approval while `require_approval` is on.

**Follow-ups in the same thread.** With `thread_followups: true` (the default, also a switch on the dashboard's Settings tab), follow-ups go out as replies to the email before them. When a step is sent, its Message-ID and thread are copied onto the later steps still queued: `In-Reply-To` names the previous email, `References` lists the whole chain (step 1, then step 2 for step 3), Gmail gets the same `threadId`, and the subject on the wire is `Re: ` plus the first email's subject. The Writer's own follow-up subject stays in the row; the Outbox and `mercury outbox` show the `Re:` subject the recipient will see. Editing or regenerating a follow-up keeps its thread. If the first email failed or was cancelled, the follow-ups are cancelled with it. Set `thread_followups: false` to send every step as a new email with its own subject.

**Writer backlog.** Mercury stops drafting new sequences once the queued first emails (pending or approved) add up to seven days of sending capacity. Drafts written weeks ahead go stale.

## The pre-send gate

Prompts can be ignored, so the last check before any email leaves is deterministic code (`mercury/gate.py`). A failure marks the row `failed` with the reason.

| Check | Rule |
|---|---|
| Recipient | Valid address, matching the prospect record. |
| Deliverability (sequence emails) | `email_status` must be `verified`, or `risky` with `send_to_risky`. Replies are exempt: the person wrote to you. |
| Subject | Present, at most 90 characters. |
| Body | Present, at most 220 words. |
| Merge tags | No unrendered `{{...}}` or `{...}` left. |
| Banned phrases | Spam triggers and AI tells such as "act now", "click here", "i hope this finds you well", "game-changer", "as an ai". |
| Links | At most 1 URL. |
| HTML | None. Mercury sends plain text only. |
| Case studies | No configured case-study name or alias outside the email's offer and the prospect's market and segment (see [`offers`](configuration.md#offers)). If the scope cannot be worked out, every configured case study is blocked. |

## Mailbox rotation

One mailbox carrying all your cold volume is the fastest way to burn a domain. With the `smtp` provider you can list several mailboxes, usually one or two per secondary domain, each with its own cap and optional warm-up ramp.

Use **Settings → Sending inboxes → Add inbox** to add an address, password/app password, daily cap, and warm-up start date. Expand **Server settings** to override the shared SMTP/IMAP hosts, ports, or login. New inboxes default to warming up from today; omit the password if you need to finish setup later.

**Edit inbox** changes the password or sending settings. A blank password field keeps the saved credential. **Test connection** checks SMTP and IMAP login without sending an email. Turn off **Use this inbox for new outreach** to finish existing threads and continue reading replies without starting new ones. Addresses cannot be renamed because existing threads reference them.

Passwords stay in the private `.env` file and are never returned by the settings API. When starting from the tracked `mercury.yaml` template, the dashboard saves inbox configuration to `mercury.local.yaml`; existing private or explicitly selected configurations are updated in place. The first additional inbox preserves any existing single SMTP sender. Adding SMTP inboxes while using another email provider requires an explicit provider-switch confirmation.

The dashboard reflects changes immediately. Restart a running `mercury run` agent to apply the new configuration and credentials. All inboxes still share `max_daily_sends`.

Manual configuration remains supported:

```yaml
channels:
  email:
    provider: smtp
    max_daily_sends: 60            # still caps the total across all mailboxes
    warmup_initial_cap: 5
    warmup_weekly_increase: 5
    mailboxes:
      - email: "jordan@acme-mail.com"       # already warm
        password_env: "MAILBOX_JORDAN_PASSWORD"
        daily_cap: 15
      - email: "alex@getacme.com"           # warming since Sept 21
        name: "Alex Rivera"
        password_env: "MAILBOX_ALEX_PASSWORD"
        daily_cap: 15
        warmup_start: "2026-09-21"
      - email: "sam@tryacme.com"            # different provider, starts next week
        password_env: "MAILBOX_SAM_PASSWORD"
        smtp_host: "smtp.fastmail.com"
        imap_host: "imap.fastmail.com"
        daily_cap: 25
        warmup_start: "2026-10-13"
```

```bash
# .env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
IMAP_HOST=imap.gmail.com
MAILBOX_JORDAN_PASSWORD=...
MAILBOX_ALEX_PASSWORD=...
MAILBOX_SAM_PASSWORD=...
```

`password_env` must name a `MAILBOX_*` variable, `SMTP_PASSWORD` or `IMAP_PASSWORD`. Anything else resolves to an empty password, so a typo cannot hand an API key to an SMTP server. A mailbox without a password is skipped for sending and polling.

For an existing mailbox with separate IMAP credentials, `imap_username` and `imap_password_env` can override its SMTP login and password. The dashboard preserves those overrides when converting a single inbox to rotation.

**Picking a mailbox for a new thread.** Among mailboxes that have credentials, accept new threads and have cap left today, Mercury picks the one with the fewest sends this cycle, then the largest share of its daily cap still unused, then list order. A warming mailbox at cap 5 and a warm one at cap 30 both drain at their own pace instead of the warm one doing all the work.

**Thread pinning.** Only step 1 rotates. Follow-ups go out from the mailbox the opener used, and replies go out from the inbox the prospect's message arrived in. A prospect never gets "Re:" mail from a stranger, and their answers land in the inbox that holds the conversation.

**Hold, never re-route.** If a thread's mailbox has been removed from the config, or has no credentials, its emails are held, not moved to another address: nobody reads the old inbox, so a reply or opt-out there would be lost. Put the mailbox back (with `enabled: false` if you want no new threads from it) or reject the held emails. To retire a mailbox, set `enabled: false`, let its threads finish, then remove it.

**Capacity.** Today's cold capacity is the sum of each mailbox's cap, further limited by `max_daily_sends`. Replies are not limited by mailbox caps, only by `max_daily_sends`, so a paused or capped mailbox still answers people who wrote back.

Emails sent before rotation was configured belong to the mailbox matching `persona.email` (or `SMTP_USERNAME`); if neither is in the list, the first mailbox takes them.

## Warm-up ramp

A mailbox with a `warmup_start` gets a cap that grows weekly:

```
cap(day) = min(daily_cap, warmup_initial_cap + floor(days_since_start / 7) * warmup_weekly_increase)
```

Before `warmup_start` the cap is 0. Without a `warmup_start` the cap is `daily_cap` from day one. Days are counted in `usage.quiet_hours.timezone`.

With the defaults (`warmup_initial_cap: 5`, `warmup_weekly_increase: 5`) and `daily_cap: 30`:

| Days since start | Cap |
|---|---|
| 0-6 | 5 |
| 7-13 | 10 |
| 14-20 | 15 |
| 21-27 | 20 |
| 28-34 | 25 |
| 35 and later | 30 |

The mailbox reaches full volume on day 35. The Mailboxes tab plots each mailbox's planned cap per day against what it actually sent.

The ramp is stored in `channels.email.mailboxes`; edit caps and start dates from an inbox's settings on the **Mailboxes** tab, **Settings → Sending inboxes**, or your private configuration file. Restart a running Mercury agent after saving. The Mailboxes tab also handles pause/resume, monitoring, the checklist, and notes.

Mercury does not run a "warm-up network" that trades fake emails with other inboxes. It does the parts that move reputation for a new mailbox: authenticate the domain, start small, ramp slowly, and stop when bounces climb.

## Health gates

Before each drain, Mercury computes per-mailbox health over the last 7 days (or since the mailbox was last resumed, if more recent). Sends are that mailbox's outreach sends. Bounces are attributed to the mailbox that sent the bounced email.

| Condition | Result |
|---|---|
| Fewer than 20 sends | OK ("not enough sends yet") |
| Bounce rate above 5% | **Pause.** The mailbox's cold cap becomes 0 and it stays paused until you resume it on the Mailboxes tab. |
| Bounce rate above 3%, up to 5% | **Hold.** Today's cap is yesterday's ramp cap, so a warming mailbox stops climbing. |
| Otherwise | OK |

A gate can only lower a cap. Openers and follow-ups from a paused mailbox are held, not cancelled; replies still go out. Resuming restarts the 7-day window, so the old spike cannot immediately re-pause a fixed mailbox. You can also pause a mailbox by hand from the Mailboxes tab.

With `gmail`, or a single SMTP mailbox, the same gates apply to that one mailbox. Hold has no visible effect there because there is no ramp to freeze.

## Replies

On native providers the Handler polls every configured inbox on every cycle (skipped while over the Claude budget, because classification uses Claude). Each new message is deduplicated, split into bounces and human replies, and classified:

| Intent | What happens |
|---|---|
| Out-of-office / vacation reply | Recorded as an `auto_reply` event, not a reply: no conversation, no reply metrics. The contact's remaining cold steps are **paused** until they are back (see below). |
| Other automatic mail (ticket acknowledgements, read receipts, list mail) | Recorded as an `auto_reply` event and otherwise ignored. The sequence continues. |
| `unsubscribe` | Prospect becomes `opted_out`, conversation closed, every queued email cancelled. No reply is sent. |
| `escalate` (legal threats, harassment complaints) | Conversation flagged `needs_human`. No auto-reply. |
| `not_interested` | Prospect becomes `lost`, conversation closed. |
| `interested`, `question`, `objection`, `wrong_person` | A reply is drafted and queued in the outbox (approval applies). |

### Out-of-office pauses

A vacation reply is recognised from its headers (`Auto-Submitted`, `X-Autoreply`, ...) or subject plus words that say the person is away, without a model call; one the reply classifier labels out-of-office is handled the same way. An `Auto-Submitted` header alone never pauses anything: receipts and "we received your message" acknowledgements carry it too.

Mercury reads the return date deterministically, in English and Spanish ("until October 20", "back next Monday", "hasta el 20 de octubre", "regreso el lunes", "20/10/2026"), against the reply's own timestamp in `usage.quiet_hours.timezone`. The sequence resumes when quiet hours end on a business day. Weekend dates move to Monday; `channels.email.ooo_resume_buffer_days` adds the configured number of business days. This also applies when you set a return date by hand, while Mercury keeps the stated return date separately from the first send time. "Until" names the return day; "through" and date ranges name the final day away. When the date is missing, could be read two ways (05/10), does not exist, is already past or more than a year away, the contact stays paused with **needs a return date** and never resumes on their own.

While paused, every queued follow-up for them waits, approved or not, and the check is repeated in the transaction that claims each email. On the return day the next unsent step becomes due and later steps move with it, so their gaps stay what the sequence says; caps, pacing, quiet hours, exclusions and approvals all still apply. A human reply, an opt-out, a bounce, an exclusion or closing the contact ends the pause for good.

The Outbox's **Away** section lists paused contacts with their return date; open one to correct the date or resume now. `mercury paused` does the same from the terminal (`list`, `set-date PAUSE_ID YYYY-MM-DD`, `resume PAUSE_ID`). Every change is in the activity log. Vacation replies never count as replies in the metrics.

On the legacy Instantly path Mercury cannot hold a sequence Instantly is sending, so it logs that the pause is unavailable instead of claiming one.

Opt-outs are matched by keyword before any model call: English phrases such as "unsubscribe", "remove me", "stop emailing", Spanish forms of "baja", and a one-word reply like "stop". Mercury first cuts quoted text and its own footer out of the message, so its own opt-out line in a quoted reply is not mistaken for a request.

**Stop-on-reply.** Any human reply (anything other than an auto-responder) moves the prospect to `replied` and cancels every queued email for them. A prospect you already moved to Meeting or Won is not pulled back to `replied`.

Conversations move through stages `initial_outreach`, `engaged`, `qualifying`, `presenting`, `negotiating`, `closing`, and end at `closed_won` or `closed_lost`. Mercury advances them from reply intent; you move deals on the Pipeline tab.

## Bounces and the kill switch

When a bounce arrives, Mercury matches it to the sent email (via In-Reply-To, the DSN's original Message-ID, the failed recipient, or an address in the bounce text), then:

- reads the enhanced status code (RFC 3463, for example `5.1.1`) from the bounce's `Status:` field or text, and sorts the bounce into a bucket,
- logs a `bounce` event with `dsn_code` and `bucket`, attributed to the sending mailbox (which feeds the [health gates](#health-gates)),
- increments the global bounce counters,
- then acts on the bucket:

| Bucket | Codes | What Mercury does |
|---|---|---|
| `LIST` | 5.1.1, 5.1.10, 5.1.0, 5.2.1, 5.4.1, 5.5.0 | A bad address. Marks it `invalid` and cancels the prospect's queued emails. No reputation action. |
| `SENDER` | 5.7.1, 5.7.0, 5.7.23, 5.7.26, 5.7.509, 5.7.520, and any other 5.7.x | Authentication or policy. Pauses that mailbox (the pause reason points at the DNS checks on the Mailboxes tab). The prospect is left alone. |
| `BURNED` | 5.7.606 to 5.7.614 | The domain is blocked on reputation. Pauses every mailbox on the domain and marks it `CANCEL_CANDIDATE` on the Mailboxes tab. Resuming a mailbox clears the flag. |
| `THROTTLE` | any 4.x.x | The server asked us to slow down. Halves that mailbox's daily cap for 7 days. |
| `NOISE` | 5.2.2, 5.3.4, 5.7.133 | Mailbox full, message too big, group that rejects outsiders. Logged, ignored by every rate and the address is kept. |
| `UNKNOWN` | no code, or a code not above | Treated as a bad address (the pre-classification behaviour) and counted in the total rate. |

If a `SENDER` or `BURNED` bounce cannot be pinned to a mailbox Mercury knows, or the pause cannot be saved, Mercury trips the global kill switch instead. A bounce it cannot process at all also trips it.

The Mailboxes tab shows the bucket breakdown for each inbox over its health window.

**Global kill switch.** Mercury puts all sending on hold, and nothing leaves the outbox until you clear the hold, when either:

- `SENDER` plus `BURNED` bounces are more than 20% of the classified bounces (`LIST`, `SENDER`, `BURNED` and `THROTTLE`), once at least 30 are classified. `LIST` bounces alone never trip this.
- Bounces counted since the hold was last cleared (not `NOISE`) exceed `max_bounce_rate` (default 2%) of all emails sent, once at least 50 have been sent.

Set `max_bounce_rate: 0` to disable the rate check (not recommended); the sender/burned share check stays on.

The kill switch is global. The health gates are per mailbox. Both can be in effect.

### Pauses and holds

Your pause and a health hold are kept apart, because they are lifted differently:

| What | Set by | Lifted by |
|---|---|---|
| **Your pause** | `mercury sending pause`, **Pause all sending** on the Outbox tab, or MCP | `mercury sending resume` / **Resume sending**. Lifts your pause and nothing else. |
| **Bounce hold** (the kill switch) | The bounce monitor, above | `mercury sending clear-hold` / **Clear bounce hold**, after you fix the cause. This is the only thing that restarts the bounce count. It needs the admin permission. |
| **Compliance hold** | `compliance.postal_address` is empty | Setting the address. |
| **Inbox paused or on hold** | You (inbox drawer), the warm-up health gate, or a sender/reputation bounce | **Resume** in that inbox's drawer on the Mailboxes tab. Other inboxes keep sending. |

```bash
mercury sending              # your pause, every hold, and what is in flight
mercury sending pause        # stop new sends
mercury sending resume       # lift your pause; prints any hold still in force
mercury sending clear-hold   # lift the bounce hold and reset the bounce counters
```

Resume never resets the bounce counters, never lifts a hold, and never bypasses suppression, caps, pacing, the warm-up ramp or an inbox's gates. If a hold is still in force, resume says so and sending stays blocked. Pausing or resuming twice changes nothing.

**What can be in flight.** The sender checks for a pause or hold right before it claims each email, so a pause stops the next email, not the next cycle. An email already claimed (outbox status `sending`) is mid-way through its provider call. That call is not cut off: an SMTP send stopped halfway cannot tell whether the message left, and retrying it could send twice. It finishes and is recorded as sent, failed or queued for retry like any other. `mercury sending` and the Outbox banner list these rows. A row still `sending` after 30 minutes was interrupted (the agent was killed mid-send); the next cycle puts it back to `approved`, where your pause or a hold keeps it.

**Stopping the agent.** **Stop** on the Controls tab asks the agent to finish the step it is on: the email in flight finishes, nothing new is claimed, and the pacing delay is skipped. If it has not exited within a few seconds, Controls shows **Stopping** until it has. Pressing Stop again only checks progress. `POST /api/mercury/stop?force=true` kills it outright.

The Outbox tab lists your pause and each hold with its reason, and Today shows "Sending is paused" or "Sending is on hold" at the top of Needs you. `mercury sending`, the Outbox tab and Today all read the same status.

## Deliverability health

Zero replies on its own says nothing: it can be the copy, the list, or mail landing in spam, and on a small sample it is not even a signal. `mercury health` (and the **Deliverability** card on Today, plus a verdict per domain on the Mailboxes tab) answers the narrower question Mercury can answer honestly: is there enough evidence to keep a sending domain, or to cancel it?

Per sending domain (every mailbox on it added together) it shows outreach sends, replies and bounces over the last 7, 14 and 30 days, how long the domain has been sending, and a verdict over the last 30 days. The ladder, checked in this order:

| Verdict | When |
|---|---|
| Cancel candidate | Bounce composition says the domain is burned (a 5.7.6xx reputation block). Reads the `BURNED` bucket the Handler stores on each bounce (see [bounces and the kill switch](#bounces-and-the-kill-switch)); bounces logged before classification never trigger it. |
| Too young | Under 30 days of sending, or nothing sent yet. |
| Keep | 200 or more sends and replies at 1% or above. |
| Cancel candidate | 0 replies on 150 or more sends, or replies under 1% after 200 sends. |
| Not enough data | Everything else: the reply rate needs 200 sends and the bounce rate 50. |

A domain is never "keep" without evidence, and a cancel candidate is only a candidate: run a [placement test](#placement-test) before retiring it. Today lists cancel candidates under Needs you.

Definitions match the trends chart: a send is an outreach email (Mercury's own replies don't count), a reply is a human reply (out-of-office excluded, one per prospect per day), and a bounce is attributed to the mailbox that sent the bounced email. Events no send can be traced to are reported as unattributed and charged to no domain. **Sending age** counts from the domain's first outreach send or the earliest `warmup_start` of its mailboxes, whichever is older; it is not the registration date, so a domain warmed elsewhere reads young until Mercury has 30 days of its history.

A high bounce rate is shown as a flag (over `max_bounce_rate`, after 50 sends) but does not change the verdict on its own: a bounce could be a bad address (a list problem) as easily as a blocked sender (a domain problem). Only a `BURNED` bounce makes a domain a cancel candidate. The per-mailbox [health gates](#health-gates) already pause an inbox past 5%.

```bash
mercury health          # the table, the reasons, the thresholds and the last placement test
mercury health --json
```

## Placement test

`mercury mail placement` sends the campaign's real email 1 (the newest step-one email in the outbox, with the legal footer exactly as a prospect gets it) from every configured mailbox to a few seed inboxes you own, plus the same text from a control sender with a known-good reputation, usually a personal Gmail. Then it reads the seeds over IMAP and records where each copy landed: Primary, Promotions or another Gmail tab, Inbox (non-Gmail), Spam, or not found. Configure the seeds and the control under [`channels.email.placement`](configuration.md#channelsemail).

| Result | Reads as |
|---|---|
| Your mailboxes mostly in spam, the control in the inbox | Domain or reputation problem. |
| Both in spam | Copy problem. Rewrite it and test again. |
| Your mailboxes in spam, no control configured (or not found) | Assume the copy first. |
| Your mailboxes mostly in the inbox | Placement is fine. Low replies point at the copy or the list. |

"Mostly" means at least half of the copies found; copies not found yet are left out. Each domain is judged on its own against the same control, so one burned domain can't hide behind a healthy one.

The test never touches the prospect outbox: nothing it sends counts toward a daily cap, a warm-up ramp, the trends or the health verdict. Results are stored one row per sender and seed in the `placement_tests` table, with the last run's summary line in `settings`. The last result shows on Today and next to the DNS checklist in each domain's drawer on the Mailboxes tab.

```bash
mercury mail placement --dry-run            # who would send to whom; sends nothing
mercury mail placement                      # send, wait up to placement.wait_seconds, read the seeds
mercury mail placement --mailbox x.com      # only this domain (or address); repeatable
mercury mail placement show [RUN]           # the last (or a given) result
mercury mail placement check [RUN]          # read the seeds again for late mail
mercury mail placement mark RUN --seed you@outlook.com --sender a@x.com --folder spam
```

Seeds are read with an app password (Gmail and Yahoo need 2-step verification turned on to create one). Outlook.com has moved IMAP to OAuth sign-in and may refuse app passwords; if it does, leave its `password_env` empty, look at the inbox yourself and record the folder with `mark`. Gmail tabs (Primary, Promotions) are read with Gmail's own search; other providers only distinguish inbox from spam.

## Compliance footer and opt-outs

CAN-SPAM requires a valid physical postal address and a clear opt-out mechanism in commercial email. Mercury appends a footer to every sequence email at send time, outside the draft so the writer can never drop or rewrite it:

```
...email body...

Acme Roofing Software · 123 Main St, Suite 4, Denver, CO 80202
Not relevant? Reply "unsubscribe" and you won't hear from me again.
```

The company comes from `persona.company`, the address from `compliance.postal_address`, and the opt-out line from `compliance.opt_out_line_en` or `opt_out_line_es`, chosen by whether the body reads as Spanish or English. Replies to people who wrote to you do not get the footer.

While `compliance.postal_address` is empty, the native sender sends nothing and logs a compliance hold on every cycle.

Opting out is by reply. An opt-out is recorded as an exclusion on the address itself (see below), so deleting the contact, importing it again or rediscovering it never makes it emailable. For regional rules beyond this, see the [FAQ](faq.md#is-this-legal-can-spam-gdpr).

## Exclusions and company limits

**Exclusions** stop every Mercury email, replies included, to an exact address or a domain. A domain rule matches that domain only; tick *include subdomains* (`--subdomains`) to also match `eu.acme.com` and the like. Each rule records its source:

| Source | Added by | Lifting it |
|---|---|---|
| `opt_out` | The reply handler, when someone asks to stop | Needs an explicit confirmation and a note. Never lifted by an import or by removing another rule. |
| `bounce` | The bounce handler | Any time, with a note. |
| `manual` | You, in the dashboard (**Exclusions**) or `mercury exclusions add` | Any time. |
| `import` | A CSV of exclusions | Any time. An import only ever adds rules. |

Rules for the same address from different sources are separate, so removing a manual rule leaves an opt-out in place. Rules are never deleted: lifting one stamps who did it and why, and every change is kept in an append-only history.

Exclusions are checked when contacts are imported, when a campaign is staged, and again inside the same database transaction that claims an email for sending. Email already queued when a rule is added (approved or not) is moved to **blocked** at once. Lifting the rule does not send it: send it back to review from the Exclusions or Outbox tab and approve it again.

**Company limits** count against a *known company*: the contact's `company_id`, or the company whose domain is the contact's email domain. Shared providers such as gmail.com or outlook.com never make two people colleagues, and a contact with no known company shows "Company unknown" and gets no company limit.

- `max_new_contacts_per_company_per_day` caps first emails per company in a rolling 24 hours.
- `max_active_contacts_per_company` caps unfinished cold sequences per company (first email sent, later steps still queued, paused or blocked).
- `pause_company_on_reply` holds cold mail to the rest of a company once one person writes back. Replies to the people who wrote keep going. Resume it on the Exclusions tab or with `mercury holds release`. You can also pause a company yourself from its contact list.

Limits are enforced when an email is claimed for sending. A claimed first email counts until the provider answers, so two sender processes cannot both take the last slot. A send that fails frees its slot, and a person contacted twice in a day counts once. Held email keeps its status: when the hold lifts or a slot frees up, approved email goes out and drafts still wait for review. The Outbox shows why each held email is waiting.

The legacy Instantly provider sends sequences itself, so Mercury applies exclusions only when it adds leads and cannot enforce company limits or reply holds there.

## DNS: SPF, DKIM, DMARC, MX

Authentication decides whether mail lands in the inbox. The Mailboxes tab checks each sending domain (2-second timeout per lookup, cached for 10 minutes):

| Check | Pass when | Typical fix |
|---|---|---|
| MX | The domain has MX records, so replies and bounces reach you. | Add the MX records your provider gives you. |
| SPF | Exactly one `v=spf1` TXT record, not ending in `+all` or `?all`. | `v=spf1 include:_spf.google.com ~all` (Google) or `v=spf1 include:spf.protection.outlook.com -all` (Microsoft). Merge duplicates into one record. |
| DKIM | A key is found at a common selector: `google`, `default`, `selector1`, `selector2`, `k1`, `s1`, `mail`, `dkim`. | Google Workspace: Admin, Apps, Gmail, Authenticate email. Microsoft 365: Defender, Email authentication, DKIM. A key at an uncommon selector shows as "unknown", not failed. |
| DMARC | A `_dmarc` record with `p=quarantine` or `p=reject`. `p=none` is a warning. | Start with `v=DMARC1; p=none; rua=mailto:you@yourdomain`, then move to `p=quarantine` once SPF and DKIM pass for a couple of weeks. |

The checklist's "SPF, DKIM and DMARC all pass" item ticks itself when MX, SPF, DKIM and DMARC all pass.

Other habits the Warm-up checklist walks you through: send from a secondary domain and point its website at your real one, use a real name and plain signature, send a few personal emails a day alongside cold ones, seed-test placement in Gmail and Outlook, add the domain to Google Postmaster Tools and keep the spam rate under 0.3%, and add a second mailbox rather than pushing one past about 50 a day.

## Address verification

Raw SMTP probing does not work against Google Workspace and Microsoft 365, which host most business mail: they accept every recipient from an unknown IP and bounce later, and outbound port 25 is usually blocked on home and cloud networks anyway. So Mercury learns each company's address pattern and verifies one candidate:

1. Classify the domain by its MX host (Google, Microsoft, a security gateway, or other).
2. Learn the pattern (`first.last@`, `flast@`, ...) from cache, then Hunter's domain search, then addresses found on the site, else default to `first.last`.
3. Verify that single candidate with the best channel for the domain type. For Google, Microsoft and gateway domains: ZeroBounce, then Reoon, then Hunter. For small or self-hosted servers: Reoon, then a direct SMTP check (with a catch-all test), then Hunter.

Addresses published on a company's own site (the inbox sweep) are verified through treg.to (when `TREG_TOKEN` is set), then Reoon, then Hunter. The sweep pauses when no verifier has credits left, rather than creating unverifiable prospects.

Every address carries one status:

| Status | Meaning | Sent? |
|---|---|---|
| `verified` | A verifier confirmed the mailbox exists. | Yes. |
| `risky` | The domain is catch-all: it accepts everything, so a wrong address still bounces later. | Only with `send_to_risky: true`. |
| `guess` | Nobody could tell. | **Never.** Not drafted, not staged, blocked by the gate. |
| `invalid` | Rejected by a verifier, or it bounced. | Never. |

Without any verifier key, most addresses end up `guess` and Mercury writes nothing. Mercury periodically re-checks a few `guess` addresses, since a slow or greylisting mail server often answers "unknown" the first time.

Mercury does not use open or click tracking; every email is plain text. Measure with reply rate and bounce rate instead (see [Today](dashboard.md#today)).
