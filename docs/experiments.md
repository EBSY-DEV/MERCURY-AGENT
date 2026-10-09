# A/B experiments

An experiment compares two versions of your outreach, arm A and arm B, on one variable at a time: the opening angle, the subject line, or the writing persona. Each prospect is assigned to one arm before anything is written for them and stays there for their whole sequence. Results count people, not emails, and wait until every counted prospect has had the same time to answer.

The code is in `mercury/experiments.py` (assignment, exposure, outcomes, results), `mercury/experiment_stats.py` (intervals) and `mercury/control/experiments.py` (commands). The dashboard API is `mercury/experiments_api.py`; the CLI is `mercury experiments`. The dashboard's Experiments tab (`mercury/web/experiments.js`) only lays out what the API below computes: the table, the controls, the sample progress, the metric strip, the weekly bars, the B minus A interval and the New experiment drawer. `results` also carries `by_week` (per week of first email, each arm's contacted, mature, pending, positive and replied counts, and whether the week is still open) and `series` (cumulative daily counts among prospects whose window had closed) for those charts, and `GET /api/experiments` carries the personas and ready-made eligible groups the form chooses from.

## Defining an experiment

| Field | Meaning |
|---|---|
| `name`, `hypothesis` | Labels for people. Editable at any time. |
| `variable` | `opening_angle`, `subject_line` or `persona`. |
| `arms` | Exactly two, A and B. Each has a `name`, an `instruction` (added to the writer prompt for the whole sequence) and an optional `persona_id` (empty: the default voice). |
| `cohort` | Who is eligible. Optional filters, combined with AND: `industries`, `import_batch_ids`, `require_signals`, `exclude_signals`, `min_score`, `prospect_ids`. Empty: every new prospect Mercury drafts for. |
| `allocation_a` | Percent of eligible prospects that go to A (1 to 99, default 50). |
| `enroll_until`, `max_enrolled` | When and how many to enroll. A date means the end of that day, UTC. |
| `response_window_days` | How long each prospect has to answer before they count. |
| `min_per_arm` | Mature prospects each arm needs before any comparison is shown as a result. |
| `min_duration_days` | The shortest run before a decision. |
| `primary_metric` | `positive_reply_rate` (default) or `any_reply_rate`. |

One variable at a time is enforced. In an opening-angle or subject-line experiment both arms use the same persona and their instructions differ. In a persona experiment the personas differ and the instructions are identical.

## Lifecycle and controls

`draft` → `running` ⇄ `paused` → `completed`, plus a separate mail hold.

| Control | Effect on new enrollment | Effect on queued mail |
|---|---|---|
| **Start** | Opens enrollment. The revision is frozen: each arm is pinned to an exact persona version (the default voice is resolved now, so changing the default later does not move an arm). | None. |
| **Pause enrollment** | No new prospects are enrolled. Prospects already assigned are still written in their arm. | None: approved mail keeps sending. |
| **Resume** | Enrollment opens again. | None. |
| **Hold unsent mail** | None. | Every unsent sequence email of the experiment is refused at the send claim (`experiment_hold`). Nothing is unapproved, rejected or cancelled, and drafts can still be reviewed. |
| **Release** | None. | Held mail goes out on its schedule. A follow-up still waits its delay after the previous step really went out. |
| **Complete** (needs confirmation) | Ends enrollment for good. | None: enrolled sequences finish unless you hold them. Results keep maturing. |

A hold never lets anything skip a check: review and approval snapshots, exclusions and opt-outs, out-of-office pauses, company holds and limits, the pre-send gate and the kill switch all apply to experiment mail exactly as to any other mail. Replies Mercury writes to people who answered are not experiment mail and are never held.

Nothing is ever changed automatically: no arm is disabled, no copy is rewritten, and no winner is picked for you.

### Edits and revisions

