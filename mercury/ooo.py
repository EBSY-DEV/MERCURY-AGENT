"""Out-of-office replies: tell a vacation reply from other machine mail, and
read the return date out of it.

Everything here is deterministic and offline. No model call is made: a date
that the rules below cannot read with confidence is never guessed, the pause
it creates waits for a person to set the date ("needs review").

Two questions, two functions:

  * ``classify_automatic`` -- is this inbound message a machine, and if so is
    it a vacation reply, a read/delivery receipt, or a generic acknowledgement
    ("we received your message")? Only a vacation reply pauses a sequence.
    An ``Auto-Submitted`` header alone never does.
  * ``parse_return_date`` -- the day the person is back, resolved against the
    message's own timestamp in the configured timezone. English and Spanish:
    absolute dates ("October 20", "20 de octubre de 2026", "2026-10-20"),
    numeric dates that read one way only ("10/20", "20/10/2026"), weekdays
    ("back next Monday", "regreso el lunes"), "tomorrow" / "mañana",
    "next week" / "la próxima semana", and a bare day ("until the 20th",
    "hasta el 20").

Mercury resumes on the return day at ``RESUME_HOUR`` local time, never at
midnight. Quiet hours, caps and pacing still apply after that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import pytz

# Local hour on the return day at which a paused sequence becomes eligible.
RESUME_HOUR = 9
# A return date further out than this is more likely a misread than a
# sabbatical; it goes to review instead of silently parking the contact.
MAX_AWAY_DAYS = 370
# A month and day with no year that fell this recently before the message is
# a past date (flagged), not next year's.
RECENT_PAST_DAYS = 180

KINDS = ("out_of_office", "receipt", "acknowledgement")

# Pipeline statuses that take a contact out of their sequence for good (the
# sender's stop-on-reply set). A vacation reply from them pauses nothing, and
# an active pause on them ends instead of resuming.
SEQUENCE_OVER = frozenset({"replied", "opted_out", "lost", "meeting", "closed"})

# Plain-language reasons a pause needs a person, shown in the dashboard.
REVIEW_REASONS = {
    "no_date": "The reply gives no return date.",
    "ambiguous": "The date can be read two ways, like 05/10.",
    "invalid": "The date in the reply does not exist.",
    "past": "The return date is before the reply was sent.",
    "unclear": "A date is mentioned, but not as the day they are back.",
    "conflicting": "The reply gives more than one return date.",
    "too_far": "The return date is more than a year away.",
}


# ── Telling machines apart ──

# Accent folding that keeps every index in place, so a span found in the
# folded text is the same span in the original.
_FOLD = str.maketrans("áéíóúüñàèìòùâêîôû", "aeiouunaeiouaeiou")


def fold(text: str) -> str:
    return (text or "").lower().translate(_FOLD)


_AUTO_SUBJECTS = (
    "out of office", "out-of-office", "automatic reply", "auto-reply", "autoreply",
    "auto reply", "fuera de la oficina", "respuesta automatica",
    "read receipt", "delivery receipt", "return receipt",
    "we have received your", "we've received your", "acuse de recibo",
)
_RECEIPT_PREFIXES = ("read:", "delivered:", "leido:", "entregado:", "not read:", "no leido:")
_RECEIPT_MARKERS = (
    "read receipt", "delivery receipt", "return receipt", "acuse de recibo",
    "your message was read", "was read on", "su mensaje fue leido", "tu mensaje fue leido",
    "was delivered to", "fue entregado",
)

_VACATION = re.compile("|".join((
    # English
    r"\bout of (the )?office\b", r"\booo\b",
    r"\bon (a )?(vacation|holiday|holidays|leave|annual leave|pto|sabbatical|"
    r"parental leave|maternity leave|paternity leave|sick leave|business trip|"
    r"business travel)\b",
    r"\b(away|out) (from|of) (the|my) (office|desk)\b",
    r"\bi('m| am| will be| ll be) (currently |now )?(away|out|travell?ing|on leave|off)\b",
    r"\bcurrently (away|out|travell?ing|on leave|unavailable)\b",
    r"\blimited (access to (my )?e-?mail|e-?mail access)\b",
    r"\b(will|i'll|i will) be back\b", r"\bback (in|at) (the|my) (office|desk)\b",
    r"\bi('m| am) back on\b", r"\b(i|i'll|i will) return on\b",
    r"\breturning (on|to the office)\b", r"\b(out|away) until\b",
    r"\bannual leave\b", r"\bvacation\b",
    # Spanish (folded)
    r"\bfuera de (la )?oficina\b", r"\bde vacaciones\b", r"\bausente\b",
    r"\bde (permiso|licencia)\b", r"\bno (estare|estoy) disponible\b",
    r"\b(estare|estoy) fuera\b", r"\bregreso el\b", r"\bregresare\b", r"\bvolvere\b",
    r"\bvuelvo el\b", r"\bde vuelta el\b", r"\bacceso limitado\b",
    r"\bme reincorporo\b", r"\bme reincorporare\b",
)))


def _headers(headers: dict | None) -> dict:
    return {str(k).lower(): str(v or "").lower() for k, v in (headers or {}).items()}


def is_automatic(subject: str, headers: dict | None) -> bool:
    """A machine wrote this: RFC 3834 headers, the common vendor headers, or
    a responder's tell-tale subject."""
    h = _headers(headers)
    if h.get("auto-submitted", "no") not in ("", "no"):
        return True
    if h.get("precedence") in ("bulk", "auto_reply", "junk"):
        return True
    if "x-autoreply" in h or "x-autorespond" in h:
        return True
    s = fold(subject).strip()
    return any(m in s for m in _AUTO_SUBJECTS) or s.startswith(_RECEIPT_PREFIXES)


