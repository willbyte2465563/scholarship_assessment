"""Data checks over the `typed` view, each returning applicant IDs.

The profiler reports these; intake turns them into holds and flags. One definition of each rule.
"""

from __future__ import annotations

import duckdb

from app.source import (
    BLANK_ID, ID, IDENTITY_COLUMNS, LLM_TEXT_COLUMNS, PROFILE_COLUMNS, SCHEMA, TEXT_COLUMNS,
    blank, ids, rounded, unparseable,
)

# Under half the column's median word count counts as short. For essays, that's also
# intake's truncation rule; punctuation and whitespace signals were too weak to keep.
SHORT_TEXT_RATIO = 0.5
SHINGLE_WORDS = 5
NEAR_DUPLICATE_JACCARD = 0.5
COMMON_SHINGLE_DOCS = 50  # shingles in more docs than this are boilerplate and don't count

INSTRUCTION_PATTERNS = {
    "override": (
        r"\b(ignore|disregard|forget|override)\b.{0,40}"
        r"\b(instructions?|rubric|prompt|rules|above|previous|prior)\b"
    ),
    "score_demand": (
        r"\b(give|assign|award|rate)\b.{0,30}\b(this (applicant|student|candidate)|me|them|him|her)\b"
        r".{0,30}\b(maximum|max|highest|perfect|full|top|10)\b"
    ),
    "addressed_to_model": (
        r"\b(language model|llm|chatgpt|you are an ai|as an ai|"
        r"ai (reviewer|grader|scorer|system)|note to the (ai|grader|scorer|model))\b"
    ),
    "prompt_markup": r"(?m)(</?(system|assistant|user|instructions?)>|\[/?inst\]|<\|[a-z_]+\|>|```|^\s*(system|assistant)\s*:)",
    "hidden_characters": r"[\x{200B}-\x{200F}\x{2028}\x{2029}\x{2060}\x{FEFF}]",
}

# (name, rule in words, failure SQL over the typed view, columns that must be usable for it to run)
SANITY_CHECKS = [
    ("rank_within_class", "class rank is between 1 and class size",
     "rank_pos < 1 OR rank_size < 1 OR rank_pos > rank_size", ["class_rank"]),
    ("gpa_within_scale", "GPA is between 0 and its scale, and the scale is positive",
     "gpa_n < 0 OR gpa_scale_n <= 0 OR gpa_n > gpa_scale_n", ["gpa", "gpa_scale"]),
    ("test_score_plausible", "test score is a whole number in the ACT (1-36) or SAT (400-1600) range",
     "test_score_n <> floor(test_score_n) OR NOT (test_score_n BETWEEN 1 AND 36 OR test_score_n BETWEEN 400 AND 1600)",
     ["test_score"]),
    ("household_size_valid", "household size is a whole number of at least 1",
     "household_size_n < 1 OR household_size_n <> floor(household_size_n)", ["household_size"]),
    ("work_hours_valid", "work hours per week are between 0 and 168",
     "work_hours_n < 0 OR work_hours_n > 168", ["work_hours_per_week"]),
    ("service_hours_valid", "community service hours are not negative",
     "service_hours_n < 0", ["community_service_hours"]),
]


def text_lengths(con: duckdb.DuckDBPyConnection) -> dict:
    out = {}
    for c in TEXT_COLUMNS:
        words = f"len(string_split(trim(regexp_replace({c}, '\\s+', ' ', 'g')), ' '))"
        stats = con.execute(
            f"""
            SELECT min(w), quantile_cont(w, 0.05), median(w), quantile_cont(w, 0.95), max(w), median(chars)
            FROM (SELECT {words} AS w, length({c}) AS chars FROM typed WHERE NOT {blank(c)})
            """
        ).fetchone()
        keys = ["min", "p05", "median", "p95", "max"]
        entry = {"words": {k: rounded(v) for k, v in zip(keys, stats[:5])}, "median_chars": stats[5]}
        median = stats[2]
        entry["short_ids"] = (
            ids(con, f"NOT {blank(c)} AND {words} < ?", [median * SHORT_TEXT_RATIO]) if median else []
        )
        out[c] = entry
    return out


def sanity_checks(con: duckdb.DuckDBPyConnection) -> list[dict]:
    results = []
    for name, rule, fails, inputs in SANITY_CHECKS:
        could_not_run = {}
        usable = []
        for c in inputs:
            could_not_run[f"{c} blank"] = ids(con, blank(c))
            bad = unparseable(c)
            if bad:
                could_not_run[f"{c} unparseable"] = ids(con, bad)
            usable.append(f"NOT {blank(c)}" + (f" AND NOT {bad}" if bad else ""))
        results.append({
            "check": name,
            "rule": rule,
            "failed_ids": ids(con, " AND ".join(usable) + f" AND ({fails})"),
            "could_not_run": {k: v for k, v in could_not_run.items() if v},
        })
    # The same school should report one class size. Keyed on name, city and state, since
    # names like "Central High School" repeat across towns. Only IDs come out.
    groups = con.execute(
        """
        SELECT list(applicant_id ORDER BY applicant_id) FROM typed
        WHERE school_key <> '' AND rank_size IS NOT NULL
        GROUP BY school_key, lower(trim(city)), upper(trim(state)) HAVING count(DISTINCT rank_size) > 1
        ORDER BY 1
        """
    ).fetchall()
    results.append({
        "check": "class_size_consistent_per_school",
        "rule": "applicants from the same high school report the same class size",
        "failed_ids": sorted(i for (group,) in groups for i in group),
        "failed_groups": [group for (group,) in groups],
        "could_not_run": {
            k: v for k, v in {
                "high_school blank": ids(con, blank("high_school")),
                "class_rank blank or unparseable": ids(con, "rank_size IS NULL"),
            }.items() if v
        },
    })
    return results