Name, hypothesis, enrollment end and cap change in place. Any other change is substantive. On a draft it rewrites the draft. On a started experiment it creates the next revision, frozen at once: new enrollments go to it, everyone already enrolled stays in the revision and arm they were assigned, and results are reported per revision (the current one by default, `?revision=N` for an earlier one). Frozen revisions and their arms cannot be updated; the database refuses it.

## Assignment

When the Writer is about to draft for a batch of prospects, the experiments seam (`writer_groups`) enrolls the eligible ones and stores the assignment before any model call. The arm is drawn from a SHA-256 hash of the experiment id, the revision number and the prospect id, so it does not depend on the order prospects arrive in, their score, or which cycle picks them up, and a retry or restart gives the same arm. The split approaches `allocation_a` as enrollment grows.

- A prospect has at most one assignment per experiment (primary key), and enters at most one experiment ever, so no one carries one variant into another comparison. When two running experiments match the same prospect, the one started first takes them.
- Assignments are permanent (the database refuses updates) and are honoured for the prospect's whole sequence, whatever the experiment's state later.
- Only native providers (Gmail, SMTP) enroll. The legacy Instantly path sends its own sequences and is never enrolled.

## Exposure

Each arm's share of a batch becomes its own campaign, written with the arm's persona version and instruction. That persona snapshot, including an `experiment` marker, is stored with every generation (`email_generations.persona_json`), and a database trigger stamps each sequence email written from it with `experiment_id`, `experiment_revision_id` and `experiment_arm_id` when it is queued. Follow-ups come from the same arm-written sequence, and a regenerated draft reuses its generation's snapshot, so the whole sequence stays in one arm. The generation also records the exact prompt and persona version, so sent content can be traced to its arm (`GET /api/experiments/{id}/assignments`).

Mail written before experiments existed, and mail for prospects outside one, keeps empty ids: explicitly unassigned, never given an invented arm.

## Outcomes

Every reply attributed to an arm has one outcome label:

`positive_interested`, `positive_soft`, `positive_referral`, `neutral_question`, `not_now`, `not_interested`, `hostile`, `unsubscribe`, `ooo`, `bounce`, `other`.

The label comes from, in order of precedence:

1. **A person** (`POST /api/experiments/outcomes/{inbound_id}`, or `mercury experiments label ID LABEL`). Final.
2. **The outcome classifier**: one Claude call per human reply of an enrolled prospect, run alongside the heartbeat while within budget (`experiments.classify_outcomes`). It returns a label and a confidence from 0 to 1. A reply is never classified twice; if the classifier gives nothing usable, the mapping below is stored instead so a bad reply cannot cost a call every cycle.
3. **The handler's intent**, mapped with no model call:

| Handler intent | Outcome | Confidence | Why |
|---|---|---|---|
| bounce message | `bounce` | 1.0 | Settled by the delivery report. |
| out-of-office (headers or classifier) | `ooo` | 1.0 | Automatic, excluded from replies. |
| other automatic reply (receipt, acknowledgement) | `other` | 1.0 | Automatic, excluded from replies. |
| `unsubscribe` | `unsubscribe` | 1.0 | Keyword rules run before any model. |
| `interested` | `positive_interested` | 0.8 | The same intent the trend chart counts as positive. |
| `question` | `neutral_question` | 0.8 | |
| `not_interested` | `not_interested` | 0.8 | |
| `escalate` | `hostile` | 0.6 | Also where a failed intent classifier lands. |
| `objection` | `not_now` | 0.5 | Covers timing, price and competition. |
| `wrong_person` | `positive_referral` | 0.5 | Only a referral when they name someone. |
| anything else | `other` | 0.0 | |

A label below `experiments.confidence_threshold` (default 0.7) is **uncertain**: a prospect whose only positive label is uncertain is shown in the `uncertain` count and never counted as positive until the classifier or a person settles it.

## Metrics

Each prospect counts once per arm. All rates are among **mature** prospects, within their window, divided by mature.

