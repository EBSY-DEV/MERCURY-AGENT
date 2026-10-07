"""Reading vacation replies: what counts as one, and the return date it gives.

Pure functions, no database. Every date resolves against the message's own
timestamp in a given timezone, never against the machine running the test.
"""

from datetime import date, datetime

import pytest

from mercury.ooo import (
    classify_automatic, message_time, parse_return_date, resume_time,
)

# Wednesday 7 October 2026, 14:00 UTC (10:00 in New York).
SENT = datetime(2026, 10, 7, 14, 0)
NY = "America/New_York"


def back(text, sent=SENT, tz=NY):
    return parse_return_date(text, sent, tz)


# ── Which machine wrote it ──


def test_vacation_reply_is_told_apart_from_receipts_and_acknowledgements():
    auto = {"Auto-Submitted": "auto-replied"}
    assert classify_automatic("Automatic reply: hi", "I'm away until Monday.", auto) == "out_of_office"
    assert classify_automatic("Respuesta automática", "Estoy de vacaciones.", auto) == "out_of_office"
    assert classify_automatic("Read: hi", "Your message was read on Monday.", {}) == "receipt"
    assert classify_automatic("Delivered: hi", "", {}) == "receipt"
    assert classify_automatic("Re: hi", "We have received your message and will reply soon.",
                              auto) == "acknowledgement"


def test_an_auto_submitted_header_alone_does_not_make_a_vacation_reply():
    # A ticket system's "thanks, we got it" carries the same header.
    assert classify_automatic("Re: hi", "Thanks for writing to example.com support.",
                              {"Auto-Submitted": "auto-generated"}) == "acknowledgement"


def test_a_receipt_about_an_email_on_vacations_is_still_a_receipt():
    assert classify_automatic("Read: Out of office plans", "Your message was read.", {}) == "receipt"


def test_a_person_writing_about_their_vacation_is_not_a_machine():
    # No automatic headers or subject: this goes to the reply classifier.
    assert classify_automatic("Re: hi", "I'm on vacation until the 20th but interested!", {}) is None


# ── Absolute dates ──


@pytest.mark.parametrize("text, expected", [
    ("I'm away until October 20.", date(2026, 10, 20)),
    ("Out of the office until Oct. 20th, 2026 with limited access to email.", date(2026, 10, 20)),
    ("I'll be back on Monday, October 19.", date(2026, 10, 19)),
    ("I will return 2026-10-26.", date(2026, 10, 26)),
    ("Back on 10/20.", date(2026, 10, 20)),
    ("I am out of the office from October 13 until October 20.", date(2026, 10, 20)),
    ("Out of office October 13-16, returning October 19.", date(2026, 10, 19)),
    ("On October 20 I'll be back in the office.", date(2026, 10, 20)),
    # A range is inclusive: out through the 16th means back on the 17th.
    ("Out from Oct 9 to Oct 16.", date(2026, 10, 17)),
])
def test_english_absolute_dates(text, expected):
    r = back(text)
    assert r.review_state == "scheduled", r
    assert r.local_date == expected
    assert r.text and r.text in text


@pytest.mark.parametrize("text, expected", [
    ("Estaré fuera de la oficina hasta el 20 de octubre.", date(2026, 10, 20)),
    ("Regresaré el 26 de octubre de 2026.", date(2026, 10, 26)),
    ("Volveré el 20/10/2026.", date(2026, 10, 20)),
    ("No estaré disponible hasta el lunes 19 de octubre.", date(2026, 10, 19)),
    ("Fuera de la oficina del 13 al 17 de octubre.", date(2026, 10, 18)),
])
def test_spanish_absolute_dates(text, expected):
    r = back(text)
    assert r.review_state == "scheduled", r
    assert r.local_date == expected


def test_a_date_early_next_year_rolls_into_next_year():
    r = back("Back January 5.", sent=datetime(2026, 12, 20, 12))
    assert r.local_date == date(2027, 1, 5)


