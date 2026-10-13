"""Profile the applicant CSV with DuckDB.

Writes a markdown report and a JSON file with the same stats. Output holds
counts, column names and applicant IDs only, never raw text or personal info.
Everything is computed in SQL so the CSV is never loaded row by row into Python.

    python -m app.profiling data/raw/scholarship_candidates.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
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

# Only these columns have their values shown in the report. Everything else is counts and IDs.
SHOW_VALUES = ("school_type", "first_generation", "gpa_scale")

TEXT_COLUMNS = [c for c, kind in SCHEMA.items() if kind == "text"]
# Free text that reaches the LLM, so it gets scanned for instructions.
LLM_TEXT_COLUMNS = TEXT_COLUMNS + ["intended_major"]
# Prose fields where a missing final punctuation mark suggests the text was cut off.
TRUNCATION_COLUMNS = ["essay_text", "recommendation_summary", "financial_need_statement"]
# Fields that, together, describe an applicant's numbers. Groups matching on all of them are reported.
PROFILE_COLUMNS = [
    "gpa", "gpa_scale", "class_rank", "test_score", "household_size",
    "first_generation", "work_hours_per_week", "community_service_hours",
]
IDENTITY_COLUMNS = ["city", "state", "zip", "high_school"]

SHORT_TEXT_RATIO = 0.5  # under half the column's median word count counts as short
SHINGLE_WORDS = 5
NEAR_DUPLICATE_JACCARD = 0.5
COMMON_SHINGLE_DOCS = 50  # shingles in more docs than this are boilerplate and don't count
MAX_IDS_IN_MARKDOWN = 25

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


def _blank(c: str) -> str:
    return f"({c} IS NULL OR trim({c}) = '')"


def _unparseable(c: str) -> str | None:
    rule = PARSES.get(SCHEMA[c])
    return None if rule is None else f"(NOT {_blank(c)} AND NOT ({rule.format(c=c)}))"


def _ids(con: duckdb.DuckDBPyConnection, where: str, params: list | None = None) -> list[str]:
    row = con.execute(
        f"SELECT list({ID} ORDER BY {ID}) FROM typed WHERE {where}", params or []
    ).fetchone()
    return row[0] or []


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


def profile_columns(con: duckdb.DuckDBPyConnection) -> dict:
    out = {}
    for c, kind in SCHEMA.items():
        col = {"expected": kind, "blank_ids": _ids(con, _blank(c))}
        bad = _unparseable(c)
        col["unparseable_ids"] = _ids(con, bad) if bad else []
        if kind == "number":
            lo, med, mean, hi = con.execute(
                f"SELECT min(v), median(v), avg(v), max(v) FROM (SELECT TRY_CAST({c} AS DOUBLE) AS v FROM typed)"
            ).fetchone()
            col["summary"] = {"min": lo, "median": med, "mean": _round(mean), "max": hi}
        if c in SHOW_VALUES:
            col["values"] = dict(
                con.execute(
                    f"SELECT coalesce(trim({c}), '(blank)') AS v, count(*) FROM typed GROUP BY v ORDER BY v"
                ).fetchall()
            )
        else:
            col["distinct"] = con.execute(f"SELECT count(DISTINCT {c}) FROM typed").fetchone()[0]
        out[c] = col
    return out


def profile_text(con: duckdb.DuckDBPyConnection) -> dict:
    out = {}
    for c in TEXT_COLUMNS:
        words = f"len(string_split(trim(regexp_replace({c}, '\\s+', ' ', 'g')), ' '))"
        stats = con.execute(
            f"""
            SELECT min(w), quantile_cont(w, 0.05), median(w), quantile_cont(w, 0.95), max(w), median(chars)
            FROM (SELECT {words} AS w, length({c}) AS chars FROM typed WHERE NOT {_blank(c)})
            """
        ).fetchone()
        keys = ["min", "p05", "median", "p95", "max"]
        entry = {"words": {k: _round(v) for k, v in zip(keys, stats[:5])}, "median_chars": stats[5]}
        median = stats[2]
        entry["short_ids"] = (
            _ids(con, f"NOT {_blank(c)} AND {words} < ?", [median * SHORT_TEXT_RATIO]) if median else []
        )
        if c in TRUNCATION_COLUMNS:
            # No closing punctuation, a trailing ellipsis, or trailing whitespace (text cut after a sentence).
            entry["possibly_truncated_ids"] = _ids(
                con,
                f"""NOT {_blank(c)} AND (
                    NOT regexp_matches(rtrim({c}), '[.!?"\\x27\u201d\u2019)]$')
                    OR regexp_matches(rtrim({c}), '(\\.\\.\\.|\u2026)$')
                    OR {c} <> rtrim({c}))""",
            )
        out[c] = entry
    return out


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


def sanity_checks(con: duckdb.DuckDBPyConnection) -> list[dict]:
    results = []
    for name, rule, fails, inputs in SANITY_CHECKS:
        could_not_run = {}
        usable = []
        for c in inputs:
            could_not_run[f"{c} blank"] = _ids(con, _blank(c))
            bad = _unparseable(c)
            if bad:
                could_not_run[f"{c} unparseable"] = _ids(con, bad)
            usable.append(f"NOT {_blank(c)}" + (f" AND NOT {bad}" if bad else ""))
        results.append({
            "check": name,
            "rule": rule,
            "failed_ids": _ids(con, " AND ".join(usable) + f" AND ({fails})"),
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
        "failed_ids": sorted(i for (ids,) in groups for i in ids),
        "failed_groups": [ids for (ids,) in groups],
        "could_not_run": {
            k: v for k, v in {
                "high_school blank": _ids(con, _blank("high_school")),
                "class_rank blank or unparseable": _ids(con, "rank_size IS NULL"),
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
            {"ids": ids, "same_name": n, "same_essay": e} for ids, n, e in profiles
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
        FROM typed WHERE NOT {_blank(c)}
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
        "pairs": [{"ids": [a, b], "jaccard": _round(j)} for a, b, j in pairs],
        "could_not_run": {
            reason: ids for reason, ids in {
                f"{c} blank": _ids(con, _blank(c)),
                f"{c} under {k} words": sorted(
                    r[0] for r in con.execute(f"SELECT id FROM words WHERE len(w) < {k}").fetchall()
                ),
            }.items() if ids
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


def build_findings(p: dict) -> list[dict]:
    """The short list at the top of the report: everything that isn't clean, with IDs."""
    f = []

    def add(area: str, message: str, ids: list[str] | None = None) -> None:
        f.append({"area": area, "message": message, "ids": ids or []})

    src = p["source"]
    if src["parse_errors"]:
        add("parsing", f"{len(src['parse_errors'])} CSV lines could not be parsed (see line numbers in JSON)")
    if src["missing_columns"]:
        add("schema", f"missing columns: {', '.join(src['missing_columns'])}")
    if src["unexpected_columns"]:
        add("schema", f"unexpected columns: {', '.join(src['unexpected_columns'])}")
    for c, col in p["columns"].items():
        if col["blank_ids"]:
            add("missing", f"{c}: {len(col['blank_ids'])} blank", col["blank_ids"])
        if col["unparseable_ids"]:
            add("unparseable", f"{c}: {len(col['unparseable_ids'])} don't parse as {col['expected']}",
                col["unparseable_ids"])
    for c, t in p["text"].items():
        if t["short_ids"]:
            add("text", f"{c}: {len(t['short_ids'])} under half the median length", t["short_ids"])
        if t.get("possibly_truncated_ids"):
            add("text", f"{c}: {len(t['possibly_truncated_ids'])} may be truncated (no closing punctuation, or trailing whitespace)",
                t["possibly_truncated_ids"])
    for s in p["sanity"]:
        if s["failed_ids"]:
            add("sanity", f"{s['check']}: {len(s['failed_ids'])} fail ({s['rule']})", s["failed_ids"])
        for reason, ids in s["could_not_run"].items():
            add("could not check", f"{s['check']}: couldn't run for {len(ids)} ({reason})", ids)
    d = p["duplicates"]
    if d["duplicate_applicant_ids"]:
        add("duplicates", "applicant_id used more than once", d["duplicate_applicant_ids"])
    for ids in d["exact_copies_except_id"]:
        add("duplicates", "identical in every field except applicant_id", ids)
    for pair in d["same_name"]:
        also = ", ".join(pair["also_matching"]) or "nothing else"
        add("duplicates", f"same name; also matching: {also}", pair["ids"])
    for g in d["same_numbers"]:
        add("duplicates", "same values in every numeric/profile field"
            f" (same name: {'yes' if g['same_name'] else 'no'}, same essay: {'yes' if g['same_essay'] else 'no'})",
            g["ids"])
    for c, near in d["near_identical"].items():
        for pair in near["pairs"]:
            add("duplicates", f"{c} near-identical (Jaccard {pair['jaccard']})", pair["ids"])
        for reason, ids in near["could_not_run"].items():
            add("could not check", f"{c} near-duplicate check couldn't run for {len(ids)} ({reason})", ids)
    by_category: dict[tuple[str, str], list[str]] = {}
    for hit in p["instruction_like"]:
        by_category.setdefault((hit["category"], hit["column"]), []).append(hit["id"])
    for (category, c), ids in sorted(by_category.items()):
        add("instruction-like", f"{c}: matches '{category}' pattern", ids)
    return f