| Metric | Definition |
|---|---|
| enrolled | Prospects assigned to the arm. |
| awaiting_first_touch | Enrolled, first email not sent yet (draft, in review, scheduled, held or failed). |
| contacted | Enrolled prospects with a successfully sent first touch: the earliest sent email of the arm's sequence, normally step 1. Drafts, rejected, cancelled and failed sends never count. |
| mature | Contacted prospects whose response window (first touch + `response_window_days`) has ended. The primary denominator. |
| pending | Contacted, still inside the window. Shown apart, never in a rate. |
| sent (raw) | Sequence emails of the arm that were sent, every step. Follow-ups are emails, not samples. |
| replied | Prospects with at least one human reply in the window. |
| positive | Prospects with at least one reply labelled `positive_*` at or above the threshold, in the window. At most one per prospect, however many times they write. |
| uncertain | Prospects whose only positive label is below the threshold. |
| bounced | Prospects with a bounce attributed to the arm in the window. Mailbox-full and similar temporary bounces are left out, as in the trend chart. |
| opted_out | Prospects with a reply labelled `unsubscribe` in the window. |
| late_replies | Prospects whose first human reply came after their window. Visible, never in the primary rate. |
| raw.replied / positive / bounced / opted_out | The same counts over every contacted prospect at any time, including late replies and pending prospects. |

**Excluded everywhere:** drafts and failed sends (never contacted), duplicate copies of a message delivered to another inbox, automatic replies (vacation notices, receipts), messages from Mercury's own mailboxes, messages still waiting to be handled (counted in `data_quality.unhandled_messages`).

**Attribution.** A reply belongs to the arm of the sent email it answers: In-Reply-To or References, followed back through Mercury's own replies when the prospect answers one of those. With no such header, it belongs to the most recent sequence email sent to that prospect from the mailbox that received it, sent before the reply. Never to whichever campaign owns the prospect now: a reply to an older campaign's email is not counted, even for a prospect who is now enrolled. Messages that cannot be tied to the revision are counted in `data_quality.unattributed_messages`.

## Statistics and decisions

- Each arm's rate carries a **Wilson score interval** (95%).
- The comparison is **B minus A** with **Newcombe's hybrid score interval** (Newcombe 1998, method 10, built from the two Wilson intervals), 95%. It behaves well at zero counts and small samples, where the usual Wald interval does not. The tests check it against the worked examples in that paper.
- The result is **Not enough data yet** until both arms have `min_per_arm` mature prospects and the experiment has run `min_duration_days`. The decision line says how far along it is and the earliest date a decision is possible: exact once enough first touches are sent (the Nth first touch plus the window), otherwise estimated from the send rate so far (shown with `~`), and none when enrollment has ended short of the minimum.
- With enough data: **B ahead** or **A ahead** when the interval excludes zero, otherwise **No clear difference**.
- **Health first.** Over mature prospects of both arms (once there are `health_min_mature`), an any-reply rate below `low_reply_rate`, or a bounce rate above `high_bounce_rate`, raises a warning that points to the deliverability checks (Mailboxes tab, `mercury mail placement`), and the result reads **Check deliverability first** instead of naming an arm. Mail that is not reaching the inbox says nothing about the copy.

## Configuration

```yaml
experiments:
  classify_outcomes: true       # one Claude call per human reply of an enrolled prospect
  confidence_threshold: 0.7     # below this a label is "uncertain"
  response_window_days: 14      # defaults offered for a new experiment
  min_per_arm: 50
  min_duration_days: 14
  health_min_mature: 30         # health checks start at this many mature prospects
  low_reply_rate: 0.01
  high_bounce_rate: 0.05
  max_classifications_per_cycle: 20
```

At low volume, 50 per arm only detects large differences: with rates near 5%, the interval for the difference is roughly plus or minus 9 points. Raise `min_per_arm` if you need to see smaller effects, and expect it to take longer.

## API

