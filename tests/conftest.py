"""Builders for small applicant CSVs. Every row is clean unless a test overrides a field."""

import csv
from pathlib import Path

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
