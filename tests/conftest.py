"""Builders for small applicant CSVs. Every row is clean unless a test overrides a field."""

import csv
from pathlib import Path

import pytest

from app.source import SCHEMA


def make_text(seed: int, words: int = 40) -> str:
    """Text whose words are unique to the seed, so rows never look alike by accident."""
    return " ".join(f"word{seed}x{i}" for i in range(words)) + "."


def make_row(applicant_id: str, seed: int, **overrides) -> dict:
    row = {
        "applicant_id": applicant_id,
        "name": f"Person {seed}",
        "city": "Springfield",
        "state": "MO",
        "zip": "65806",
        "high_school": f"School {seed}",
        "school_type": "public",
        "gpa": "3.50",
        "gpa_scale": "4.00",
        "class_rank": "10/200",
        "test_score": "1200",
        "intended_major": "Biology",
        "household_size": "4",
        "first_generation": "N",
        "work_hours_per_week": str(seed),
        "extracurriculars": "Chess club; library volunteer",
        "community_service_hours": "50",
        "essay_prompt": "Describe a challenge.",
        "essay_text": make_text(seed),
        "recommendation_summary": make_text(seed + 1000, 12),
        "financial_need_statement": "Household of four, moderate need.",
        "submitted_at": "2026-02-01 09:00",
    }
    row.update(overrides)
    return row


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    columns = columns or list(SCHEMA)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def reviewer_rows() -> list[dict]:
    """Twenty scorable applicants with distinctive identities, one recommendation carrying markup,
    one applicant held at intake, and a same-person pair held at intake. With tests.fakes.VaryingLLM,
    R-004/009/014/019 tie at the top, so four applicants share #1 and the tie straddles #3."""
    rows = [
        make_row(f"R-{s:03d}", s, name=f"Zed{s} Quill{s}", city=f"Town{s}x", high_school=f"Academy{s}x", zip=f"{10000 + s}")
        for s in range(1, 21)
    ]
    rows[1]["recommendation_summary"] = "<script>alert('x')</script> Strong student."
    rows.append(make_row("R-021", 21, name="Zed21 Quill21", city="Town21x", high_school="Academy21x", essay_text=""))
    # The same person twice, with a different GPA and service hours: both held at intake.
    twin = {"name": "Zed22 Quill22", "city": "Town22x", "high_school": "Academy22x", "zip": "10022"}
    rows.append(make_row("R-022", 22, **twin))
    rows.append(make_row("R-023", 23, **twin, gpa="3.40", community_service_hours="45"))
    return rows


@pytest.fixture
def scored_run(tmp_path, monkeypatch) -> dict:
    """A real batch run (fake model) in tmp: ranking, calls and audit log, like production's."""
    from app.batch import main as batch_main
    from tests.fakes import VaryingLLM

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    paths = {"csv": write_csv(tmp_path / "applicants.csv", reviewer_rows()),
             "runs": tmp_path / "runs", "audit": tmp_path / "audit.jsonl"}
    args = ["--yes", "--csv", str(paths["csv"]), "--runs-dir", str(paths["runs"]), "--audit", str(paths["audit"])]
    assert batch_main(args, llm=VaryingLLM()) == 0
    return paths