def profile(path: Path) -> dict:
    con = duckdb.connect()
    p = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"path": path.as_posix(), "sha256": sha256(path), **load(con, path)},
    }
    p["columns"] = profile_columns(con)
    p["text"] = profile_text(con)
    p["sanity"] = sanity_checks(con)
    p["duplicates"] = duplicates(con)
    p["instruction_like"] = instruction_like(con)
    p["findings"] = build_findings(p)
    return p


def _round(v):
    return round(v, 3) if isinstance(v, float) else v


def _id_list(ids: list[str]) -> str:
    if not ids:
        return ""
    shown = ", ".join(ids[:MAX_IDS_IN_MARKDOWN])
    extra = len(ids) - MAX_IDS_IN_MARKDOWN
    return shown + (f", and {extra} more (see JSON)" if extra > 0 else "")


def render_markdown(p: dict) -> str:
    src = p["source"]
    lines = [
        "# Data profile",
        "",
        f"- Source: `{src['path']}`",
        f"- SHA-256: `{src['sha256']}`",
        f"- Rows: {src['rows']}",
        f"- Generated: {p['generated_at']}",
        "",
        "Counts and applicant IDs only. No raw text or personal information.",
        "",
        "## Findings",
        "",
    ]
    if not p["findings"]:
        lines.append("Nothing flagged.")
    by_area: dict[str, list[dict]] = {}
    for f in p["findings"]:
        by_area.setdefault(f["area"], []).append(f)
    for area, findings in by_area.items():
        if lines[-1] != "":
            lines.append("")
        lines += [f"### {area.capitalize()}", ""]
        for f in findings:
            ids = f" — {_id_list(f['ids'])}" if f["ids"] else ""
            lines.append(f"- {f['message']}{ids}")

    lines += ["", "## Columns", "", "| Column | Expected | DuckDB inferred | Blank | Unparseable | Distinct |",
              "|---|---|---|---|---|---|"]
    for c, col in p["columns"].items():
        distinct = col.get("distinct", len(col.get("values", {})))
        lines.append(
            f"| {c} | {col['expected']} | {src['inferred_types'].get(c, '-')} | {len(col['blank_ids'])}"
            f" | {len(col['unparseable_ids'])} | {distinct} |"
        )

    lines += ["", "## Numeric columns", "", "| Column | Min | Median | Mean | Max |", "|---|---|---|---|---|"]
    for c, col in p["columns"].items():
        if "summary" in col:
            s = col["summary"]
            lines.append(f"| {c} | {s['min']} | {s['median']} | {s['mean']} | {s['max']} |")

    lines += ["", "## Value counts", ""]
    for c in SHOW_VALUES:
        counts = ", ".join(f"{v}: {n}" for v, n in p["columns"][c]["values"].items())
        lines.append(f"- {c}: {counts}")

    lines += ["", "## Text lengths (words)", "",
              "| Column | Min | P5 | Median | P95 | Max | Median chars | Short | Possibly truncated |",
              "|---|---|---|---|---|---|---|---|---|"]
    for c, t in p["text"].items():
        w = t["words"]
        truncated = len(t["possibly_truncated_ids"]) if "possibly_truncated_ids" in t else "-"
        lines.append(
            f"| {c} | {w['min']} | {w['p05']} | {w['median']} | {w['p95']} | {w['max']} | {t['median_chars']}"
            f" | {len(t['short_ids'])} | {truncated} |"
        )

    lines += ["", "## Sanity checks", "", "| Check | Rule | Failed | Couldn't run |", "|---|---|---|---|"]
    for s in p["sanity"]:
        skipped = sum(len(v) for v in s["could_not_run"].values())
        lines.append(f"| {s['check']} | {s['rule']} | {len(s['failed_ids'])} | {skipped} |")

    d = p["duplicates"]
    lines += [
        "", "## Duplicates", "",
        f"- Repeated applicant_id: {len(d['duplicate_applicant_ids'])}",
        f"- Groups identical except applicant_id: {len(d['exact_copies_except_id'])}",
        f"- Pairs with the same name: {len(d['same_name'])}",
        f"- Groups with identical numbers: {len(d['same_numbers'])}",
    ]
    for c, near in d["near_identical"].items():
        lines.append(f"- Near-identical {c} pairs (Jaccard >= {NEAR_DUPLICATE_JACCARD}): {len(near['pairs'])}")

    lines += ["", "## Instruction-like text", "",
              f"Pattern matches in text that reaches the model. Categories: {', '.join(INSTRUCTION_PATTERNS)}.",
              f"Matches: {len(p['instruction_like'])}. A match is a flag for a person, not proof of anything.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    p = profile(args.csv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "data_profile.json").write_text(json.dumps(p, indent=2, default=str) + "\n", encoding="utf-8")
    (args.out_dir / "data_profile.md").write_text(render_markdown(p), encoding="utf-8")
    print(f"{len(p['findings'])} findings -> {args.out_dir / 'data_profile.md'}")


if __name__ == "__main__":
    main()
