"""Deterministic draft rules: word counting, limits, greetings, names, reviews.

Synthetic only: fictional businesses on example.com.
"""

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mercury.config import MercuryConfig
from mercury.draft_rules import (
    DEFAULT_WORD_LIMITS,
    FLAG_GENERIC_GREETING,
    FLAG_OVER_LIMIT,
    apply_short_name,
    count_words,
    decode_flags,
    draft_flags,
    drop_greeting,
    encode_flags,
    has_generic_greeting,
    is_generic_greeting_line,
    mentions_review_claim,
    recheck_flags,
    strip_generic_greeting,
    strip_review_claims,
    word_limit,
    word_limits,
)
from mercury.registry.base import name_variants, normalize_business_name, short_business_name

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"


def config(**writer) -> MercuryConfig:
    data = yaml.safe_load(TEMPLATE.read_text())
    if writer:
        data["writer"] = copy.deepcopy(writer)
    return MercuryConfig(**data)


# ── Counting ──

def test_count_includes_greeting_and_sign_off_but_not_symbols():
    body = "Hi Pat,\n\nYour quote form takes three days. How do you track it?\n\nSam"
    assert count_words(body) == 14
    # A lone dash, bullet or ampersand is not a word; a hyphenated word is one.
    assert count_words("one - two & three * four follow-up don't") == 6
    assert count_words("") == 0 and count_words(None) == 0


def test_a_merge_variable_is_one_word_even_with_spaces():
    assert count_words("Hi {{first_name}}, {{ company }} is next.") == 5


def test_numbers_and_accents_count():
    assert count_words("Hola equipo, 3 años de ¿cotización?") == 6


# ── Limits ──

def test_default_limits_and_per_step_override():
    assert DEFAULT_WORD_LIMITS == {1: 90, 2: 80, 3: 50}
    assert word_limits(config()) == {1: 90, 2: 80, 3: 50}
    custom = config(word_limits={2: 60})
    assert word_limits(custom) == {1: 90, 2: 60, 3: 50}
    assert [word_limit(custom, n) for n in (1, 2, 3, 4)] == [90, 60, 50, 50]


@pytest.mark.parametrize("limits, message", [
    ({0: 50}, "numbered 1 to"),
    ({1: 5}, "between 10 and 500"),
    ({2: 900}, "between 10 and 500"),
])
def test_invalid_word_limits_fail_clearly(limits, message):
    with pytest.raises(ValidationError, match=message):
        config(word_limits=limits)


def test_a_config_object_without_writer_uses_the_defaults():
    assert word_limit(object(), 2) == 80


# ── Flags ──

def test_over_limit_flag_counts_the_whole_body():
    body = " ".join(["word"] * 88) + "\n\nSam"
    assert count_words(body) == 89
    assert draft_flags(body, 90) == []
    assert draft_flags("Hi Pat, " + body, 90) == [FLAG_OVER_LIMIT]
    assert draft_flags(body, 0) == []  # no limit: a reply


def test_flags_round_trip_and_empty_is_blank():
    assert encode_flags([]) == "" and decode_flags("") == []
    stored = encode_flags([FLAG_OVER_LIMIT, FLAG_GENERIC_GREETING, FLAG_OVER_LIMIT])
    assert decode_flags(stored) == [FLAG_GENERIC_GREETING, FLAG_OVER_LIMIT]
    assert decode_flags("not json") == []


def test_recheck_measures_the_limit_again_and_never_invents_a_greeting_flag():
    long = " ".join(["word"] * 100)
    assert recheck_flags(long, 90, []) == [FLAG_OVER_LIMIT]
    assert recheck_flags("short", 90, [FLAG_OVER_LIMIT]) == []
    assert recheck_flags("Hi there,\nshort", 90, []) == []
    assert recheck_flags("Hi there,\nshort", 90, [FLAG_GENERIC_GREETING]) == [FLAG_GENERIC_GREETING]
    assert recheck_flags("Hi Pat,\nshort", 90, [FLAG_GENERIC_GREETING]) == []


# ── Generic greetings ──

@pytest.mark.parametrize("line", [
    "Hi there,", "Hello team,", "Hello,", "Hi,", "Hey there!", "Hola,", "Hola equipo,",
    "Hola, equipo de Example Shop,", "Hello Example Shop team,", "Dear sir or madam,",
    "To whom it may concern,", "Good morning,", "Saludos, equipo de Example Shop", "Hi all,",
])
def test_generic_greetings_are_recognised(line):
    assert is_generic_greeting_line(line), line


@pytest.mark.parametrize("line", [
    "Hi Pat,", "Dear Maria,", "Buenas, don Rafael", "Hola Rafael,", "Pat, a quick note about quotes.",
    "Your quote form takes three days.", "",
])
def test_a_greeting_by_name_is_not_generic(line):
    assert not is_generic_greeting_line(line), line