Reads return JSON as is. Commands return `{"success": true, ...}`; a refused one returns `{"success": false, "message", "code", ...}` with 400 (`invalid`, `ambiguous`), 404 (`not_found`) or 409 (`not_editable`, `stale_version`, `confirmation_required`). An `Idempotency-Key` header makes a retried command return the first answer. Every command is in the audit log. `{ref}` is an id, an exact name or a unique id prefix.

| Method and path | Purpose |
|---|---|
| `GET /api/experiments` | The table: one row per experiment, plus `options` for the form (variables, metrics, outcome labels, cohort filters, defaults, control effects). |
| `GET /api/experiments/{ref}?revision=N` | `{"experiment": definition and controls, "results": results}`. |
| `GET /api/experiments/{ref}/results?revision=N` | Results only. |
| `GET /api/experiments/{ref}/assignments?arm=A&limit=&offset=` | Who is in which arm, with every email stamped for them, its generation and persona version. |
| `GET /api/experiments/{ref}/preview?prospect=ID` | Eligible now, each sampled prospect's real arm, each arm's persona and prompt block (with `prospect`, the full first-email prompt). No model call, nobody enrolled. |
| `POST /api/experiments/preview` | The same for an unsaved definition (the New experiment form): expected split instead of real arms. |
| `POST /api/experiments` | Create a draft. |
| `PATCH /api/experiments/{ref}` | Edit. `expected_version` refuses a stale save. `arms` may name one arm: `[{"key": "B", "instruction": "..."}]`. The answer says `new_revision`. |
| `POST /api/experiments/{ref}/start` · `/pause` · `/resume` · `/release` | Controls. |
| `POST /api/experiments/{ref}/hold` | `{"reason": "..."}` optional. |
| `POST /api/experiments/{ref}/complete` | `{"confirm": true}` required. |
| `POST /api/experiments/outcomes/{inbound_id}` | `{"label": "positive_soft"}`: a person's label. |

Create body (synthetic):

```json
{
  "name": "Opening angle test",
  "hypothesis": "A question opener earns more positive replies.",
  "variable": "opening_angle",
  "arms": [
    {"name": "variant A", "instruction": "Open with a question about their week."},
    {"name": "variant B", "instruction": "Open with an observation from their site."}
  ],
  "cohort": {"industries": ["segment_a"]},
  "allocation_a": 50,
  "enroll_until": "2026-11-30",
  "response_window_days": 14,
  "min_per_arm": 50,
  "primary_metric": "positive_reply_rate"
}
```

A table row (`GET /api/experiments`):

```json
{"id": "ae34fbafa8cd", "name": "Opening angle test", "status": "running",
 "status_label": "Running", "hold_mail": false, "revision": 1,
 "variable": "opening_angle", "variable_label": "Opening angle",
 "primary_metric": "positive_reply_rate", "primary_metric_label": "Positive-reply rate",
 "enrolled": {"A": 40, "B": 38, "total": 78}, "mature": {"A": 40, "B": 38},
 "rate": {"A": 0.1, "B": 0.236842},
 "result": {"code": "insufficient_data", "label": "Not enough data yet"},
 "warnings": 0, "created_at": "2026-10-08T21:22:53", "started_at": "2026-10-08T21:22:53",
 "completed_at": null}
```

`result.code` is one of `not_started`, `insufficient_data`, `check_deliverability`, `no_clear_difference`, `a_ahead`, `b_ahead`.

The detail's `experiment` (abridged):

