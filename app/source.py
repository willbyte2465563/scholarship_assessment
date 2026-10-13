"""The applicant CSV: what each column should hold, and how it's loaded into DuckDB.

Shared by the profiler and intake so both read the file the same way.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import duckdb

ID = "applicant_id"
BLANK_ID = "(blank id)"  # stands in for a missing applicant_id so the row still shows up in lists

# What each column should hold. Checks key off this, not off DuckDB's inference.
SCHEMA = {
    "applicant_id": "id",
    "name": "label",
    "city": "label",
    "state": "label",
    "zip": "zip",
    "high_school": "label",
    "school_type": "label",
    "gpa": "number",
    "gpa_scale": "number",
    "class_rank": "rank",
    "test_score": "number",
    "intended_major": "label",
    "household_size": "number",
    "first_generation": "yes_no",
    "work_hours_per_week": "number",
    "extracurriculars": "text",
    "community_service_hours": "number",
    "essay_prompt": "label",
    "essay_text": "text",
    "recommendation_summary": "text",
    "financial_need_statement": "text",
    "submitted_at": "timestamp",
}

# SQL that is true when a non-blank value parses as its kind. Kinds not listed always parse.
PARSES = {
    "number": "TRY_CAST({c} AS DOUBLE) IS NOT NULL",
    "rank": r"regexp_full_match(trim({c}), '\d+\s*/\s*\d+')",
    "yes_no": "upper(trim({c})) IN ('Y', 'N')",
    "timestamp": "TRY_CAST({c} AS TIMESTAMP) IS NOT NULL",
    "zip": r"regexp_full_match(trim({c}), '\d{{5}}(-\d{{4}})?')",
}

TEXT_COLUMNS = [c for c, kind in SCHEMA.items() if kind == "text"]
# Free text that reaches the LLM, so it gets scanned for instructions.
LLM_TEXT_COLUMNS = TEXT_COLUMNS + ["intended_major"]
# Fields that, together, describe an applicant's numbers. Groups matching on all of them are reported.
PROFILE_COLUMNS = [
    "gpa", "gpa_scale", "class_rank", "test_score", "household_size",
    "first_generation", "work_hours_per_week", "community_service_hours",
]
IDENTITY_COLUMNS = ["city", "state", "zip", "high_school"]


def blank(c: str) -> str:
    return f"({c} IS NULL OR trim({c}) = '')"


def unparseable(c: str) -> str | None:
    rule = PARSES.get(SCHEMA[c])
    return None if rule is None else f"(NOT {blank(c)} AND NOT ({rule.format(c=c)}))"


def ids(con: duckdb.DuckDBPyConnection, where: str, params: list | None = None) -> list[str]:
    row = con.execute(
        f"SELECT list({ID} ORDER BY {ID}) FROM typed WHERE {where}", params or []
    ).fetchone()
    return row[0] or []


def rounded(v):
    return round(v, 3) if isinstance(v, float) else v


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(con: duckdb.DuckDBPyConnection, path: Path) -> dict:
    """Read every value as text, so nothing is coerced before we've looked at it.

    The dialect and columns are given rather than sniffed (columns come from the header line),
    so malformed lines get rejected and reported instead of making the sniffer give up.
    Creates the `raw` table and the `typed` view the checks run against.
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    con.execute(
        """
        CREATE TABLE raw AS SELECT * FROM read_csv(
            ?, header = true, delim = ',', quote = '"', escape = '"',
            columns = ?, store_rejects = true)
        """,
        [str(path), {c: "VARCHAR" for c in header}],
    )
    parse_errors = con.execute(
        # One reject per bad column, so group to one entry per line.
        "SELECT line, string_agg(DISTINCT error_type, ', ') FROM reject_errors GROUP BY line ORDER BY line"
    ).fetchall()
    present = [r[0] for r in con.execute("DESCRIBE raw").fetchall()]
    if ID not in present:
        raise ValueError(f"no {ID} column; can't report anything by ID")
    missing = [c for c in SCHEMA if c not in present]
    # Missing columns are added as all-blank so every check reports them instead of crashing.
    for c in missing:
        con.execute(f"ALTER TABLE raw ADD COLUMN {c} VARCHAR")
    try:
        inferred = {
            r[0]: r[1]
            for r in con.execute("DESCRIBE SELECT * FROM read_csv(?)", [str(path)]).fetchall()
        }
    except duckdb.Error as e:
        inferred = {"_error": type(e).__name__}
    con.execute(
        rf"""
        CREATE VIEW typed AS SELECT * REPLACE (coalesce(nullif(trim({ID}), ''), '{BLANK_ID}') AS {ID}),
            TRY_CAST(gpa AS DOUBLE) AS gpa_n,
            TRY_CAST(gpa_scale AS DOUBLE) AS gpa_scale_n,
            TRY_CAST(regexp_extract(class_rank, '^\s*(\d+)\s*/\s*(\d+)\s*$', 1) AS BIGINT) AS rank_pos,
            TRY_CAST(regexp_extract(class_rank, '^\s*(\d+)\s*/\s*(\d+)\s*$', 2) AS BIGINT) AS rank_size,
            TRY_CAST(test_score AS DOUBLE) AS test_score_n,
            TRY_CAST(household_size AS DOUBLE) AS household_size_n,
            TRY_CAST(work_hours_per_week AS DOUBLE) AS work_hours_n,
            TRY_CAST(community_service_hours AS DOUBLE) AS service_hours_n,
            lower(trim(regexp_replace(name, '\s+', ' ', 'g'))) AS name_key,
            lower(trim(regexp_replace(high_school, '\s+', ' ', 'g'))) AS school_key
        FROM raw
        """
    )
    return {
        "rows": con.execute("SELECT count(*) FROM typed").fetchone()[0],
        "parse_errors": [{"line": line, "error": err} for line, err in parse_errors],
        "missing_columns": missing,
        "unexpected_columns": [c for c in present if c not in SCHEMA],
        "inferred_types": inferred,
    }
