import csv
import re
from pathlib import Path

import pytest

from app.redaction import (
    APPLICANT, CITY, HIGH_SCHOOL, identity_terms, neutralize_pronouns, rank_percentile, redact, scrub,
)
from tests.conftest import make_row

REAL_CSV = Path("data/raw/scholarship_candidates.csv")
DROPPED = {"applicant_id", "name", "city", "state", "zip", "high_school", "school_type", "submitted_at"}


def test_scrub_replaces_name_whole_words_any_case():
    terms = identity_terms({"name": "Grace Whitfield"})
    text, count = scrub("GRACE said Whitfield's plan worked gracefully. Grace Whitfield agreed.", terms)
    assert text == f"{APPLICANT} said {APPLICANT}'s plan worked gracefully. {APPLICANT} agreed."
    assert count == 3


def test_scrub_replaces_school_before_the_town_inside_it():
    terms = identity_terms({"high_school": "Cedar Falls High", "city": "Cedar Falls"})
    text, _ = scrub("At Cedar Falls High, and around Cedar Falls.", terms)
    assert text == f"At {HIGH_SCHOOL}, and around {CITY}."


def test_hyphenated_names_and_initials():
    terms = identity_terms({"name": "Wei-Lin J. Chen"})
    assert {"Wei-Lin J. Chen", "Wei", "Lin", "Chen"} <= set(terms)
    assert "J." not in terms


@pytest.mark.parametrize("text, expected", [
    ("She is careful. He was late.", "They is careful. They was late."),
    ("Her essay moved him.", "Their essay moved them."),
    ("I told her that it mattered, and thanked her.", "I told them that it mattered, and thanked them."),
    ("The idea was hers; she taught herself.", "The idea was theirs; they taught themselves."),
    ("HE is the shepherd of these thesis themes.", "THEY is the shepherd of these thesis themes."),
])
def test_neutralize_pronouns(text, expected):
    assert neutralize_pronouns(text)[0] == expected


@pytest.mark.parametrize("rank, expected", [
    ("2/318", "top 1%"),
    ("88/412", "top 22%"),
    (" 7 / 402 ", "top 2%"),
    ("402/402", "top 100%"),
    ("500/400", None),
    ("0/100", None),
    ("top ten", None),
    ("", None),
    (None, None),
])
def test_rank_percentile(rank, expected):
    assert rank_percentile(rank) == expected


def test_view_drops_identity_fields_and_keeps_scoring_inputs():
    r = redact(make_row("A-001", 1, class_rank="7/402", first_generation="y"), ref="r-1")
    view = r["view"]
    assert not DROPPED & set(view)
    assert view["ref"] == "r-1"
    assert view["class_rank_percentile"] == "top 2%"
    assert view["first_generation"] == "Y"
    assert view["gpa"] == 3.5 and view["household_size"] == 4


def test_blank_and_unparseable_values_are_none():
    view = redact(make_row("A-001", 1, gpa="", test_score="n/a", first_generation="maybe"), ref="r")["view"]
    assert view["gpa"] is None and view["test_score"] is None and view["first_generation"] is None


def test_pronouns_change_in_the_recommendation_only():
    row = make_row("A-001", 1, essay_text="She is my sister.", recommendation_summary="She is my student.")
    r = redact(row, ref="r")
    assert r["view"]["essay_text"] == "She is my sister."
    assert r["view"]["recommendation_summary"] == "They is my student."


def test_record_counts_replacements_per_field_and_hashes():
    row = make_row(
        "A-001", 1, name="Ana Ruiz", city="Tulsa", high_school="Tulsa East",
        essay_text="Ana grew up in Tulsa.", recommendation_summary="Ana leads. She listens.",
        financial_need_statement="Single parent.",
    )
    r = redact(row, ref="r-1")
    assert r["view"]["essay_text"] == f"{APPLICANT} grew up in {CITY}."
    assert r["replacements"] == {
        "extracurriculars": 0, "essay_text": 2, "recommendation_summary": 2, "financial_need_statement": 0,
    }
    assert r["redaction_version"] == "1"
    again = redact(row, ref="r-2")
    assert again["view_sha256"] == r["view_sha256"]  # the ref doesn't change the content hash
    assert again["raw_sha256"] == r["raw_sha256"]
    assert redact({**row, "essay_text": "Different."}, ref="r-1")["view_sha256"] != r["view_sha256"]


def test_no_identity_strings_survive_in_any_real_view():
    with REAL_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows
    for row in rows:
        view = redact(row, ref="r")["view"]
        text = " ".join(str(v) for v in view.values() if v is not None)
        for term in identity_terms(row):
            assert not re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE), (
                row["applicant_id"], term,
            )
