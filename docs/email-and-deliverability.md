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

1. If the global kill switch is on, nothing goes out.
2. If `compliance.postal_address` is empty, nothing goes out ("compliance hold").
3. Rows are ordered replies first, then follow-ups, then new first emails, so a backlog of openers never starves step 2.
4. A sequence row is cancelled if the prospect's status is `replied`, `opted_out`, `lost`, `meeting` or `closed`.
5. Step N never leaves before step N-1 was sent. If the earlier step was rejected, cancelled or failed, this one is cancelled too. A follow-up is rescheduled to the earlier step's actual send time plus its delay, so an opener approved a week late does not drag its follow-ups out right behind it.
6. A mailbox is chosen (see [Mailbox rotation](#mailbox-rotation)) and the [pre-send gate](#the-pre-send-gate) runs.
7. The email is sent with the [compliance footer](#compliance-footer-and-opt-outs) appended (not on replies). The prospect moves to `contacted`.

At most 8 emails leave per cycle, with 4 to 15 seconds of random delay between sends. `max_daily_sends` caps all native sends in a rolling 24-hour window. With `spread_sends: true`, the day's remaining cold budget is divided over the cycles left before quiet hours.

Temporary failures (timeouts, connection errors, 4xx deferrals, rate limiting) keep the row approved and retry up to 3 times, 30, 60 and 90 minutes later. Permanent errors mark it `failed`.

**Reviewing.** In the dashboard's Outbox tab you can, for each pending draft:

- **Edit** the subject and body. Pending and approved rows can be edited; an approved row stays approved. Approving saves unsaved edits first.
- **Regenerate** with an optional instruction ("shorter", "mention their reviews"). The new draft goes back to `pending_review`.
- **Approve** or **reject**. Rejecting a sequence step also rejects every later step of that sequence for that prospect.

`mercury outbox` does the same from the terminal (see [Getting started](getting-started.md#8-review-the-outbox)). The Calendar tab can reschedule a pending or approved email to a future time.

**Follow-up auto-approval.** With `auto_approve_followups: true`, a pending follow-up is approved as soon as the step before it is approved or sent. That happens when you approve in the dashboard and again on every cycle, so a sequence you signed off on is not stuck waiting for two more clicks. Replies always need their own approval while `require_approval` is on.

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
| Out-of-office / auto-reply | Ignored. The sequence continues. |
| `unsubscribe` | Prospect becomes `opted_out`, conversation closed, every queued email cancelled. No reply is sent. |
| `escalate` (legal threats, harassment complaints) | Conversation flagged `needs_human`. No auto-reply. |
| `not_interested` | Prospect becomes `lost`, conversation closed. |
| `interested`, `question`, `objection`, `wrong_person` | A reply is drafted and queued in the outbox (approval applies). |

Opt-outs are matched by keyword before any model call: English phrases such as "unsubscribe", "remove me", "stop emailing", Spanish forms of "baja", and a one-word reply like "stop". Mercury first cuts quoted text and its own footer out of the message, so its own opt-out line in a quoted reply is not mistaken for a request.

**Stop-on-reply.** Any human reply (anything other than an auto-responder) moves the prospect to `replied` and cancels every queued email for them. A prospect you already moved to Meeting or Won is not pulled back to `replied`.

Conversations move through stages `initial_outreach`, `engaged`, `qualifying`, `presenting`, `negotiating`, `closing`, and end at `closed_won` or `closed_lost`. Mercury advances them from reply intent; you move deals on the Pipeline tab.

## Bounces and the kill switch

When a bounce arrives, Mercury matches it to the sent email (via In-Reply-To, the DSN's original Message-ID, the failed recipient, or an address in the bounce text), then:

- marks the address `invalid`,
- cancels every queued email for that prospect,
- logs a `bounce` event attributed to the sending mailbox (which feeds the [health gates](#health-gates)),
- increments the global bounce counter.

**Global kill switch.** Once at least 10 emails have been sent, if bounces counted since the last resume exceed `max_bounce_rate` (default 5%) of all emails sent, Mercury pauses all sending. Nothing leaves the outbox until you resume. Set `max_bounce_rate: 0` to disable it (not recommended).

```bash
mercury sending            # status
mercury sending pause      # manual stop
mercury sending resume     # resume and reset the bounce counter
```

The Outbox tab has the same Pause/Resume button, and Today shows "Sending is paused" at the top of Needs you. Fix the cause (usually unverified addresses) before resuming.

The kill switch is global. The health gates are per mailbox. Both can be in effect.

## Compliance footer and opt-outs

CAN-SPAM requires a valid physical postal address and a clear opt-out mechanism in commercial email. Mercury appends a footer to every sequence email at send time, outside the draft so the writer can never drop or rewrite it:

```
...email body...

Acme Roofing Software · 123 Main St, Suite 4, Denver, CO 80202
Not relevant? Reply "unsubscribe" and you won't hear from me again.
```

The company comes from `persona.company`, the address from `compliance.postal_address`, and the opt-out line from `compliance.opt_out_line_en` or `opt_out_line_es`, chosen by whether the body reads as Spanish or English. Replies to people who wrote to you do not get the footer.

While `compliance.postal_address` is empty, the native sender sends nothing and logs a compliance hold on every cycle.

Opting out is by reply. Opted-out prospects are never emailed again. For regional rules beyond this, see the [FAQ](faq.md#is-this-legal-can-spam-gdpr).

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