```json
{"id": "ae34fbafa8cd", "status": "running", "status_label": "Running", "hold_mail": false,
 "hold_reason": "", "version": 2, "revision": 1, "frozen": true,
 "variable_label": "Opening angle", "allocation_a": 50, "allocation_b": 50,
 "cohort_description": "every new prospect Mercury drafts for",
 "setup_line": "Opening angle · 50/50 split · 14-day response window · 50 mature per arm · Positive-reply rate",
 "arms": [{"id": "0b0f6001fff9", "arm_key": "A", "name": "variant A",
           "instruction": "Open with a question about their week.", "persona_id": "",
           "persona_version_id": "workspace-v1", "persona_name": "Default voice"}, "..."],
 "revisions": [{"id": "c1c47fe2bfab", "number": 1, "frozen_at": "2026-10-08T21:22:53"}],
 "controls": {"can_edit": true, "edit_creates_revision": true, "can_start": false,
              "can_pause": true, "can_resume": false, "can_complete": true,
              "can_hold": true, "can_release": false, "effects": {"pause": "...", "hold": "...", "complete": "..."}},
 "queued": {"pending_review": 0, "approved": 12, "blocked": 0, "sending": 0, "unsent": 12}}
```

The detail's `results` (abridged; arm B has the same shape as A):

```json
{"revision": 1, "as_of": "2026-10-08T21:22:54", "window_days": 14, "min_per_arm": 50,
 "confidence_threshold": 0.7, "primary_metric_label": "Positive-reply rate",
 "arms": [{"key": "A", "name": "variant A", "enrolled": 40, "awaiting_first_touch": 0,
           "contacted": 40, "mature": 40, "pending": 0,
           "primary": {"metric": "positive_reply_rate", "count": 4, "rate": 0.1,
                       "interval": {"low": 0.03958, "high": 0.230518}},
           "positive": {"count": 4, "rate": 0.1, "interval": {"low": 0.03958, "high": 0.230518}},
           "any_reply": {"count": 7, "rate": 0.175}, "bounce": {"count": 0, "rate": 0.0},
           "opt_out": {"count": 0, "rate": 0.0}, "uncertain": 0, "late_replies": 0,
           "labels": {"positive_interested": 4, "neutral_question": 3},
           "raw": {"sent": 40, "contacted": 40, "replied": 7, "positive": 4, "bounced": 0, "opted_out": 0}},
          "..."],
 "comparison": {"metric_label": "Positive-reply rate", "difference": 0.136842,
                "interval": {"low": -0.031869, "high": 0.303413}, "direction": "B minus A",
                "method": "Newcombe hybrid score interval (Wilson), 95%"},
 "decision": {"code": "insufficient_data", "label": "Not enough data yet",
              "line": "Insufficient data: 38 of 50 mature per arm, earliest decision ~2026-11-01",
              "sufficient": false, "mature_min": 38, "mature_needed": 50, "duration_met": true,
              "earliest_decision_at": "2026-11-01T08:45:00", "earliest_estimated": true,
              "reasons": ["38 of 50 mature per arm"], "recommends_winner": false,
              "automatic_changes": false},
 "health": {"warnings": [{"code": "low_reply_rate", "message": "...",
                          "action": {"label": "Check deliverability", "tab": "mailboxes",
                                     "cli": "mercury mail placement"}}]},
 "data_quality": {"unhandled_messages": 0, "automatic_excluded": 1, "own_mail_excluded": 0,
                  "duplicates_excluded": 1, "arm_mismatches": 0, "unattributed_messages": 0},
 "definitions": {"contacted": "...", "mature": "...", "...": "..."}}
```

Rates are fractions (0.1 is 10%); a rate with no mature prospects is `null`.

## CLI

```bash
mercury experiments                                  # the table
mercury experiments create --file test.yaml          # or flags: --name --variable --a-instruction ...
mercury experiments preview --file test.yaml         # before saving
mercury experiments preview "Opening angle test"     # a saved one: real arms
mercury experiments start "Opening angle test"
mercury experiments show "Opening angle test" [--revision 1]
mercury experiments pause | resume | release NAME
mercury experiments hold NAME --reason "checking copy"
mercury experiments complete NAME --confirm
mercury experiments edit NAME --b-instruction "..."  # a new revision once started
mercury experiments assignments NAME --arm B
mercury experiments label INBOUND_ID positive_soft
```

Every command takes `--json`.
