"""Deterministic rules for a draft: word count, greeting, names, review claims.

The prompts ask for these things; this module checks them in code, so the
number the Writer is told and the number that is enforced are the same one
(``word_limits``), and a draft that breaks a rule is stopped or flagged before
it can be staged. Everything here is pure: no database, no model, no config
file, so the state layer, the Writer, the Sender and the Outbox review service
can all use it.

Word counting rule (``count_words``): the whole email body as it is staged,
greeting and sign-off included, never the subject and never the legal footer
the Sender appends at send time. A word is a whitespace-separated token that
holds at least one letter or digit, so a lone dash, bullet or ampersand does
not count and "follow-up" or "don't" are one word each. A merge variable such
as ``{{first_name}}`` counts as one word, even with spaces inside the braces.
"""

from __future__ import annotations

import json
import re

# Per sequence step, counted over the whole body (greeting and sign-off
# included). Overridable in mercury.yaml under ``writer.word_limits``.
DEFAULT_WORD_LIMITS: dict[int, int] = {1: 90, 2: 80, 3: 50}

FLAG_OVER_LIMIT = "over_word_limit"
FLAG_GENERIC_GREETING = "generic_greeting"
FLAG_LABELS = {
    FLAG_OVER_LIMIT: "Over the word limit",
    FLAG_GENERIC_GREETING: "Generic greeting",
}


# ── Word counting and limits ──

_MERGE_TAG = re.compile(r"\{\{[^{}]*\}\}")


def count_words(text: str) -> int:
    """Words in an email body; see the module docstring for the rule."""
    text = _MERGE_TAG.sub(" tag ", text or "")
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


def word_limits(config) -> dict[int, int]:
    """The word limit of every step: the defaults, then ``writer.word_limits``."""
    configured = getattr(getattr(config, "writer", None), "word_limits", None) or {}
    return {**DEFAULT_WORD_LIMITS, **{int(k): int(v) for k, v in configured.items()}}


def word_limit(config, step: int) -> int:
    """The limit for one step. A step past the configured ones takes the last."""
    limits = word_limits(config)
    step = int(step or 1)
    return limits.get(step) or limits[max(limits)]


# ── Flags on a staged draft ──

def encode_flags(flags) -> str:
    """The stored form: '' for none, else a JSON list of codes."""
    codes = sorted({str(f) for f in flags or [] if f})
    return json.dumps(codes) if codes else ""


def decode_flags(raw) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def flag_details(flags) -> list[dict]:
    return [{"code": code, "label": FLAG_LABELS.get(code, code.replace("_", " ").capitalize())}
            for code in flags or []]


def draft_flags(body: str, limit: int = 0, *, check_greeting: bool = False,
                business_names=()) -> list[str]:
    """The flags a draft carries. ``limit`` 0 means no word limit applies
    (a reply). The greeting check is for drafts whose contact has no name to
    greet by, or any draft the Writer wants checked."""
    flags = []
    if limit and count_words(body) > limit:
        flags.append(FLAG_OVER_LIMIT)
    if check_greeting and has_generic_greeting(body, business_names):
        flags.append(FLAG_GENERIC_GREETING)
    return flags


def recheck_flags(body: str, limit: int, previous=()) -> list[str]:
    """Flags for an edited body. The word limit is always re-measured. A
    generic-greeting flag is re-measured too, but a clean edit never adds
    one the draft did not have."""
    return draft_flags(body, limit, check_greeting=FLAG_GENERIC_GREETING in (previous or ()))


# ── Greetings ──

_OPENER = (r"(?:hi|hello|hey|hola|dear|greetings|good\s+(?:morning|afternoon|evening)"
           r"|buenos\s+d[ií]as|buenas\s+(?:tardes|noches|d[ií]as)|buenas|saludos|estimad[oa]s?)")
_WHOEVER = (r"(?:there|all|everyone|everybody|folks|friends|sir|madam|sir\s*(?:or|/)\s*madam"
            r"|team|equipo|todos|amigos|gente)")
_TRAIL = r"[\s,.!:;\-–—]*"
_BARE = re.compile(rf"^{_OPENER}{_TRAIL}$", re.IGNORECASE)
_TARGETED = re.compile(rf"^{_OPENER}[\s,]+{_WHOEVER}{_TRAIL}$", re.IGNORECASE)
_TEAM_OF = re.compile(rf"^{_OPENER}[\s,]+(?:[\w'’.&-]+\s+){{0,4}}(?:team|equipo(?:\s+de\s+[^,\n]+)?){_TRAIL}$", re.IGNORECASE)
_CONCERN = re.compile(rf"^to\s+whom\s+it\s+may\s+concern{_TRAIL}$", re.IGNORECASE)
# "Hi there, I noticed ..." on one line: greeting, a comma, then content.
_INLINE = re.compile(
    rf"^{_OPENER}(?:[\s,]+(?:{_WHOEVER}(?:\s+de\s+[^,.!?\n]+)?|\{{\{{[^{{}}]*\}}\}}))?\s*[,!:]\s*(?P<rest>\S.*)$",
    re.IGNORECASE | re.DOTALL)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def is_generic_greeting_line(line: str, business_names=()) -> bool:
    """A greeting that could be sent to anyone: "Hi there,", "Hello team,",
    "Hola, equipo de X," or one addressed to the business by name."""
    text = (line or "").strip()
    if not text or len(text.split()) > 9:
        return False
    if any(p.match(text) for p in (_BARE, _TARGETED, _TEAM_OF, _CONCERN)):
        return True
    names = {_norm(n) for n in business_names or () if n}
    if names:
        match = re.match(rf"^{_OPENER}[\s,]+(?P<who>[^\n]+?){_TRAIL}$", text, re.IGNORECASE)
        if match and _norm(match.group("who")) in names:
            return True
    return False