def duplicates(con: duckdb.DuckDBPyConnection) -> dict:
    dup_ids = con.execute(
        f"SELECT {ID} FROM typed WHERE {ID} <> '{BLANK_ID}' GROUP BY {ID} HAVING count(*) > 1 ORDER BY 1"
    ).fetchall()
    non_id = [c for c in SCHEMA if c != ID]
    exact = con.execute(
        f"SELECT list({ID} ORDER BY {ID}) FROM typed GROUP BY {', '.join(non_id)} HAVING count(*) > 1 ORDER BY 1"
    ).fetchall()
    same_name = con.execute(
        f"""
        SELECT a.{ID}, b.{ID}, [{', '.join(f"CASE WHEN a.{c} IS NOT DISTINCT FROM b.{c} THEN '{c}' END" for c in IDENTITY_COLUMNS)}]
        FROM typed a JOIN typed b ON a.name_key = b.name_key AND a.{ID} < b.{ID}
        WHERE a.name_key <> ''
        ORDER BY 1, 2
        """
    ).fetchall()
    profiles = con.execute(
        f"""
        SELECT list({ID} ORDER BY {ID}), count(DISTINCT name_key) = 1, count(DISTINCT essay_text) = 1
        FROM typed GROUP BY {', '.join(PROFILE_COLUMNS)} HAVING count(*) > 1 ORDER BY 1
        """
    ).fetchall()
    return {
        "duplicate_applicant_ids": [r[0] for r in dup_ids],
        "exact_copies_except_id": [r[0] for r in exact],
        "same_name": [
            {"ids": [a, b], "also_matching": [m for m in matched if m]} for a, b, matched in same_name
        ],
        "same_numbers": [
            {"ids": group, "same_name": n, "same_essay": e} for group, n, e in profiles
        ],
        "near_identical": {c: near_identical(con, c) for c in ("essay_text", "recommendation_summary")},
    }


def near_identical(con: duckdb.DuckDBPyConnection, c: str) -> dict:
    """Jaccard similarity on word shingles. Only pairs sharing a shingle are compared, so it isn't n^2."""
    k = SHINGLE_WORDS
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE words AS
        SELECT {ID} AS id, string_split(trim(regexp_replace(lower({c}), '[^a-z0-9]+', ' ', 'g')), ' ') AS w
        FROM typed WHERE NOT {blank(c)}
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE shingles AS
        WITH starts AS (SELECT id, w, unnest(range(1, len(w) - {k} + 2)) AS i FROM words WHERE len(w) >= {k}),
        all_shingles AS (SELECT DISTINCT id, hash(array_to_string(w[i:i + {k} - 1], ' ')) AS h FROM starts),
        common AS (SELECT h FROM all_shingles GROUP BY h HAVING count(*) > {COMMON_SHINGLE_DOCS})
        SELECT * FROM all_shingles WHERE h NOT IN (SELECT h FROM common)
        """
    )
    pairs = con.execute(
        f"""
        WITH sizes AS (SELECT id, count(*) AS n FROM shingles GROUP BY id),
        shared AS (
            SELECT a.id AS a, b.id AS b, count(*) AS n
            FROM shingles a JOIN shingles b ON a.h = b.h AND a.id < b.id
            GROUP BY 1, 2
        )
        SELECT shared.a, shared.b, shared.n / (sa.n + sb.n - shared.n) AS jaccard
        FROM shared JOIN sizes sa ON sa.id = shared.a JOIN sizes sb ON sb.id = shared.b
        WHERE shared.n / (sa.n + sb.n - shared.n) >= {NEAR_DUPLICATE_JACCARD}
        ORDER BY 1, 2
        """
    ).fetchall()
    return {
        "pairs": [{"ids": [a, b], "jaccard": rounded(j)} for a, b, j in pairs],
        "could_not_run": {
            reason: found for reason, found in {
                f"{c} blank": ids(con, blank(c)),
                f"{c} under {k} words": sorted(
                    r[0] for r in con.execute(f"SELECT id FROM words WHERE len(w) < {k}").fetchall()
                ),
            }.items() if found
        },
    }


def instruction_like(con: duckdb.DuckDBPyConnection) -> list[dict]:
    queries, params = [], []
    for c in LLM_TEXT_COLUMNS:
        for category, pattern in INSTRUCTION_PATTERNS.items():
            queries.append(f"SELECT {ID}, '{c}', '{category}' FROM typed WHERE regexp_matches({c}, ?, 'i')")
            params.append(pattern)
    rows = con.execute(" UNION ALL ".join(queries) + " ORDER BY 1, 2, 3", params).fetchall()
    return [{"id": i, "column": c, "category": cat} for i, c, cat in rows]
