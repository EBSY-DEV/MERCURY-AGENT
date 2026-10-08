# The pain library

A cold email is allowed to say one thing about the prospect's situation: a pain
a person has checked. Pains are governed the way [signals](prospecting.md) are.
Mercury proposes, you decide, and only confirmed pains are written from.

| Status | Meaning |
|---|---|
| `proposed` | Suggested by the trainer or added by hand. Never used. |
| `confirmed` | A person said yes. The Writer may use it. |
| `rejected` | A person said no. Never used, never proposed again, and a draft that raises it is stopped before sending. |

Only a person changes a status, and the change records who and when. The
database refuses a decision attributed to the trainer or the system.

## What a pain holds

`code` (stable, `PAIN_...`), `label`, `owner_words` (how the owner says it),
`scene` (one or two lines), `cost`, `market` (an `icp.markets` name, blank for
any), `sector` (blank for any), `signal_codes` (the signals that make it
applicable), `offer_key` (the offer that answers it), `evidence` (URLs or
notes) and `avoid_terms` (phrases that mark a draft as raising it).

## Commands

```bash
mercury pains                            # the library; --status, --market to filter
mercury pains show CODE
mercury pains --confirm CODE[,CODE...]   # or 'all' for every proposed pain
mercury pains --reject CODE --note "why"
mercury pains --reopen CODE              # back to proposed
mercury pains add --label "..." --words "..." --scene "..." --cost "..." \
    --sector SECTOR --market MARKET --offer OFFER_KEY --signal SIGNAL_CODE
mercury pains edit CODE --cost "..."     # editing never changes the status
```

`mercury train` adds the pains it finds as `proposed` and writes only the
confirmed ones into `skills/product_knowledge.md`.

## How re-training treats existing pains

A proposal is the same pain as one on file when it derives the same code (the
code hashes the normalised text), has the same normalised text, or shares most
of its content words (Jaccard 0.5, or 70% of the shorter statement). The
original wording is kept, so editing a label does not hide the match. A match
with a rejected pain drops the proposal; a match with a proposed or confirmed
pain leaves it untouched. A rewording too loose to match is still caught at
send time by the word guard below.

## Choosing the pain for an email

`select_pain_for_prospect` returns exactly one confirmed pain or none. A pain
is eligible when its offer is blank or the email's offer, its market is blank
or the prospect's, its sector is blank or fits the company's industry, and it
names no signals or the company carries at least one of them (confirmed
signals only). The most specific eligible pain wins: more matched signals, then
a named sector, market and offer, then the lowest code.

## Pains in the Writer

Every email gets exactly one confirmed pain when one fits, and none otherwise.
`Writer.pain_source` (default: the selection above, with the offer the prospect
was routed to) supplies it; set it to `None` and every prompt says no pain was
supplied.

- **Where it goes.** With an offer, the pain is in the offer brief
  ("the only pain you may name"). Without offers, once at least one pain is
  confirmed, it is its own block in the prompt. The block also carries the
  pain's code as an internal reference, so the recorded prompt says which
  pain it was written around.
- **Never-use list.** Rejected pains go into every first-email, sequence and
  rewrite prompt as "NEVER raise these", in the owner's words. A rejected pain
  written for another offer is left out of this offer's prompt, so one offer's
  content does not reach another's; the guard below still checks drafts
  against it.
- **A shared sequence.** Steps 2 and 3 are one template for the whole group, so
  the sequence carries a pain only when every prospect in the group was given
  the same one. Otherwise the template raises none, and each prospect's own
  first email still carries theirs. The template's pain code is stored on its
  steps (`EmailStep.pain_code`) and copied onto the staged outbox rows.
- **Rewrites.** A rewrite from the review desk selects again for that
  prospect, keeps the email's offer, and records the pain it used.
- **Recording.** `outbox.pain_code` is set for personalized first emails,
  staged sequence rows and rewrites. `GET /api/outbox` rows (and
  `GET /api/outbox/{id}`) carry `pain_code` and `pain`: `null`, or
  `{code, label, words, scene, cost, status}` read from the library as it is
  now (`status` is `missing` if the pain was deleted), next to `offer`.
- **Discarded drafts.** A first email, sequence or rewrite that raises a
  rejected pain is discarded before it is staged; the prospects are tried
  again on the next cycle. The send gate still checks everything that leaves.

## The guard

`find_rejected_pain_hits(text, rejected, allowed)` reports a draft that uses an
avoided phrase, or at least two distinctive words of a rejected pain (words of
four letters or more that no confirmed pain also uses). The sender runs it on
every cold sequence email just before sending, whatever wrote it (a first
email, a template follow-up, a rewrite, a manual edit); a hit fails the email
with `gate: rejected pain CODE`. It is a separate check from the case-study
scope: a draft that trips both lists both reasons, one each.

## Results per pain

An email staged for a pain records `outbox.pain_code`. `mercury pains` and
`GET /api/pains` show, per pain, the sequence emails sent, the people reached,
and how many of them replied (out-of-office notices excluded) or were
classified interested. A reply counts for every pain the person was sent
before it.

## API

`GET /api/pains[?status=&market=&offer_key=]`, `GET /api/pains/{code}`,
`POST /api/pains`, `POST /api/pains/{code}/save`, `POST /api/pains/status`.
