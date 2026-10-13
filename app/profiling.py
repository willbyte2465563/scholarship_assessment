"""Profile the applicant CSV with DuckDB.

Writes a markdown report and a JSON file with the same stats. Output holds
counts, column names and applicant IDs only, never raw text or personal info.
Everything is computed in SQL so the CSV is never loaded row by row into Python.

    python -m app.profiling data/raw/scholarship_candidates.csv
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from app.checks import (
    INSTRUCTION_PATTERNS, NEAR_DUPLICATE_JACCARD, duplicates, instruction_like, sanity_checks, text_lengths,
)
from app.source import SCHEMA, blank, ids, load, rounded, sha256, unparseable

# Only these columns have their values shown in the report. Everything else is counts and IDs.
SHOW_VALUES = ("school_type", "first_generation", "gpa_scale")
MAX_IDS_IN_MARKDOWN = 25


def profile_columns(con: duckdb.DuckDBPyConnection) -> dict:
    out = {}
    for c, kind in SCHEMA.items():
        col = {"expected": kind, "blank_ids": ids(con, blank(c))}
        bad = unparseable(c)
        col["unparseable_ids"] = ids(con, bad) if bad else []
        if kind == "number":
            lo, med, mean, hi = con.execute(
                f"SELECT min(v), median(v), avg(v), max(v) FROM (SELECT TRY_CAST({c} AS DOUBLE) AS v FROM typed)"
            ).fetchone()
            col["summary"] = {"min": lo, "median": med, "mean": rounded(mean), "max": hi}
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
    for s in p["sanity"]:
        if s["failed_ids"]:
            add("sanity", f"{s['check']}: {len(s['failed_ids'])} fail ({s['rule']})", s["failed_ids"])
        for reason, found in s["could_not_run"].items():
            add("could not check", f"{s['check']}: couldn't run for {len(found)} ({reason})", found)
    d = p["duplicates"]
    if d["duplicate_applicant_ids"]:
        add("duplicates", "applicant_id used more than once", d["duplicate_applicant_ids"])
    for group in d["exact_copies_except_id"]:
        add("duplicates", "identical in every field except applicant_id", group)
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
        for reason, found in near["could_not_run"].items():
            add("could not check", f"{c} near-duplicate check couldn't run for {len(found)} ({reason})", found)
    by_category: dict[tuple[str, str], list[str]] = {}
    for hit in p["instruction_like"]:
        by_category.setdefault((hit["category"], hit["column"]), []).append(hit["id"])
    for (category, c), found in sorted(by_category.items()):
        add("instruction-like", f"{c}: matches '{category}' pattern", found)
    return f


def profile(path: Path) -> dict:
    con = duckdb.connect()
    p = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"path": path.as_posix(), "sha256": sha256(path), **load(con, path)},
    }
    p["columns"] = profile_columns(con)
    p["text"] = text_lengths(con)
    p["sanity"] = sanity_checks(con)
    p["duplicates"] = duplicates(con)
    p["instruction_like"] = instruction_like(con)
    p["findings"] = build_findings(p)
    return p


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
            listed = f" — {_id_list(f['ids'])}" if f["ids"] else ""
            lines.append(f"- {f['message']}{listed}")

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
              "| Column | Min | P5 | Median | P95 | Max | Median chars | Short |",
              "|---|---|---|---|---|---|---|---|"]
    for c, t in p["text"].items():
        w = t["words"]
        lines.append(
            f"| {c} | {w['min']} | {w['p05']} | {w['median']} | {w['p95']} | {w['max']} | {t['median_chars']}"
            f" | {len(t['short_ids'])} |"
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
