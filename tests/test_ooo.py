"""Out-of-office recognition, return-date extraction and resume timing.

Everything here is pure: the extractor takes the time the message arrived, so
no test depends on the clock. The reference message arrives Wednesday
2026-10-07 15:00 UTC, which is 11:00 that day in New York.
"""

from datetime import date, datetime

import pytest

from mercury.ooo import (
    classify_auto_reply,
    extract_return_date,
    operator_clock,
    resume_time,
)

RECEIVED = datetime(2026, 10, 7, 15, 0)   # Wednesday
NY = "America/New_York"


def got(text, received=RECEIVED, tz=NY):
    return extract_return_date(text, received, tz)


# ── clear dates, English ──


@pytest.mark.parametrize("text,expected", [
    ("I'm away until October 20.", date(2026, 10, 20)),
    ("I will be out of the office until Oct 20th and will reply on my return.", date(2026, 10, 20)),
    ("Out of office until Oct. 20.", date(2026, 10, 20)),
    ("I am out until 20 October.", date(2026, 10, 20)),
    ("I'm out until the 20th of October.", date(2026, 10, 20)),
    ("I will return on October 20, 2026.", date(2026, 10, 20)),
    ("Back on Oct 20 2026.", date(2026, 10, 20)),
    ("Returning Tuesday, October 20.", date(2026, 10, 20)),
    ("I am out of the office from Monday, October 12 until Tuesday, October 20, 2026.",
     date(2026, 10, 20)),
    ("I'll be back on 10/20.", date(2026, 10, 20)),            # 20 cannot be a month
    ("Back on 10/20/2026.", date(2026, 10, 20)),
    ("Back on 10/20/26.", date(2026, 10, 20)),
    ("I'm out until 2026-10-20.", date(2026, 10, 20)),
    ("Out of office until 20.10.2026.", date(2026, 10, 20)),
    ("I will be back on 5/5.", date(2027, 5, 5)),               # same either way, next occurrence
    ("Back on SEPTEMBER 3, 2027", date(2027, 9, 3)),
])
def test_clear_english_dates(text, expected):
    r = got(text)
    assert r.status == "date", r
    assert r.date == expected
    assert r.confidence >= 0.8
    assert r.text  # the phrase it was read from is kept


def test_phrase_is_kept_as_written():
    r = got("Thanks for writing. I'm away until Tuesday, October 20 with no email access.")
    assert r.text == "Tuesday, October 20"


# ── ranges and "through" ──


@pytest.mark.parametrize("text,expected", [
    ("I'm on vacation Oct 12-20. Back after.", date(2026, 10, 21)),
    ("Out of the office October 12 to October 20.", date(2026, 10, 21)),
    ("I will be away from October 12 through October 16.", date(2026, 10, 17)),
    ("Out of office from 10/12 to 10/20.", date(2026, 10, 21)),
    ("Away 12-20 October.", date(2026, 10, 21)),
    ("Out of the office through Friday.", date(2026, 10, 10)),   # last day away is Friday the 9th
    ("Out through Oct 16.", date(2026, 10, 17)),
])
def test_range_end_is_the_last_day_away(text, expected):
    r = got(text)
    assert r.status == "date", r
    assert r.date == expected


def test_until_is_the_day_they_are_back_not_the_day_after():
    assert got("Out until October 16.").date == date(2026, 10, 16)
    assert got("Out through October 16.").date == date(2026, 10, 17)


# ── relative dates ──


@pytest.mark.parametrize("text,expected", [
    ("I'll be back tomorrow.", date(2026, 10, 8)),
    ("Out of the office for two weeks.", date(2026, 10, 21)),
    ("I am out of the office for 3 days.", date(2026, 10, 10)),
    ("I will be away for a week.", date(2026, 10, 14)),
    ("I'm on leave for the next 2 weeks.", date(2026, 10, 21)),
    ("Back in 3 days.", date(2026, 10, 10)),
    ("I'll return in a week.", date(2026, 10, 14)),
    ("Returning in 2 months.", date(2026, 12, 7)),
    ("Out until next Monday.", date(2026, 10, 12)),
    ("Back Monday.", date(2026, 10, 12)),
    ("I'm away until Friday.", date(2026, 10, 9)),
    ("I will be back next week.", date(2026, 10, 12)),
    ("Back on the 20th.", date(2026, 10, 20)),
    ("Back on the 5th.", date(2026, 11, 5)),
])
def test_relative_dates_use_the_message_time(text, expected):
    r = got(text)
    assert r.status == "date", r
    assert r.date == expected