def test_a_greeting_that_names_the_business_is_generic():
    assert is_generic_greeting_line("Hi Example Shop,", ["Example Shop"])
    assert not is_generic_greeting_line("Hi Example Shop,")


def test_strip_removes_a_greeting_line_or_an_inline_greeting():
    assert strip_generic_greeting("Hi there,\n\nYour form takes days.\nSam") == ("Your form takes days.\nSam", "Hi there,")
    body, removed = strip_generic_greeting("Hello team, your form takes days. Sam")
    assert body == "Your form takes days. Sam" and removed == "Hello team,"
    assert strip_generic_greeting("Hi Pat,\nYour form takes days.") == ("Hi Pat,\nYour form takes days.", "")
    # A body that would be left empty is not touched.
    assert strip_generic_greeting("Hello there") == ("Hello there", "")
    assert has_generic_greeting("Hi there,\nx") and not has_generic_greeting("Hi Pat,\nx")


def test_drop_greeting_removes_a_template_greeting_line_only():
    assert drop_greeting("Hi {{first_name}},\n\nBody here.") == "Body here."
    assert drop_greeting("Hola {{first_name}}!\nBody") == "Body"
    assert drop_greeting("Body only.\nMore.") == "Body only.\nMore."
    # A sentence that merely starts with a greeting word stays.
    assert drop_greeting("Hello world is the oldest program there is.") == "Hello world is the oldest program there is."


# ── Business names ──

@pytest.mark.parametrize("full, short", [
    ("Acme Roofing LLC - Springfield", "Acme Roofing"),
    ("Example Bakery, Inc.", "Example Bakery"),
    ("Sample Plumbing | Riverton", "Sample Plumbing"),
    ("Fictional Tiles L.L.C.", "Fictional Tiles"),
    ("Delta Garage (Riverton)", "Delta Garage"),
    ("Beta Florist PLLC – Northfield", "Beta Florist"),
    ("Smith & Sons Co.", "Smith & Sons"),
    ("Example Tailor Ltd", "Example Tailor"),
    ("Gamma Cafe Corp - Downtown", "Gamma Cafe"),
    ("Example Tailor", "Example Tailor"),
    ("The Example Cafe Company", "The Example Cafe"),
    ("Inc", "Inc"),
])
def test_short_business_name_strips_legal_suffixes_and_locations(full, short):
    assert short_business_name(full) == short


def test_short_name_and_the_registry_key_share_one_implementation():
    for name in ("Acme Roofing LLC - Springfield", "Example Bakery, Inc.", "Delta Garage (Riverton)",
                 "Beta Florist PLLC – Northfield", "Smith & Sons Co."):
        assert normalize_business_name(name) == normalize_business_name(short_business_name(name))
    assert name_variants("Acme Roofing LLC - Springfield") == ["Acme Roofing LLC"]
    assert name_variants("Acme Roofing LLC") == []


def test_apply_short_name_keeps_the_full_name_once_and_never_in_the_subject():
    full = "Acme Roofing LLC - Springfield"
    subject, body = apply_short_name(
        "acme roofing llc quotes",
        "Acme Roofing LLC - Springfield posts slowly. Acme Roofing LLC again, and acme roofing llc once more.",
        full, short_business_name(full), name_variants(full))
    assert subject == "Acme Roofing quotes"
    assert body.count("Acme Roofing LLC") == 1
    assert body.count("Acme Roofing") == 3
    # Nothing to do when the name has nothing to strip.
    assert apply_short_name("a note", "Example Tailor again. Example Tailor.", "Example Tailor",
                            "Example Tailor") == ("a note", "Example Tailor again. Example Tailor.")


# ── Review counts and ratings ──

@pytest.mark.parametrize("text", [
    "established and busy (72 Google reviews, 4.7) — context only, never the opener",
    "a 4.8 star rating", "4.8 stars", "120 reviews", "rated 4.9 by customers", "72 reseñas",
    "reviews: 120", "5-star service", "star rating of the shop", "un 4.7 con 72 reseñas",
])
def test_review_claims_are_detected(text):
    assert mentions_review_claim(text), text


@pytest.mark.parametrize("text", [
    "pays an agency", "customers can leave reviews online", "the site has 40 pages",
    "blog abandoned", "open since 1998", "five trucks on the road",
])
def test_other_facts_are_not_review_claims(text):
    assert not mentions_review_claim(text), text


def test_strip_review_claims_drops_the_sentence_and_keeps_the_rest():
    note = ("Pays for search ads. established and busy (72 Google reviews, 4.7) — context only. "
            "No online booking. Rated 4.8 stars.")
    assert strip_review_claims(note) == "Pays for search ads. No online booking."
    assert strip_review_claims("72 reviews") == ""
    assert strip_review_claims("") == ""