# ── Relative dates, against the message's own day ──


@pytest.mark.parametrize("text, expected", [
    ("Back next Monday.", date(2026, 10, 12)),
    ("Back on Monday.", date(2026, 10, 12)),
    ("Out until the 20th.", date(2026, 10, 20)),
    ("Back tomorrow.", date(2026, 10, 8)),
    ("I'm on vacation through Friday.", date(2026, 10, 10)),
    ("Regreso el lunes.", date(2026, 10, 12)),
    ("Regreso el próximo lunes por la mañana.", date(2026, 10, 12)),
    ("Hasta el 20, gracias.", date(2026, 10, 20)),
    ("Vuelvo mañana.", date(2026, 10, 8)),
    ("Estaré de vuelta la próxima semana.", date(2026, 10, 12)),
])
def test_relative_dates(text, expected):
    r = back(text)
    assert r.review_state == "scheduled", r
    assert r.local_date == expected


def test_a_day_of_month_already_passed_means_next_month():
    assert back("Out until the 3rd.").local_date == date(2026, 11, 3)


def test_the_same_weekday_means_next_week_not_today():
    # SENT is a Wednesday.
    assert back("Back Wednesday.").local_date == date(2026, 10, 14)


def test_tomorrow_is_read_in_the_configured_timezone():
    # 02:30 UTC on the 8th is still the evening of the 7th in New York.
    late = datetime(2026, 10, 8, 2, 30)
    assert back("Back tomorrow.", sent=late, tz=NY).local_date == date(2026, 10, 8)
    assert back("Back tomorrow.", sent=late, tz="UTC").local_date == date(2026, 10, 9)


def test_the_resume_time_is_morning_local_across_a_dst_change():
    # New York leaves daylight time on 1 November 2026.
    assert resume_time(date(2026, 10, 30), NY) == datetime(2026, 10, 30, 13, 0)
    assert resume_time(date(2026, 11, 2), NY) == datetime(2026, 11, 2, 14, 0)
    assert back("Back October 20.").resume_at == datetime(2026, 10, 20, 13, 0)


# ── What goes to review instead of being guessed ──


@pytest.mark.parametrize("text, reason", [
    ("Back on 05/10.", "ambiguous"),            # 10 May or 5 October
    ("Regreso el 03/04/2027.", "ambiguous"),
    ("Back February 30.", "invalid"),
    ("I was out until October 1.", "past"),
    ("I am currently out of the office.", "no_date"),
    ("Our support team is available 24/7.", "no_date"),
    ("Out next week, back the week after.", "unclear"),
    ("Back October 20 or October 22.", "conflicting"),
    ("Back on October 20, 2028.", "too_far"),
])
def test_unreadable_dates_need_review(text, reason):
    r = back(text)
    assert r.review_state == "needs_review"
    assert r.resume_at is None and r.local_date is None
    assert r.review_reason == reason


def test_an_ambiguous_date_keeps_the_text_for_the_operator():
    assert back("Back on 05/10.").text == "05/10"


def test_the_same_numbers_both_ways_are_not_ambiguous():
    assert back("Back on 10/10.").local_date == date(2026, 10, 10)


# ── Leap days ──


def test_february_29_resolves_only_to_a_leap_year():
    assert back("Back Feb 29.", sent=datetime(2027, 12, 20, 12)).local_date == date(2028, 2, 29)
    assert back("Back February 29, 2028.",
                sent=datetime(2028, 1, 10, 12)).local_date == date(2028, 2, 29)
    assert back("Back Feb 29.", sent=datetime(2026, 12, 20, 12)).review_reason == "invalid"
    assert back("Back February 29, 2027.").review_reason == "invalid"


# ── The message timestamp ──


def test_message_time_reads_the_date_header_as_utc():
    assert message_time("Wed, 07 Oct 2026 22:30:00 -0400", SENT) == datetime(2026, 10, 8, 2, 30)
    assert message_time("not a date", SENT) == SENT
    assert message_time("", SENT) == SENT