def test_same_weekday_means_next_week_not_today():
    # Received on a Wednesday: "back Wednesday" is a week away.
    assert got("Back Wednesday.").date == date(2026, 10, 14)


def test_relative_dates_follow_the_operators_timezone_at_the_boundary():
    late = datetime(2026, 10, 8, 2, 30)          # 22:30 on Oct 7 in New York
    assert got("I'll be back tomorrow.", late, NY).date == date(2026, 10, 8)
    assert got("I'll be back tomorrow.", late, "UTC").date == date(2026, 10, 9)
    early = datetime(2026, 10, 7, 20, 0)         # 09:00 on Oct 8 in Auckland
    assert got("I'll be back tomorrow.", early, "Pacific/Auckland").date == date(2026, 10, 9)
    assert got("I'll be back tomorrow.", early, NY).date == date(2026, 10, 8)


def test_a_timezone_aware_received_time_is_converted():
    from datetime import timedelta, timezone

    aware = datetime(2026, 10, 7, 22, 30, tzinfo=timezone(timedelta(hours=-4)))  # 02:30 UTC Oct 8
    assert got("Back tomorrow.", aware, "UTC").date == date(2026, 10, 9)
    assert got("Back tomorrow.", aware, NY).date == date(2026, 10, 8)


# ── Spanish ──


@pytest.mark.parametrize("text,expected", [
    ("Estaré fuera de la oficina hasta el 20 de octubre.", date(2026, 10, 20)),
    ("Estare fuera de la oficina hasta el 20 de Octubre", date(2026, 10, 20)),
    ("Regreso el martes 20 de octubre de 2026.", date(2026, 10, 20)),
    ("Me encuentro de vacaciones del 12 al 20 de octubre.", date(2026, 10, 21)),
    ("Estaré ausente hasta el 20/10.", date(2026, 10, 20)),
    ("Me reincorporo el 3 de noviembre.", date(2026, 11, 3)),
    ("Volveré el lunes.", date(2026, 10, 12)),
    ("Estaré de vuelta mañana.", date(2026, 10, 8)),
    ("Regresaré en dos semanas.", date(2026, 10, 21)),
    ("Estaré ausente por dos semanas.", date(2026, 10, 21)),
    ("Estoy fuera hasta el próximo lunes.", date(2026, 10, 12)),
    ("Regreso la próxima semana.", date(2026, 10, 12)),
    ("De vacaciones hasta el 1 de enero.", date(2027, 1, 1)),
    ("Regreso el día 20.", date(2026, 10, 20)),
])
def test_spanish_dates(text, expected):
    r = got(text)
    assert r.status == "date", r
    assert r.date == expected


# ── ambiguous, invalid, past, too far, none: never guessed ──


@pytest.mark.parametrize("text", [
    "I will be back on 10/11.",                       # Oct 11 or Nov 10
    "Out until 05/06.",
    "Regreso el 03/04.",
    "I will be out of the office until January.",     # no day
    "Back next month.",
    "Regreso el próximo mes.",
    "Back on Monday, October 21.",                    # Oct 21 is a Wednesday
    "Back Oct 20, or maybe Oct 25.",                  # two different dates
    "Out until Oct 20, with limited email until Oct 15.",
])
def test_ambiguous_dates_are_reported_not_guessed(text):
    r = got(text)
    assert r.status == "ambiguous", r
    assert r.date is None


@pytest.mark.parametrize("text", [
    "Back 31/02.",
    "Until Feb 30.",
    "Back on 13/13/2026.",
    "Out until 2026-02-30.",
    "I'm out until Feb 29.",                  # 2027 is not a leap year
    "Back Feb 29, 2027.",
])
def test_impossible_dates_are_invalid(text):
    r = got(text)
    assert r.status == "invalid", r
    assert r.date is None


