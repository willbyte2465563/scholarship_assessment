"""The demo run committed in demo/: what a fresh clone shows. Guard what's in it."""

import csv
import json
from pathlib import Path

from app.audit import verify
from app.redaction import fingerprint
from app.review import latest_run, load_run

DEMO = Path("demo")
REVIEWER_EVENTS = {"hold_released", "hold_excluded", "comparison_viewed", "score_override", "final_3_recorded"}


def _entries():
    return [json.loads(line) for line in (DEMO / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_demo_audit_chain_verifies():
    assert verify(DEMO / "audit.jsonl") is None


def test_demo_has_no_reviewer_actions_and_no_final_3():
    assert not {e["event"] for e in _entries()} & REVIEWER_EVENTS


def test_demo_ranking_is_the_one_the_log_recorded():
    run = load_run(latest_run(DEMO / "scoring"))
    last = [e for e in _entries() if e["event"] == "batch_ranked"][-1]
    assert last["ranking_sha256"] == fingerprint(run["ranking"])
    assert run["meta"]["prompt_version"] == "score_v2"


def test_demo_holds_no_names_schools_or_towns():
    with Path("data/raw/scholarship_candidates.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    identity = {r["name"] for r in rows} | {r["high_school"] for r in rows}
    for path in DEMO.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not [s for s in identity if s in text], path
