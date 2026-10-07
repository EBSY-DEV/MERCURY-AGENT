"""Out-of-office replies: recognise a vacation auto-reply, read when the person
is back, and work out the first sending time after that.

Everything here is deterministic and offline: no model call, no network, no
clock. The caller passes the time the message arrived, which is what relative
phrases ("back tomorrow", "for two weeks") are measured against. English and
Spanish are supported because Mercury writes to US and Dominican prospects.

The extractor never guesses. When the reply has no usable date it says so
(``none``), and the same goes for a date that could be read two ways
(``10/11``), one that does not exist (``31/02``, ``Feb 29`` in a common year),
one that is already behind us (``past``) and one so far out that it is surely
a typo (``too_far``). The caller turns each of those into a "return date needs
review" pause rather than inventing a date.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

# A date further out than this is treated as a typo, not a vacation.
MAX_DAYS_AHEAD = 365
# A month-and-day with no year that is already this far behind us is read as
# next year ("until January 3" written on December 28). Anything closer is a
# date that has simply passed.
ROLL_FORWARD_AFTER_DAYS = 60

# ── Recognising a vacation reply ──

# Subject markers: if the sender's own autoresponder titled the message like
# this, it is a vacation notice and nothing else.
_SUBJECT_MARKERS = re.compile(
    r"out[ -]of[ -](the[ -])?office|\booo\b|on vacation|vacation (reply|notice|message)"
    r"|away from (my |the )?(desk|office)|fuera de (la )?oficina|\bausente\b|vacaciones"
    r"|de licencia"
)
# First-person statements. A support desk saying "we are out of office on
# weekends" is not one of these.
_PERSONAL_AWAY = re.compile(
    r"\b(i am|i'm|im|i will be|i'll be|i have been|i'm currently|i am currently|i am now)\b"
    r".{0,45}?\b(out of( the)? office|away|on (vacation|holiday|leave|pto|annual leave"
    r"|maternity leave|paternity leave|sabbatical)|out until|traveling|travelling"
    r"|out of town|off until)"
    r"|\bmy (office hours|vacation)\b"
    r"|\b(estare|estoy|me encuentro|me ausentare|voy a estar|permanecere|estaremos)\b"
    r".{0,60}?\b(fuera|ausente|de vacaciones|de viaje|de licencia|de permiso|de baja"
    r"|de descanso|sin acceso)"
    r"|\bno (me encuentro|estare|estoy) (en la oficina|disponible)"
)
_GENERIC_AWAY = re.compile(
    r"out[ -]of[ -](the[ -])?office|\booo\b|on vacation|on holiday|on annual leave"
    r"|\baway from (my |the )?(desk|office|email|computer)"
    r"|(office|we) (is|are|will be) closed|limited (access|connectivity) to (my )?e-?mail"
    r"|fuera de (la )?oficina|de vacaciones|oficina (estara )?cerrada"
)
# Acknowledgements and ticket systems. Without a first-person away statement,
# these are not vacation notices even when they mention being out of office.
_ACK_MARKERS = re.compile(
    r"we have received (your|the)|your (message|request|inquiry|enquiry|email|ticket)"
    r" (has been|was|is) (received|logged|created|submitted)|ticket (number|#|id)|case (number|#|id)"
    r"|reference (number|#)|thank you for (contacting|reaching out|your (message|email|inquiry))"
    r"|thanks for (contacting|reaching out)|gracias por (contactar|escribir|comunicarse)"
    r"|hemos recibido (su|tu)|su (mensaje|solicitud|consulta) (ha sido|fue) (recibid|registrad)"
    r"|numero de (ticket|caso|referencia)"
)
_RECEIPT_SUBJECT = re.compile(
    r"^\s*(read|delivered|not read|accepted|declined|tentative)\s*:|(return|read|delivery) receipt"
    r"|acuse de (recibo|lectura)|confirmacion de (lectura|entrega)"
)


def _fold(text: str) -> str:
    """Lowercase and strip accents, one output character per input character."""
    out = []
    for ch in text or "":
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        if len(base) != 1:
            base = ch
        if base.isspace():
            base = " "
        elif base in "‐‑‒–—−":
            base = "-"
        elif base in "’‘ʼ":
            base = "'"
        out.append(base.lower())
    return "".join(out)


def is_receipt(subject: str, headers: dict | None = None) -> bool:
    """A read or delivery receipt (a message disposition notification)."""
    ctype = " ".join(str(v) for k, v in (headers or {}).items()
                     if str(k).lower() == "content-type").lower()
    return "report-type=disposition-notification" in ctype or bool(
        _RECEIPT_SUBJECT.search(_fold(subject)))


def classify_auto_reply(subject: str, body: str, headers: dict | None = None) -> str:
    """What kind of automatic message this is: ``ooo``, ``receipt`` or
    ``acknowledgement``.

    Only ``ooo`` pauses a sequence. ``receipt`` covers read/delivery receipts
    and ``acknowledgement`` covers everything else an autoresponder says
    ("we received your message", ticket numbers, list mail). The caller has
    already decided the message is automatic, from its headers or its subject.
    """
    subj = _fold(subject)
    text = _fold(body or "")[:3000]
    if is_receipt(subject, headers):
        return "receipt"
    personal = bool(_PERSONAL_AWAY.search(text) or _PERSONAL_AWAY.search(subj))
    if _SUBJECT_MARKERS.search(subj) or personal:
        return "ooo"
    if _GENERIC_AWAY.search(text) and not _ACK_MARKERS.search(text):
        return "ooo"
    return "acknowledgement"


# ── Reading the return date ──


@dataclass
class ReturnDate:
    """What the extractor found.

    ``status`` is ``date`` when ``date`` is a usable return date, otherwise one
    of ``none``, ``ambiguous``, ``invalid``, ``past`` or ``too_far``; ``date``
    is still set for ``past`` and ``too_far`` so the review screen can show it.
    ``text`` is the phrase the date was read from, as the sender wrote it.
    """

    status: str
    date: date | None = None
    text: str = ""
    confidence: float = 0.0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "date" and self.date is not None


_MONTHS = {
    "jan": 1, "january": 1, "ene": 1, "enero": 1,
    "feb": 2, "february": 2, "febrero": 2,
    "mar": 3, "march": 3, "marzo": 3,
    "apr": 4, "april": 4, "abr": 4, "abril": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "june": 6, "junio": 6,
    "jul": 7, "july": 7, "julio": 7,
    "aug": 8, "august": 8, "ago": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "septiembre": 9, "setiembre": 9,
    "oct": 10, "october": 10, "octubre": 10,
    "nov": 11, "november": 11, "noviembre": 11,
    "dec": 12, "december": 12, "dic": 12, "diciembre": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "lunes": 0,
    "tuesday": 1, "tues": 1, "tue": 1, "martes": 1,
    "wednesday": 2, "wed": 2, "miercoles": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "jueves": 3,
    "friday": 4, "fri": 4, "viernes": 4,
    "saturday": 5, "sat": 5, "sabado": 5,
    "sunday": 6, "sun": 6, "domingo": 6,
}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "un": 1, "una": 1, "uno": 1,
    "two": 2, "dos": 2, "three": 3, "tres": 3, "four": 4, "cuatro": 4,
    "five": 5, "cinco": 5, "six": 6, "seis": 6, "seven": 7, "siete": 7,
    "eight": 8, "ocho": 8, "nine": 9, "nueve": 9, "ten": 10, "diez": 10,
    "eleven": 11, "once": 11, "twelve": 12, "doce": 12,
    "a couple of": 2, "a couple": 2, "couple of": 2,
}
_UNITS = {
    "day": "d", "days": "d", "dia": "d", "dias": "d",
    "week": "w", "weeks": "w", "semana": "w", "semanas": "w",
    "month": "m", "months": "m", "mes": "m", "meses": "m",
}

_MONTH = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?"
_WD = r"(?:" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b"
_NUM = r"(?:\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b"
_ORD = r"(?:st|nd|rd|th)?"
_OF = r"(?:of\s+|de\s+)?"

# A word that says "this is when I am reachable again" (or, for "through",
# the last day I am away).
_CUE = re.compile(
    r"(?<![a-z])(until|till|til|through|thru|returning|returns|return|back|resum\w*"
    r"|available again|hasta|regres\w*|volver\w*|vuelv\w*|de vuelta|reincorpor\w*|retom\w*"
    r"|a partir del|disponible)(?![a-z])"
)
_AWAY_CONTEXT = re.compile(
    r"out of( the)? office|away|vacation|holiday|leave|pto|travel|trip|ausente|fuera"
    r"|vacaciones|viaje|licencia|limited (access|connectivity)|sin acceso"
)

_RE_RANGE_MONTH_FIRST = re.compile(
    rf"(?<![a-z])(?P<m1>{_MONTH})\s*(?P<d1>\d{{1,2}}){_ORD}(?:\s*,?\s*(?P<y1>\d{{4}}))?(?!\d)"
    rf"\s*(?:-|to|through|thru)\s*(?:(?P<m2>{_MONTH})\s*)?(?P<d2>\d{{1,2}}){_ORD}"
    rf"(?:\s*,?\s*(?P<y2>\d{{4}}))?(?![\d:])(?!\s*[ap]\.?m\b)"
)
_RE_RANGE_DAY_FIRST = re.compile(
    rf"(?<![\d/.-])(?P<d1>\d{{1,2}}){_ORD}(?:\s+{_OF}(?P<m1>{_MONTH}))?"
    rf"\s*(?:-|to|through|thru|al)\s*(?:the\s+)?(?P<d2>\d{{1,2}}){_ORD}\s+{_OF}(?P<m2>{_MONTH})"
    rf"(?:\s*,?\s*(?:de\s+|del\s+)?(?P<y>\d{{4}}))?(?!\d)"
)
_NUMERIC = r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
_RE_RANGE_NUMERIC = re.compile(
    rf"(?<![\d/.:-])(?P<a>{_NUMERIC})\s*(?:-|to|through|thru|al)\s*(?P<b>{_NUMERIC})(?![\d/])"
)
_RE_MONTH_FIRST = re.compile(
    rf"(?:(?P<wd>{_WD}),?\s+)?(?<![a-z])(?P<m>{_MONTH})\s*(?P<d>\d{{1,2}}){_ORD}(?!\d)"
    rf"(?:\s*,?\s*(?P<y>\d{{4}}))?(?!\d)"
)
_RE_DAY_FIRST = re.compile(
    rf"(?:(?P<wd>{_WD}),?\s+(?:the\s+|el\s+)?)?(?<![\d/.-])(?P<d>\d{{1,2}}){_ORD}\s+{_OF}"
    rf"(?P<m>{_MONTH})(?:\s*,?\s*(?:de\s+|del\s+)?(?P<y>\d{{4}}))?(?!\d)"
)
_RE_ISO = re.compile(r"(?<![\d-])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d-])")
_RE_NUMERIC = re.compile(
    r"(?<![\d/.:-])(?P<a>\d{1,2})(?P<sep>[/.-])(?P<b>\d{1,2})"
    r"(?:(?P=sep)(?P<y>\d{4}|\d{2}))?(?![\d])(?![/.-]\d)"
)
_RE_MONTH_ONLY = re.compile(
    rf"(?:until|till|til|through|thru|back|returning|return|hasta|regres\w*|volver\w*)"
    rf"\s+(?:in\s+|en\s+|early\s+|late\s+|mid\s+|a\s+|principios de\s+|finales de\s+)?(?P<m>{_MONTH})"
    rf"(?!\s*\d)"
)
_RE_WEEKDAY = re.compile(
    rf"(?<![a-z])(?:(?:next|this|proximo|este|el)\s+)?(?P<wd>{_WD})(?![a-z])"
)
_RE_ORDINAL_DAY = re.compile(
    r"(?<![\d/.-])(?:the\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)(?!\w)"
    r"|(?:el|el dia)\s+(?P<d2>\d{1,2})(?!\s*(?:de\b|/|-|\d|:)))"
)
_RE_REL_IN = re.compile(
    rf"(?:until|till|til|back|returning|return|regres\w*|volver\w*|vuelv\w*|de vuelta)\s+"
    rf"(?:in|en|within|dentro de|after|despues de)\s+(?P<n>{_NUM})\s+(?P<u>{_UNIT})"
)
_RE_REL_FOR = re.compile(
    rf"(?:for|por|durante)\s+(?:the\s+next\s+|the\s+|los\s+proximos\s+|las\s+proximas\s+)?"
    rf"(?P<n>{_NUM})\s+(?P<u>{_UNIT})"
)
_RE_TOMORROW = re.compile(
    r"(?:until|till|til|back|returning|return|regres\w*|volver\w*|vuelv\w*|de vuelta)"
    r"\s+(?:on\s+)?(?:tomorrow|manana)(?![a-z])"
)
_RE_NEXT_WEEK = re.compile(
    r"(?:until|till|til|back|returning|return|hasta|regres\w*|volver\w*|vuelv\w*|de vuelta)\s+"
    r"(?:(?:early|late|the|en|a|in)\s+)?(?:next week|la proxima semana|la semana que viene"
    r"|la semana proxima)"
)
_RE_NEXT_MONTH = re.compile(
    r"(?:until|till|til|back|returning|return|hasta|regres\w*|volver\w*|vuelv\w*|de vuelta)\s+"
    r"(?:(?:early|late|the|en|a|in)\s+)?(?:next month|el proximo mes|el mes que viene)"
)


@dataclass
class _Cand:
    start: int
    end: int
    kind: str                      # "date" | "ambiguous" | "invalid"
    date: date | None = None
    confidence: float = 0.0
    note: str = ""


def _add_months(d: date, n: int) -> date:
    month_index = d.month - 1 + n
    year, month = d.year + month_index // 12, month_index % 12 + 1
    day = d.day
    while day > 28:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, day)


def _num(token: str) -> int | None:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _resolve_month_day(day: int, month: int, year: int | None, today: date):
    """(date | None, kind, confidence_penalty). Without a year, the next
    occurrence of that month and day, counting only dates that exist
    (Feb 29 lands on the next leap year)."""
    if year is not None:
        try:
            return date(year, month, day), "date", 0.0
        except ValueError:
            return None, "invalid", 0.0
    found_any = False
    for y in (today.year, today.year + 1):
        try:
            cand = date(y, month, day)
        except ValueError:
            continue
        found_any = True
        if y == today.year and cand < today - timedelta(days=ROLL_FORWARD_AFTER_DAYS):
            continue
        return cand, "date", (0.05 if y != today.year else 0.0)
    if not found_any:
        return None, "invalid", 0.0
    # Only reachable when the sole existing candidate was rolled over; keep
    # the in-year one so the caller reports it as past.
    try:
        return date(today.year, month, day), "date", 0.0
    except ValueError:
        return None, "invalid", 0.0


def _numeric_date(a: int, b: int, year_text: str | None, today: date):
    """Day/month or month/day, but only when the digits settle which."""
    year = None
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    if a > 12 and b <= 12:
        day, month = a, b
    elif b > 12 and a <= 12:
        month, day = a, b
    elif a > 12 and b > 12:
        return None, "invalid", 0.0
    elif a == b:
        day = month = a
    else:
        return None, "ambiguous", 0.0
    d, kind, pen = _resolve_month_day(day, month, year, today)
    return d, kind, pen


def _check_weekday(d: date | None, wd_text: str | None) -> bool:
    if d is None or not wd_text:
        return True
    return d.weekday() == _WEEKDAYS[wd_text]


def _cue_before(norm: str, start: int) -> tuple[bool, bool]:
    """(has a return cue just before ``start``, that cue is "through")."""
    window = norm[max(0, start - 45):start]
    for sep in (". ", "; ", "! ", "? "):
        i = window.rfind(sep)
        if i != -1:
            window = window[i + len(sep):]
    last = None
    for m in _CUE.finditer(window):
        last = m
    if last is None:
        return False, False
    return True, last.group(1) in ("through", "thru")


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < e and s < end for s, e in spans)


def _collect(norm: str, today: date) -> list[_Cand]:
    cands: list[_Cand] = []
    taken: list[tuple[int, int]] = []

    def add(c: _Cand):
        cands.append(c)
        taken.append((c.start, c.end))

    def range_end(m, d_name, m_names, y_name_candidates):
        month_txt = next((m.group(n) for n in m_names if m.group(n)), None)
        month = _MONTHS[month_txt.rstrip(".")] if month_txt else None
        year_txt = next((m.group(n) for n in y_name_candidates if m.group(n)), None)
        return int(m.group(d_name)), month, int(year_txt) if year_txt else None

    # Ranges: "Oct 6-20", "from October 6 to October 20", "del 6 al 20 de octubre".
    # The end of a range is the last day away, so they are back the day after.
    for m in _RE_RANGE_MONTH_FIRST.finditer(norm):
        day, month, year = range_end(m, "d2", ("m2", "m1"), ("y2", "y1"))
        d, kind, pen = _resolve_month_day(day, month, year, today)
        if d is not None:
            d += timedelta(days=1)
        add(_Cand(m.start(), m.end(), kind, d, 0.9 - pen, "range end"))
    for m in _RE_RANGE_DAY_FIRST.finditer(norm):
        if _overlaps(taken, m.start(), m.end()):
            continue
        day, month, year = range_end(m, "d2", ("m2",), ("y",))
        d, kind, pen = _resolve_month_day(day, month, year, today)
        if d is not None:
            d += timedelta(days=1)
        add(_Cand(m.start(), m.end(), kind, d, 0.9 - pen, "range end"))
    for m in _RE_RANGE_NUMERIC.finditer(norm):
        if _overlaps(taken, m.start(), m.end()):
            continue
        parts = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", m.group("b")).groups()
        d, kind, pen = _numeric_date(int(parts[0]), int(parts[1]), parts[2], today)
        if d is not None:
            d += timedelta(days=1)
        add(_Cand(m.start(), m.end(), kind, d, 0.85 - pen, "range end"))

    # Single dates need a cue ("until", "back on", "hasta el") in front of them.
    def single(m, kind, d, conf, note):
        if _overlaps(taken, m.start(), m.end()):
            return
        cued, through = _cue_before(norm, m.start())
        if not cued:
            return
        if kind == "date" and d is not None and through:
            d += timedelta(days=1)  # "through Oct 17": the last day away
        add(_Cand(m.start(), m.end(), kind, d, conf, note))

    for m in _RE_ISO.finditer(norm):
        d, kind, pen = _resolve_month_day(int(m["d"]), int(m["m"]), int(m["y"]), today)
        single(m, kind, d, 0.95, "iso")
    for rx in (_RE_MONTH_FIRST, _RE_DAY_FIRST):
        for m in rx.finditer(norm):
            year = int(m["y"]) if m["y"] else None
            month = _MONTHS[m["m"].rstrip(".")]
            d, kind, pen = _resolve_month_day(int(m["d"]), month, year, today)
            if kind == "date" and not _check_weekday(d, m["wd"]):
                single(m, "ambiguous", None, 0.0, "weekday does not match the date")
                continue
            single(m, kind, d, 0.95 - pen, "named month")
    for m in _RE_NUMERIC.finditer(norm):
        if m["sep"] == "." and not m["y"]:
            continue  # "1.5 weeks", not a date
        d, kind, pen = _numeric_date(int(m["a"]), int(m["b"]), m["y"], today)
        single(m, kind, d, 0.9 - pen - (0.05 if m["y"] and len(m["y"]) == 2 else 0), "numeric")
    for m in _RE_MONTH_ONLY.finditer(norm):
        if not _overlaps(taken, m.start(), m.end()):
            add(_Cand(m.start(), m.end(), "ambiguous", None, 0.0, "month without a day"))

    # Weekdays and bare days of the month.
    for m in _RE_WEEKDAY.finditer(norm):
        wd = _WEEKDAYS[m["wd"]]
        delta = (wd - today.weekday()) % 7 or 7
        single(m, "date", today + timedelta(days=delta), 0.75, "weekday")
    for m in _RE_ORDINAL_DAY.finditer(norm):
        day = int(m["d1"] or m["d2"])
        if not 1 <= day <= 31:
            continue
        span = m.span("d1") if m["d1"] else m.span("d2")
        if _overlaps(taken, *span):
            continue
        d = None
        for offset in (0, 1, 2):
            probe = _add_months(today.replace(day=1), offset)
            try:
                cand = probe.replace(day=day)
            except ValueError:
                continue
            if cand > today:
                d = cand
                break
        single(m, "date" if d else "invalid", d, 0.7, "day of month")

    # Relative phrases, measured against when the message arrived.
    def offset(n: int, unit: str) -> date:
        kind = _UNITS[unit]
        if kind == "d":
            return today + timedelta(days=n)
        if kind == "w":
            return today + timedelta(weeks=n)
        return _add_months(today, n)

    for m in _RE_REL_IN.finditer(norm):
        n = _num(m["n"])
        if n is not None and not _overlaps(taken, m.start(), m.end()):
            add(_Cand(m.start(), m.end(), "date", offset(n, m["u"]), 0.75, "relative"))
    for m in _RE_REL_FOR.finditer(norm):
        n = _num(m["n"])
        if n is None or _overlaps(taken, m.start(), m.end()):
            continue
        if not _AWAY_CONTEXT.search(norm[max(0, m.start() - 90):m.start()]):
            continue
        add(_Cand(m.start(), m.end(), "date", offset(n, m["u"]), 0.7, "relative"))
    for m in _RE_TOMORROW.finditer(norm):
        if not _overlaps(taken, m.start(), m.end()):
            add(_Cand(m.start(), m.end(), "date", today + timedelta(days=1), 0.85, "relative"))
    for m in _RE_NEXT_WEEK.finditer(norm):
        if not _overlaps(taken, m.start(), m.end()):
            monday = today + timedelta(days=7 - today.weekday())
            add(_Cand(m.start(), m.end(), "date", monday, 0.65, "next week"))
    for m in _RE_NEXT_MONTH.finditer(norm):
        if not _overlaps(taken, m.start(), m.end()):
            add(_Cand(m.start(), m.end(), "ambiguous", None, 0.0, "next month"))

    cands.sort(key=lambda c: c.start)
    return cands


def _local_today(received: datetime, tz_name: str) -> date:
    import pytz

    try:
        tz = pytz.timezone(tz_name or "UTC")
    except Exception:
        tz = pytz.UTC
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return received.astimezone(tz).date()


def extract_return_date(text: str, received: datetime, tz_name: str = "UTC") -> ReturnDate:
    """Find when the sender is back, measured against ``received``.

    ``received`` is when the message arrived (naive values are UTC) and
    ``tz_name`` is the operator's timezone, used because the sender's own is
    unknown: "back tomorrow" written at 02:00 UTC is still today in New York.
    """
    source = " ".join((text or "")[:4000].split("\n"))
    norm = _fold(source)
    today = _local_today(received, tz_name)
    cands = _collect(norm, today)
    if not cands:
        return ReturnDate("none", note="no return date in the message")

    def snippet(c: _Cand) -> str:
        return " ".join(source[c.start:c.end].split()).rstrip(" .,;:")

    good = [c for c in cands if c.kind == "date" and c.date is not None]
    bad = [c for c in cands if c.kind != "date"]
    if bad and not good:
        c = bad[0]
        status = "invalid" if c.kind == "invalid" else "ambiguous"
        return ReturnDate(status, text=snippet(c), note=c.note or status)
    if bad:
        return ReturnDate("ambiguous", text=snippet(bad[0]),
                          note="the message gives more than one reading of its dates")
    if len({c.date for c in good}) > 1:
        return ReturnDate("ambiguous", text=snippet(good[0]),
                          note="the message gives different return dates")
    c = good[0]
    if c.date < today:
        return ReturnDate("past", date=c.date, text=snippet(c), confidence=c.confidence,
                          note="that date has already passed")
    if (c.date - today).days > MAX_DAYS_AHEAD:
        return ReturnDate("too_far", date=c.date, text=snippet(c), confidence=c.confidence,
                          note=f"more than {MAX_DAYS_AHEAD} days away")
    return ReturnDate("date", date=c.date, text=snippet(c),
                      confidence=round(max(0.0, min(1.0, c.confidence)), 2), note=c.note)


# ── Turning a return date into a sending time ──


def operator_clock(config) -> tuple[str, str]:
    """(timezone name, quiet-hours end "HH:MM") from the config, tolerating a
    config without a ``usage`` section."""
    quiet = getattr(getattr(config, "usage", None), "quiet_hours", None)
    tz_name = getattr(quiet, "timezone", "") or "UTC"
    end = getattr(quiet, "end", "") or "07:00"
    return tz_name, end


def resume_time(return_date: date, tz_name: str = "UTC", quiet_end: str = "07:00") -> datetime:
    """First sending time on or after ``return_date``, as naive UTC.

    That is the moment quiet hours end in the operator's timezone, moved to
    Monday when the date falls on a weekend.
    """
    import pytz

    try:
        tz = pytz.timezone(tz_name or "UTC")
    except Exception:
        tz = pytz.UTC
    try:
        at = dtime.fromisoformat(quiet_end)
    except ValueError:
        at = dtime(7, 0)
    day = return_date
    while day.weekday() >= 5:
        day += timedelta(days=1)
    local = tz.localize(datetime.combine(day, at.replace(tzinfo=None)))
    return local.astimezone(pytz.UTC).replace(tzinfo=None)