def is_vacation_text(subject: str, body: str) -> bool:
    return bool(_VACATION.search(fold(subject)) or _VACATION.search(fold(body)[:3000]))


def classify_automatic(subject: str, body: str, headers: dict | None) -> str | None:
    """'out_of_office', 'receipt', 'acknowledgement', or None for a message
    no machine marked as automatic (a person, as far as headers can tell).

    A receipt is checked first: "Read: Out of office plans" is a receipt for
    an email about vacations, not a vacation reply. Anything else automatic
    pauses only when its words say the person is away.
    """
    if not is_automatic(subject, headers):
        return None
    s = fold(subject).strip()
    lowered = fold(body)[:2000]
    if s.startswith(_RECEIPT_PREFIXES) or any(m in s or m in lowered for m in _RECEIPT_MARKERS):
        return "receipt"
    if is_vacation_text(subject, body):
        return "out_of_office"
    return "acknowledgement"


# ── Return dates ──

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "abr": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "ago": 8, "septiembre": 9,
    "setiembre": 9, "set": 9, "octubre": 10, "noviembre": 11, "diciembre": 12, "dic": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "domingo": 6,
}


def _alt(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_MON = r"(" + _alt(_MONTHS) + r")\b\.?"
_WD = r"(" + _alt(_WEEKDAYS) + r")"
_DAY = r"(\d{1,2})(?:st|nd|rd|th|º|°)?"
_YEAR = r"(\d{4})"
_OPT_YEAR = r"(?:\s*,?\s*(?:de(?:l)?\s+)?" + _YEAR + r")?"
_RANGE_SEP = r"\s*(?:-|–|to|through|thru|al|a)\s*"

# (name, regex, base confidence). Longer matches win any overlap; ties go to
# the earlier entry, so a range beats the plain date inside it.
_PATTERNS = (
    ("range_mdd", re.compile(r"\b" + _MON + r"\s+" + _DAY + _RANGE_SEP + _DAY + r"\b" + _OPT_YEAR), 0.85),
    ("range_ddm", re.compile(r"\b(?:del?\s+)?" + _DAY + _RANGE_SEP + _DAY + r"(?:\s+(?:of|de))?\s+" + _MON + _OPT_YEAR), 0.85),
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), 0.95),
    ("md", re.compile(r"\b" + _MON + r"\s+" + _DAY + r"\b" + _OPT_YEAR), 0.95),
    ("dm", re.compile(r"\b" + _DAY + r"(?:\s+(?:of|de))?\s+" + _MON + _OPT_YEAR), 0.95),
    ("numeric", re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b"), 0.85),
    ("numeric_y", re.compile(r"\b(\d{1,2})[.-](\d{1,2})[.-](\d{4})\b"), 0.85),
    ("next_week", re.compile(r"\b(next week|la (?:proxima|siguiente) semana|"
                             r"la semana (?:que viene|proxima|siguiente))\b"), 0.7),
    ("weekday", re.compile(r"\b(?:(?:next|this|coming|el|este|el proximo|proximo)\s+)?" + _WD +
                           r"(?:\s+(?:que viene|proximo))?\b"), 0.85),
    ("tomorrow", re.compile(r"\b(tomorrow|manana)\b"), 0.85),
    ("day_en", re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b"), 0.8),
    ("day_es", re.compile(r"\b(?:el\s+(?:dia\s+)?|dia\s+)(\d{1,2})\b"), 0.8),
)

# The closest of these before a date says what the date means.
_ROLE_WORDS = (
    ("return", re.compile(
        r"\b(back|return|returns|returning|be in the office|in the office|"
        r"available again|available from|regreso|regresare|regresa|vuelvo|volvere|vuelve|"
        r"de vuelta|reincorporo|reincorporare|retorno|disponible de nuevo|"
        r"nuevamente disponible|disponible a partir del?)\b")),
    ("until", re.compile(r"\b(until|till|til|untill|hasta)\b")),
    ("through", re.compile(r"\b(through|thru)\b")),
    ("start", re.compile(r"\b(from|since|starting|leaving|beginning|desde|a partir del?|del)\b")),
)
_RETURN_AFTER = _ROLE_WORDS[0][1]
_CONNECTOR = re.compile(r"^\s*(-|–|to|through|thru|and|al|a|y)\s*$")
_ABSOLUTE = ("range_mdd", "range_ddm", "iso", "md", "dm", "numeric", "numeric_y")
_ADJACENT = re.compile(r"^[\s,(]*$")
_SENTENCE_BREAK = re.compile(r"[\n;!?]|\.\s")


class _Unreadable(ValueError):
    """A date mention that cannot become one calendar day: 'ambiguous' or 'invalid'."""


@dataclass
class _Mention:
    kind: str
    start: int
    end: int
    groups: tuple
    confidence: float
    day: date | None = None
    error: str = ""
    role: str = ""


@dataclass
class ReturnDate:
    """The outcome of reading a vacation reply.

    ``resume_at`` (naive UTC) is set only when the date is clear. Otherwise
    ``review_reason`` names why a person has to decide, and ``text`` still
    carries whatever date phrase was found, for them to read.
    """
    resume_at: datetime | None
    local_date: date | None
    text: str
    confidence: float
    review_reason: str
    timezone: str

    @property
    def review_state(self) -> str:
        return "scheduled" if self.resume_at else "needs_review"

    def as_dict(self) -> dict:
        return {
            "resume_at": self.resume_at.isoformat() if self.resume_at else None,
            "local_date": self.local_date.isoformat() if self.local_date else None,
            "text": self.text, "confidence": self.confidence,
            "review_state": self.review_state, "review_reason": self.review_reason,
            "timezone": self.timezone,
        }


def _tz(name: str):
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.utc


def resume_time(day: date, tz_name: str) -> datetime:
    """``RESUME_HOUR`` local on ``day``, as naive UTC (how the DB stores time)."""
    local = _tz(tz_name).localize(datetime.combine(day, time(RESUME_HOUR)))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def local_day(moment: datetime, tz_name: str) -> date:
    """The calendar day ``moment`` falls on in ``tz_name``. Naive = UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_tz(tz_name)).date()


def _with_year(month: int, day: int, ref: date) -> date:
    """A month and day with no year: this year's when it is still ahead (or
    only just passed, which is then flagged as past), else next year's.
    February 29 resolves only to a leap year within that window."""
    try:
        this = date(ref.year, month, day)
    except ValueError:
        this = None
    if this and (this >= ref or (ref - this).days <= RECENT_PAST_DAYS):
        return this
    try:
        return date(ref.year + 1, month, day)
    except ValueError:
        if this:
            return this
        raise _Unreadable("invalid") from None


def _ymd(year: str | None, month: int, day: int, ref: date) -> date:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise _Unreadable("invalid")
    if year:
        y = int(year)
        if y < 100:
            y += 2000
        try:
            return date(y, month, day)
        except ValueError:
            raise _Unreadable("invalid") from None
    return _with_year(month, day, ref)


def _numeric(a: str, b: str, year: str | None, ref: date) -> date:
    """1/2 is January 2 in the US and 1 February almost everywhere else. Only
    a date that reads one way (one part above 12, or both equal) is used."""
    x, y = int(a), int(b)
    readings = []
    for month, day in ((x, y), (y, x)):
        try:
            readings.append(_ymd(year, month, day, ref))
        except _Unreadable:
            pass
    readings = sorted(set(readings))
    if not readings:
        raise _Unreadable("invalid")
    if len(readings) > 1:
        raise _Unreadable("ambiguous")
    return readings[0]


def _day_of_month(day: int, ref: date) -> date:
    """'the 20th': this month when it is still ahead, else next month."""
    if not 1 <= day <= 31:
        raise _Unreadable("invalid")
    year, month = ref.year, ref.month
    if day < ref.day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    try:
        return date(year, month, day)
    except ValueError:
        raise _Unreadable("invalid") from None


def _resolve(m: _Mention, ref: date) -> date:
    g = m.groups
    if m.kind == "range_mdd":
        return _ymd(g[3], _MONTHS[g[0]], int(g[2]), ref)
    if m.kind == "range_ddm":
        return _ymd(g[3], _MONTHS[g[2]], int(g[1]), ref)
    if m.kind == "iso":
        return _ymd(g[0], int(g[1]), int(g[2]), ref)
    if m.kind == "md":
        return _ymd(g[2], _MONTHS[g[0]], int(g[1]), ref)
    if m.kind == "dm":
        return _ymd(g[2], _MONTHS[g[1]], int(g[0]), ref)
    if m.kind in ("numeric", "numeric_y"):
        return _numeric(g[0], g[1], g[2], ref)
    if m.kind == "next_week":
        return ref + timedelta(days=7 - ref.weekday())
    if m.kind == "weekday":
        ahead = (_WEEKDAYS[g[0]] - ref.weekday()) % 7
        return ref + timedelta(days=ahead or 7)
    if m.kind == "tomorrow":
        return ref + timedelta(days=1)
    return _day_of_month(int(g[0]), ref)


def _find_mentions(t: str) -> list[_Mention]:
    found = []
    for priority, (kind, rx, confidence) in enumerate(_PATTERNS):
        for match in rx.finditer(t):
            if kind == "tomorrow" and t[max(0, match.start() - 3):match.start()] == "la ":
                continue  # "por la mañana" is the morning, not tomorrow
            if match.group(0) == "24/7":
                continue
            found.append((match.end() - match.start(), -priority,
                          _Mention(kind, match.start(), match.end(), match.groups(), confidence)))
    found.sort(key=lambda f: (f[0], f[1]), reverse=True)
    taken: list[_Mention] = []
    for _length, _priority, m in found:
        if all(m.end <= o.start or m.start >= o.end for o in taken):
            taken.append(m)
    taken.sort(key=lambda m: m.start)
    # "Monday, October 20" and "20 de octubre (lunes)" name one day twice;
    # the weekday is decoration next to a full date, not a second date.
    keep = []
    for i, m in enumerate(taken):
        if m.kind == "weekday":
            before = taken[i - 1] if i else None
            after = taken[i + 1] if i + 1 < len(taken) else None
            if ((after and after.kind in _ABSOLUTE and _ADJACENT.match(t[m.end:after.start]))
                    or (before and before.kind in _ABSOLUTE
                        and _ADJACENT.match(t[before.end:m.start]))):
                continue
        keep.append(m)
    return keep


def _role(t: str, m: _Mention, previous: _Mention | None) -> str:
    if m.kind.startswith("range"):
        return "range_end"
    if previous is not None and _CONNECTOR.match(t[previous.end:m.start]):
        return "range_end"
    window = t[max(0, m.start - 60):m.start]
    breaks = list(_SENTENCE_BREAK.finditer(window))
    if breaks:
        window = window[breaks[-1].end():]
    best, best_at = "", -1
    for role, rx in _ROLE_WORDS:
        for hit in rx.finditer(window):
            if hit.end() > best_at:
                best, best_at = role, hit.end()
    if best:
        return "" if best == "start" else best
    # "On October 20 I'll be back": the keyword may follow the date, but only
    # within the same clause ("out next week, back the week after" is not).
    after = t[m.end:m.end + 30]
    stop = re.search(r"[,(]|" + _SENTENCE_BREAK.pattern, after)
    if stop:
        after = after[:stop.start()]
    return "return_after" if _RETURN_AFTER.search(after) else ""


# How sure a role makes us that the date is the day they are back, and the
# day the sequence may resume relative to it.
_ROLE_WEIGHT = {"return": (1.0, 0), "until": (1.0, 0), "through": (0.95, 1),
                "range_end": (0.85, 1), "return_after": (0.9, 0)}


def parse_return_date(text: str, received_at: datetime, tz_name: str = "UTC") -> ReturnDate:
    """Read the return date from a vacation reply's own words.

    ``received_at`` is the message's timestamp (aware, or naive UTC). Every
    relative date resolves against that moment's calendar day in ``tz_name``,
    so "back tomorrow" written late on a Tuesday evening in New York means
    Wednesday even though it is already Wednesday in UTC.
    """
    ref = local_day(received_at, tz_name)
    original = (text or "")[:4000]
    t = fold(original)

    mentions = _find_mentions(t)
    previous = None
    for m in mentions:
        m.role = _role(t, m, previous)
        try:
            m.day = _resolve(m, ref)
        except _Unreadable as e:
            m.error = str(e)
        previous = m

    def outcome(day=None, mention=None, confidence=0.0, reason=""):
        return ReturnDate(
            resume_at=resume_time(day, tz_name) if day and not reason else None,
            local_date=day if not reason else None,
            text=original[mention.start:mention.end].strip(" .,") if mention else "",
            confidence=round(confidence, 2) if not reason else 0.0,
            review_reason=reason, timezone=tz_name)

    meaningful = [m for m in mentions if m.role]
    if not meaningful:
        return outcome(mention=mentions[0] if mentions else None,
                       reason="unclear" if mentions else "no_date")
    returns = [m for m in meaningful if m.role in ("return", "return_after")]
    chosen = returns or meaningful
    broken = next((m for m in chosen if m.error), None)
    if broken:
        return outcome(mention=broken, reason=broken.error)

    days = {}
    for m in chosen:
        weight, shift = _ROLE_WEIGHT[m.role]
        days.setdefault(m.day + timedelta(days=shift), (m, m.confidence * weight))
    if len(days) > 1:
        return outcome(mention=chosen[0], reason="conflicting")
    (day, (mention, confidence)), = days.items()
    if day < ref:
        return outcome(mention=mention, reason="past")
    if (day - ref).days > MAX_AWAY_DAYS:
        return outcome(mention=mention, reason="too_far")
    return outcome(day, mention, confidence)


def parse_stored(value) -> datetime | None:
    """A timestamp as the DB stores it (naive UTC, 'T' or space), or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def message_time(raw_date: str, fallback: datetime) -> datetime:
    """The inbound message's Date header as naive UTC, or ``fallback``."""
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw_date) if raw_date else None
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
