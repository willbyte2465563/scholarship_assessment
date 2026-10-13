"""Build the redacted view of one applicant: everything the scoring model is allowed to see.

Pure functions over a raw row (a dict of strings, as read from the CSV). Dropped entirely:
applicant_id, name, city, state, zip, high school, school type, submission time. The view
carries an opaque ref instead of the ID; intake keeps the mapping.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

REDACTION_VERSION = "1"

NUMBER_FIELDS = [
    "gpa", "gpa_scale", "test_score", "household_size", "work_hours_per_week", "community_service_hours",
]
PLAIN_FIELDS = ["intended_major", "essay_prompt"]
# Free text gets the applicant's name, high school and town replaced.
FREE_TEXT_FIELDS = ["extracurriculars", "essay_text", "recommendation_summary", "financial_need_statement"]
# Only the recommendation is about the applicant throughout; essays are full of other people.
PRONOUN_FIELDS = ["recommendation_summary"]

APPLICANT = "[applicant]"
HIGH_SCHOOL = "[high school]"
CITY = "[city]"
MIN_NAME_PART = 2  # skip initials

PRONOUNS = {
    "he": "they", "she": "they", "him": "them", "his": "their",
    "hers": "theirs", "himself": "themselves", "herself": "themselves",
}
# "her" is "their" before a noun ("her essay") and "them" otherwise ("told her that").
# These are the words that most often follow object "her". Misses are accepted in RISKS.md.
AFTER_OBJECT_HER = {
    "a", "about", "again", "an", "and", "as", "at", "back", "because", "but", "by", "for", "from",
    "how", "if", "in", "into", "on", "or", "out", "over", "so", "that", "the", "through", "to",
    "up", "what", "when", "why", "with",
}
_PRONOUN = re.compile(
    r"\b(he|she|him|his|hers|himself|herself|her)\b(?=(?:\s+([A-Za-z]+))?)", re.IGNORECASE
)
_RANK = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def fingerprint(obj) -> str:
    """SHA-256 of a canonical JSON encoding, so equal content always hashes the same."""
    encoded = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def identity_terms(row: dict) -> dict[str, str]:
    """Strings to scrub from free text, mapped to their placeholders."""
    terms = {}
    name = _clean(row.get("name"))
    if name:
        terms[name] = APPLICANT
        for part in re.split(r"[\s\-]+", name):
            if len(part.strip(".")) >= MIN_NAME_PART:
                terms[part] = APPLICANT
    for field, placeholder in (("high_school", HIGH_SCHOOL), ("city", CITY)):
        value = _clean(row.get(field))
        if value:
            terms[value] = placeholder
    return terms


def scrub(text: str, terms: dict[str, str]) -> tuple[str, int]:
    """Replace whole-word matches of each term, ignoring case. Longest first, so a full name or
    a school name like "Cedar Falls High" goes before the parts or the town inside it."""
    count = 0
    for term in sorted(terms, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        text, n = pattern.subn(terms[term], text)
        count += n
    return text, count


def neutralize_pronouns(text: str) -> tuple[str, int]:
    def swap(m: re.Match) -> str:
        word = m.group(1)
        lower = word.lower()
        if lower == "her":
            following = (m.group(2) or "").lower()
            replacement = "their" if following and following not in AFTER_OBJECT_HER else "them"
        else:
            replacement = PRONOUNS[lower]
        if word.isupper() and len(word) > 1:
            return replacement.upper()
        return replacement.capitalize() if word[0].isupper() else replacement

    return _PRONOUN.subn(swap, text)


def rank_percentile(rank: str | None) -> str | None:
    """"7/402" -> "top 2%". Rounded up, so nobody is placed higher than their rank."""
    m = _RANK.match(rank or "")
    if not m:
        return None
    position, size = int(m.group(1)), int(m.group(2))
    if size < 1 or not 1 <= position <= size:
        return None
    return f"top {max(1, math.ceil(100 * position / size))}%"


def redact(row: dict, ref: str) -> dict:
    """The view for the model, plus what intake records about how it was made."""
    terms = identity_terms(row)
    view: dict = {"ref": ref}
    for field in NUMBER_FIELDS:
        view[field] = _number(row.get(field))
    view["class_rank_percentile"] = rank_percentile(row.get("class_rank"))
    first_gen = (_clean(row.get("first_generation")) or "").upper()
    view["first_generation"] = first_gen if first_gen in ("Y", "N") else None
    for field in PLAIN_FIELDS:
        view[field] = _clean(row.get(field))

    replacements = {}
    for field in FREE_TEXT_FIELDS:
        text = _clean(row.get(field))
        count = 0
        if text is not None:
            text, count = scrub(text, terms)
            if field in PRONOUN_FIELDS:
                text, swapped = neutralize_pronouns(text)
                count += swapped
        view[field] = text
        replacements[field] = count

    content = {k: v for k, v in view.items() if k != "ref"}
    return {
        "view": view,
        "redaction_version": REDACTION_VERSION,
        "replacements": replacements,
        "raw_sha256": fingerprint(row),
        # Without the ref, which changes every run, so the same input always hashes the same.
        "view_sha256": fingerprint(content),
    }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _number(value: str | None) -> int | float | None:
    """Unparseable counts as blank, the same as intake treats it."""
    try:
        n = float(_clean(value) or "")
    except ValueError:
        return None
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n