def _first_line(body: str) -> tuple[str, str]:
    stripped = (body or "").lstrip()
    head, _, rest = stripped.partition("\n")
    return head, rest


def has_generic_greeting(body: str, business_names=()) -> bool:
    head, _rest = _first_line(body)
    if is_generic_greeting_line(head, business_names):
        return True
    return bool(_INLINE.match(head.strip()))


def strip_generic_greeting(body: str, business_names=()) -> tuple[str, str]:
    """Remove a generic greeting from the start of a body. Returns the new
    body and the greeting removed ('' when there was none). A body that would
    be left empty is returned unchanged."""
    head, rest = _first_line(body)
    removed, new = "", body
    if is_generic_greeting_line(head, business_names):
        removed, new = head.strip(), rest.lstrip("\n").lstrip()
    else:
        inline = _INLINE.match(head.strip())
        if inline and has_generic_greeting(body, business_names):
            removed = head.strip()[:inline.start("rest")].strip()
            tail = inline.group("rest")
            new = (tail[:1].upper() + tail[1:] + ("\n" + rest if rest else "")).strip()
    if not new.strip():
        return body, ""
    return new, removed


_TEMPLATE_GREETING = re.compile(
    rf"^{_OPENER}(?:[\s,]+[^\n,!:]{{1,40}})?\s*[,!:]\s*(?:\n+|$)", re.IGNORECASE)


def drop_greeting(body: str) -> str:
    """Remove a greeting line from a sequence template ("Hi {{first_name}},")
    for a contact who has no name to greet by. Only a line that is just a
    greeting goes; a sentence that starts with one is left alone."""
    head, rest = _first_line(body)
    if _TEMPLATE_GREETING.match(head.strip() + "\n") and len(head.split()) <= 7:
        trimmed = rest.lstrip("\n").lstrip()
        return trimmed if trimmed else body
    inline = _INLINE.match(head.strip())
    if inline and has_generic_greeting(body):
        return strip_generic_greeting(body)[0]
    return body


# ── Business names ──

def apply_short_name(subject: str, body: str, full: str, short: str,
                     variants=()) -> tuple[str, str]:
    """Hold a draft to the naming rule: the full legal name never appears in
    the subject and at most once in the body; every other mention becomes the
    short name. ``variants`` are other spellings of the full name (the name
    without its location tail, for example) that are treated the same way."""
    if not full or not short or short.strip().lower() == full.strip().lower():
        return subject, body
    names = sorted({n.strip() for n in (full, *variants) if n and n.strip()
                    and n.strip().lower() != short.strip().lower()}, key=len, reverse=True)
    if not names:
        return subject, body
    pattern = re.compile("|".join(re.escape(n) for n in names), re.IGNORECASE)
    new_subject = pattern.sub(short, subject or "")
    seen = 0

    def keep_first(match):
        nonlocal seen
        seen += 1
        return match.group(0) if seen == 1 else short

    return new_subject, pattern.sub(keep_first, body or "")


# ── Review counts and ratings ──

_REVIEW_CLAIM = re.compile(
    r"""(?ix)
    \b\d[\d,.]*\+?\s*(?:[\w'-]+\s+){0,2}(?:reviews?|ratings?|rese[ñn]as?|opiniones|valoraciones|calificaciones)\b
  | \b(?:reviews?|ratings?|rese[ñn]as?)\b[^.;\n]{0,25}\b\d[\d.,]*\b
  | \b\d(?:[.,]\d)?\s*(?:/\s*5|out\s+of\s+5|stars?|estrellas?)\b
  | \b\d(?:[.,]\d)?-star\b
  | \b(?:rated|rating\s+of|calificad[oa]\s+con)\s+\d
  | \bstar\s+rating\b | \breview\s+count\b
    """)
REVIEW_SIGNAL_CODES = frozenset({"REVIEW_COUNT", "REVIEW_RATING"})


def mentions_review_claim(text: str) -> bool:
    return bool(_REVIEW_CLAIM.search(text or ""))


def strip_review_claims(text: str) -> str:
    """Drop the sentences of a note that quote a review count or a rating.
    They stay in the stored note (scoring still reads them); they just never
    reach the Writer's facts."""
    if not text:
        return text or ""
    parts = re.split(r"(?<=[.;!?])\s+|\n+", text)
    return " ".join(p.strip() for p in parts if p.strip() and not _REVIEW_CLAIM.search(p)).strip()