@pytest.mark.parametrize("text,when", [
    ("I am out of the office until October 1.", date(2026, 10, 1)),
    ("Out until September 15.", date(2026, 9, 15)),
    ("Back on October 20, 2025.", date(2025, 10, 20)),
    ("Out until 10/25/2025.", date(2025, 10, 25)),
    ("Back on October 6.", date(2026, 10, 6)),
])
def test_past_dates_need_review(text, when):
    r = got(text)
    assert r.status == "past", r
    assert r.date == when


def test_today_is_not_past():
    assert got("Back October 7.").status == "date"


def test_a_date_a_year_out_is_a_typo():
    r = got("Out until 2030-01-01.")
    assert r.status == "too_far"
    assert r.date == date(2030, 1, 1)
    assert got("Back Feb 29, 2028.").status == "too_far"       # real date, 509 days away


@pytest.mark.parametrize("text", [
    "Out of the office until further notice.",
    "I am currently out of the office. I will reply when I return.",
    "Thank you for your email. I am away from my desk.",
    "Please contact john@x.com or call 809-555-1234 until I return.",
    "Our office hours are Monday to Friday 9-5.",
    "Ref 2026/10 attached.",
    "",
])
def test_no_return_date(text):
    r = got(text)
    assert r.status == "none", r
    assert r.date is None


def test_a_date_without_a_return_cue_is_not_taken_as_one():
    # The 5th is a deadline for something else, not when they are back.
    assert got("Send the invoice by October 5 please. I am out of the office.").status == "none"


# ── leap dates and the year boundary ──


def test_leap_day_resolves_to_the_next_leap_year_that_is_near_enough():
    december = datetime(2027, 12, 20, 15, 0)
    r = got("I'm out until Feb 29.", december)
    assert r.status == "date" and r.date == date(2028, 2, 29)
    assert got("Back February 29, 2028.", december).date == date(2028, 2, 29)


def test_leap_day_in_a_common_year_is_invalid_even_with_a_year():
    assert got("Back Feb 29, 2027.", datetime(2027, 1, 10, 15, 0)).status == "invalid"


def test_a_month_day_that_already_passed_this_year_rolls_into_next_year():
    dec = datetime(2026, 12, 28, 15, 0)
    assert got("Back on January 3.", dec).date == date(2027, 1, 3)
    assert got("Regreso el 3 de enero.", dec).date == date(2027, 1, 3)
    # ...but one that passed days ago is simply past
    assert got("Back on December 20.", dec).status == "past"
    # and one only slightly behind is not turned into a date a year away
    assert got("Back on October 1.").status == "past"


# ── classification: vacation vs everything else automatic ──


@pytest.mark.parametrize("subject,body", [
    ("Automatic reply: Out of Office", "I am out of the office until October 20."),
    ("Re: quick question", "I'm currently out of the office with limited access to email."),
    ("Out of Office AutoReply", ""),
    ("Re: hi", "I will be on vacation from Oct 12 to Oct 20 and will reply when I am back."),
    ("Respuesta automática: Fuera de la oficina", "Regreso el 20 de octubre."),
    ("Re: tu nota", "Estaré de vacaciones hasta el 20 de octubre."),
    ("Re: tu nota", "Me encuentro fuera de la oficina y regreso el lunes."),
    ("Automatic reply: hi", "I'm away from my desk until Monday."),
    ("Re: hi", "Our office is closed for the holidays until January 2."),
    ("Automatic reply: Re: hi", "I am on maternity leave until March 3."),
])
def test_vacation_replies_are_recognised(subject, body):
    assert classify_auto_reply(subject, body, {"Auto-Submitted": "auto-replied"}) == "ooo"


