"""Intake: decide, per applicant, whether they can be scored, and build what the model sees.

A hold stops scoring until a person looks; it's for when a criterion can't be judged at all or
the record can't be trusted. A flag doesn't stop scoring; it tells the reviewer something and
lowers confidence on the criteria it touches. Nothing is dropped, edited or merged.

    python -m app.intake data/raw/scholarship_candidates.csv

Writes data/derived/intake.jsonl (one applicant per line, with the ref mapping and the view)
and data/derived/intake_summary.json (counts and who was held or flagged, by ID only).
"""

from __future__ import annotations

import argparse
import json
import secrets
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from app.checks import duplicates, instruction_like, sanity_checks, text_lengths
from app.redaction import REDACTION_VERSION, redact
from app.source import BLANK_ID, ID, SCHEMA, blank, ids, load, sha256, unparseable

INTAKE_VERSION = "1"

READINESS, NEED, PERSISTENCE, COMMUNITY, PURPOSE = (
    "academic_readiness", "financial_need", "persistence", "community", "clarity_of_purpose",
)

# Blank (or unparseable, which counts as blank) means a criterion can't be judged at all.
HOLD_IF_BLANK = {"essay_text": "missing_essay", "financial_need_statement": "missing_need_statement"}

# Any other blank field is a flag, lowering confidence on the criteria it feeds. gpa and
# class_rank are only held when both are blank; one alone is a flag.
FEEDS = {
    "gpa": [READINESS],
    "gpa_scale": [READINESS],
    "class_rank": [READINESS],
    "test_score": [READINESS],
    "recommendation_summary": [READINESS, PERSISTENCE, COMMUNITY],
    "household_size": [NEED],
    "first_generation": [NEED, READINESS],
    "work_hours_per_week": [READINESS, NEED, COMMUNITY],
    "extracurriculars": [COMMUNITY],
    "community_service_hours": [COMMUNITY],
    "intended_major": [PURPOSE],
    "essay_prompt": [PERSISTENCE, PURPOSE],
    # Not scored, but a blank one weakens duplicate detection or redaction.
    "name": [], "city": [], "state": [], "zip": [], "high_school": [], "school_type": [], "submitted_at": [],
}

# Same name plus any of these means the same person applied twice.
SAME_PERSON_LOCATION = [["zip"], ["high_school"], ["city", "state"]]


def _missing(c: str) -> str:
    bad = unparseable(c)
    return f"({blank(c)} OR {bad})" if bad else blank(c)


def assess(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Holds and flags per applicant_id, from the checks over the loaded `typed` view."""
    found: dict[str, dict] = defaultdict(lambda: {"holds": [], "flags": []})

    # Rows sharing an applicant_id share an entry, so the same finding can arrive twice.
    def add(i: str, kind: str, entry: dict) -> None:
        if entry not in found[i][kind]:
            found[i][kind].append(entry)

    def hold(i: str, rule: str, detail: str) -> None:
        add(i, "holds", {"rule": rule, "detail": detail})

    def flag(i: str, rule: str, detail: str, lowers=()) -> None:
        add(i, "flags", {"rule": rule, "detail": detail, "lowers_confidence": list(lowers)})

    for i in ids(con, f"{ID} = ?", [BLANK_ID]):
        hold(i, "missing_applicant_id", "applicant_id is blank")
    d = duplicates(con)
    for i in d["duplicate_applicant_ids"]:
        hold(i, "duplicate_applicant_id", "applicant_id appears on more than one row")

    for c, rule in HOLD_IF_BLANK.items():
        for i in ids(con, _missing(c)):
            hold(i, rule, f"{c} is blank or unparseable")
    no_gpa_or_rank = f"{_missing('gpa')} AND {_missing('class_rank')}"
    for i in ids(con, no_gpa_or_rank):
        hold(i, "missing_gpa_and_rank", "both gpa and class_rank are blank or unparseable")
    for c, lowers in FEEDS.items():
        where = _missing(c)
        if c in ("gpa", "class_rank"):
            where += f" AND NOT ({no_gpa_or_rank})"  # already held
        for i in ids(con, where):
            flag(i, f"missing_{c}", f"{c} is blank or unparseable", lowers)

    for i in text_lengths(con)["essay_text"]["short_ids"]:
        hold(i, "short_essay", "essay is under half the median essay length")

    for s in sanity_checks(con):
        for group in s.get("failed_groups") or [[i] for i in s["failed_ids"]]:
            for i in group:
                others = [o for o in group if o != i]
                conflict = f" (conflicts with {', '.join(others)})" if others else ""
                hold(i, f"sanity_{s['check']}", s["rule"] + conflict)

    for pair in d["same_name"]:
        if any(all(c in pair["also_matching"] for c in loc) for loc in SAME_PERSON_LOCATION):
            a, b = pair["ids"]
            hold(a, "same_person", f"same name and location as {b}")
            hold(b, "same_person", f"same name and location as {a}")

    names = dict(con.execute(f"SELECT {ID}, name_key FROM typed").fetchall())
    for g in d["same_numbers"]:
        if not g["same_name"]:
            for i in g["ids"]:
                others = ", ".join(o for o in g["ids"] if o != i)
                flag(i, "near_identical_application", f"same values in every numeric field as {others}")
    for pair in d["near_identical"]["essay_text"]["pairs"]:
        a, b = pair["ids"]
        if names.get(a) != names.get(b):
            flag(a, "near_identical_application", f"near-identical essay to {b}")
            flag(b, "near_identical_application", f"near-identical essay to {a}")

    for hit in instruction_like(con):
        flag(hit["id"], "instruction_like_text", f"{hit['column']} matches the '{hit['category']}' pattern")
    return found


def _new_ref() -> str:
    return "a-" + secrets.token_hex(6)


def run_intake(path: Path, new_ref: Callable[[], str] = _new_ref) -> dict:
    """Assess every row and build its redacted view. Refs are opaque and new every run; the
    ref-to-applicant_id mapping lives here, in intake's output, never in the view."""
    con = duckdb.connect()
    info = load(con, path)
    found = assess(con)
    columns = list(SCHEMA)
    rows = con.execute(f"SELECT {', '.join(columns)} FROM raw ORDER BY rowid").fetchall()

    applicants, used = [], set()
    for values in rows:
        row = dict(zip(columns, values))
        applicant_id = (row[ID] or "").strip() or BLANK_ID
        ref = new_ref()
        while ref in used:
            ref = new_ref()
        used.add(ref)
        result = found.get(applicant_id, {"holds": [], "flags": []})
        redacted = redact(row, ref)
        applicants.append({
            "applicant_id": applicant_id,
            "ref": ref,
            "status": "held" if result["holds"] else "ready",
            "holds": result["holds"],
            "flags": result["flags"],
            "lowers_confidence": sorted({c for f in result["flags"] for c in f["lowers_confidence"]}),
            "redaction": {k: v for k, v in redacted.items() if k != "view"},
            "view": redacted["view"],
        })

    ready = [a for a in applicants if a["status"] == "ready"]
    return {
        "intake_version": INTAKE_VERSION,
        "redaction_version": REDACTION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "path": path.as_posix(),
            "sha256": sha256(path),
            **{k: info[k] for k in ("rows", "parse_errors", "missing_columns", "unexpected_columns")},
        },
        "counts": {
            "applicants": len(applicants),
            "held": len(applicants) - len(ready),
            "ready": len(ready),
            "ready_with_flags": sum(1 for a in ready if a["flags"]),
            "rejected_lines": len(info["parse_errors"]),
        },
        "applicants": applicants,
    }


def summarize(result: dict) -> dict:
    """The run without the views: who was held or flagged, and why, by ID only."""
    def rules(a: dict, kind: str) -> list[str]:
        return sorted({e["rule"] for e in a[kind]})

    summary = {k: v for k, v in result.items() if k != "applicants"}
    summary["held"] = [
        {"applicant_id": a["applicant_id"], "holds": rules(a, "holds")}
        for a in result["applicants"] if a["status"] == "held"
    ]
    summary["ready_with_flags"] = [
        {"applicant_id": a["applicant_id"], "flags": rules(a, "flags")}
        for a in result["applicants"] if a["status"] == "ready" and a["flags"]
    ]
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("data/derived"))
    args = parser.parse_args(argv)

    result = run_intake(args.csv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "intake.jsonl").open("w", encoding="utf-8") as f:
        for a in result["applicants"]:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    (args.out_dir / "intake_summary.json").write_text(
        json.dumps(summarize(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    c = result["counts"]
    print(
        f"{c['applicants']} applicants: {c['held']} held, {c['ready']} ready "
        f"({c['ready_with_flags']} with flags), {c['rejected_lines']} rejected lines -> {args.out_dir}"
    )


if __name__ == "__main__":
    main()