@pytest.mark.parametrize("subject,body", [
    ("Automatic reply: your message", "Thank you for contacting Acme. Your ticket number is #48213."),
    ("Re: your note", "We have received your message and will respond within 24 hours."),
    ("Auto-reply", "Thanks for reaching out! A member of our team will get back to you."),
    ("Re: hi", "Thank you for contacting us. Our support desk is out of office on weekends. "
               "Your request has been received."),
    ("Respuesta automática", "Gracias por contactarnos. Hemos recibido su mensaje."),
    ("Newsletter", "This month at Acme."),
])
def test_acknowledgements_do_not_pause_anything(subject, body):
    assert classify_auto_reply(subject, body, {"Auto-Submitted": "auto-generated"}) == "acknowledgement"


@pytest.mark.parametrize("subject", [
    "Read: Quick question", "Delivered: hi", "Return Receipt (displayed) - hi",
    "Acuse de recibo: hola",
])
def test_receipts_are_not_vacation_replies(subject):
    assert classify_auto_reply(subject, "Your message was displayed.", {}) == "receipt"


def test_a_disposition_notification_is_a_receipt():
    headers = {"Content-Type": "multipart/report; report-type=disposition-notification"}
    assert classify_auto_reply("hi", "was read", headers) == "receipt"


# ── resume time: quiet hours, weekends, timezone ──


def test_resume_is_when_quiet_hours_end_in_the_operators_timezone():
    # Tuesday Oct 20, 07:00 EDT = 11:00 UTC
    assert resume_time(date(2026, 10, 20), NY, "07:00") == datetime(2026, 10, 20, 11, 0)
    # Santo Domingo has no DST: 07:00 AST = 11:00 UTC all year
    assert resume_time(date(2026, 10, 20), "America/Santo_Domingo", "07:00") == datetime(2026, 10, 20, 11, 0)
    assert resume_time(date(2026, 10, 20), "UTC", "09:30") == datetime(2026, 10, 20, 9, 30)


def test_a_weekend_return_resumes_on_monday():
    assert resume_time(date(2026, 10, 17), NY, "07:00") == datetime(2026, 10, 19, 11, 0)   # Saturday
    assert resume_time(date(2026, 10, 18), NY, "07:00") == datetime(2026, 10, 19, 11, 0)   # Sunday
    assert resume_time(date(2026, 10, 19), NY, "07:00") == datetime(2026, 10, 19, 11, 0)   # Monday


def test_resume_time_buffer_counts_business_days():
    # Tuesday + 1 business day = Wednesday.
    assert resume_time(date(2026, 10, 20), NY, "07:00", 1) == datetime(2026, 10, 21, 11, 0)
    # Friday + 1 skips the weekend to Monday.
    assert resume_time(date(2026, 10, 16), NY, "07:00", 1) == datetime(2026, 10, 19, 11, 0)
    # A Saturday return moves to Monday first, then the buffer adds Tuesday.
    assert resume_time(date(2026, 10, 17), NY, "07:00", 1) == datetime(2026, 10, 20, 11, 0)
    # Thursday + 2 = Monday.
    assert resume_time(date(2026, 10, 15), NY, "07:00", 2) == datetime(2026, 10, 19, 11, 0)
    # 0 and negative values mean no buffer.
    assert resume_time(date(2026, 10, 20), NY, "07:00", 0) == datetime(2026, 10, 20, 11, 0)
    assert resume_time(date(2026, 10, 20), NY, "07:00", -3) == datetime(2026, 10, 20, 11, 0)


def test_resume_time_follows_daylight_saving():
    # DST ends Sunday Nov 1 2026: Nov 3 07:00 is EST = 12:00 UTC.
    assert resume_time(date(2026, 11, 3), NY, "07:00") == datetime(2026, 11, 3, 12, 0)


def test_resume_time_tolerates_bad_input():
    assert resume_time(date(2026, 10, 20), "Not/AZone", "nope") == datetime(2026, 10, 20, 7, 0)


def test_operator_clock_reads_quiet_hours_and_tolerates_a_bare_config():
    class Quiet:
        timezone = "America/Santo_Domingo"
        end = "08:00"

    class Usage:
        quiet_hours = Quiet()

    class Cfg:
        usage = Usage()

    assert operator_clock(Cfg()) == ("America/Santo_Domingo", "08:00")
    assert operator_clock(object()) == ("UTC", "07:00")
