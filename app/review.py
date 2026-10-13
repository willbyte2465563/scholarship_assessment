"""The reviewer's side, worked out from the audit log. Plain functions; the web layer stays thin.

There's no database: every reviewer action is an audit-log entry, and the current state (hold
decisions, score overrides, the final 3) is read back from those entries.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.intake import CRITERIA
from app.ranking import build_ranking
from app.redaction import fingerprint
from app.scoring import ScoringConfig, weighted_total

HOLD_EVENTS = ("hold_released", "hold_excluded")
MARKS = ("rank", "tied", "shortlisted", "shortlist_reason", "close_call", "committee_tie")
SNAPSHOT_KEYS = ("applicant_id", "rank", "model_rank", "tied", "total", "model_total", "total_spread",
                 "shortlisted", "shortlist_reason", "close_call", "committee_tie", "adjusted", "overridden",
                 "intake_flags", "scoring_flags")


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def latest_run(runs_dir: Path) -> Path | None:
    runs = sorted(d for d in runs_dir.glob("*") if (d / "ranking.json").exists()) if runs_dir.exists() else []
    return runs[-1] if runs else None


def load_run(run_dir: Path) -> dict:
    return {"id": run_dir.name, "dir": run_dir, **json.loads((run_dir / "ranking.json").read_text(encoding="utf-8"))}


def hold_decisions(entries: list[dict]) -> dict[str, dict]:
    """The latest release or exclusion per applicant. Holds outlive a run, so these aren't per run."""
    return {e["applicant_id"]: e for e in entries if e["event"] in HOLD_EVENTS}


def overrides_for(entries: list[dict], run_id: str) -> dict[tuple[str, str], dict]:
    """The latest override per (applicant, criterion) for this run's scores."""
    return {(e["applicant_id"], e["criterion"]): e for e in entries
            if e["event"] == "score_override" and e.get("batch_run") == run_id}


def final_for(entries: list[dict], run_id: str) -> dict | None:
    finals = [e for e in entries if e["event"] == "final_3_recorded" and e.get("batch_run") == run_id]
    return finals[-1] if finals else None


def current_ranking(run: dict, overrides: dict, scoring: ScoringConfig) -> list[dict]:
    """The run's ranking with overrides applied and re-ranked by the same rules and the same
    measured noise. The model's scores, total and rank stay alongside; `adjusted` marks anyone
    whose rank moved because of an override (theirs or someone else's)."""
    entries = []
    for e in run["ranking"]:
        scores, overridden = dict(e["scores"]), []
        for c in CRITERIA:
            if (o := overrides.get((e["applicant_id"], c))) is not None:
                scores[c], overridden = o["score"], overridden + [c]
        entries.append({
            **{k: v for k, v in e.items() if k not in MARKS},
            "model_rank": e["rank"], "model_total": e["total"], "model_scores": e["scores"],
            "model_shortlisted": e["shortlisted"],
            "scores": scores, "total": weighted_total(scores, scoring.weights_percent), "overridden": overridden,
        })
    rules = scoring.ranking
    ranked = build_ranking(entries, shortlist_size=rules["shortlist_size"],
                           close_call_position=rules["close_call_position"], noise=run["noise"])
    return [{**r, "adjusted": r["rank"] != r["model_rank"]} for r in ranked]


def validate_final(selection: list[str], reason: str, acknowledged_tie: bool, current: list[dict]) -> list[str]:
    errors = []
    picked = list(dict.fromkeys(selection))
    if len(picked) != 3:
        errors.append(f"Pick exactly 3 applicants (you picked {len(picked)}).")
    shortlist = {r["applicant_id"] for r in current if r["shortlisted"]}
    if outside := [i for i in picked if i not in shortlist]:
        errors.append(f"Not on the current shortlist: {', '.join(outside)}.")
    if not reason.strip():
        errors.append("Give a reason for the final 3.")
    tie = sorted(r["applicant_id"] for r in current if r["committee_tie"])
    if set(tie) & set(picked) and not acknowledged_tie:
        errors.append(f"Your pick includes the tie at #3 ({', '.join(tie)}). Confirm you're settling it as the committee.")
    return errors


def final_snapshot(run: dict, current: list[dict], overrides: dict) -> dict:
    """What the reviewer was looking at when they chose: the shortlist as shown, every override
    in effect, the noise band, and the hash of the ranking it all came from."""
    return {
        "run": run["id"],
        "prompt_version": run["meta"]["prompt_version"],
        "ranking_sha256": fingerprint(run["ranking"]),
        "noise": run["noise"],
        "shortlist": [{k: r[k] for k in SNAPSHOT_KEYS} for r in current if r["shortlisted"]],
        "overrides": [{k: o[k] for k in ("applicant_id", "criterion", "model_score", "score", "reason", "reviewer")}
                      for o in overrides.values()],
    }


def comparison(run: dict, entries: list[dict], ids: list[str], current: list[dict], applicants: dict) -> list[dict]:
    """Side by side: per criterion the median, the reviewer's score if overridden, the spread and
    every run's score, plus the rationale and evidence from a run that scored the median. `applicants`
    is intake's output by ID; if an application changed since it was scored, `view_changed` says so."""
    calls = {}
    calls_path = run["dir"] / "calls.jsonl"
    for line in calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record["status"] == "scored":
            calls[(record["applicant_id"], record["run"])] = record
    accepted, scored_views = {}, {}
    for e in entries:
        if (e["event"] == "score_attempt" and e.get("batch_run") == run["id"] and e.get("raw_output")
                and e.get("failure") is None and not e.get("problems")):
            accepted[(e["applicant_id"], e["run"])] = {s["criterion"]: s for s in json.loads(e["raw_output"])["scores"]}
            scored_views[e["applicant_id"]] = e["view_sha256"]
    by_id = {r["applicant_id"]: r for r in current}

    out = []
    for i in ids:
        row, criteria = by_id[i], {}
        runs = sorted(k for (a, k) in calls if a == i)
        for c in CRITERIA:
            run_scores = [calls[(i, k)]["scores"][c] for k in runs]
            median = row["model_scores"][c]
            source = next((k for k in runs if calls[(i, k)]["scores"][c] == median), None)
            item = accepted.get((i, source), {}).get(c, {})
            criteria[c] = {
                "median": median, "current": row["scores"][c], "overridden": c in row["overridden"],
                "spread": max(run_scores) - min(run_scores) if run_scores else None, "runs": run_scores,
                "score_from_run": source, "rationale": item.get("rationale"), "evidence": item.get("evidence", []),
                "confidence": item.get("confidence"),
            }
        applicant = applicants.get(i)
        out.append({"applicant_id": i, "ranking": row, "criteria": criteria, "view": applicant and applicant["view"],
                    "view_changed": bool(applicant) and scored_views.get(i) != applicant["redaction"]["view_sha256"]})
    return out


def apply_hold_decisions(intake: dict, entries: list[dict]) -> dict:
    """Applicants held at intake that a reviewer released ("score anyway") become ready for the
    next batch, flagged as released. Excluded ones stay held."""
    decisions = hold_decisions(entries)
    applicants = []
    for a in intake["applicants"]:
        d = decisions.get(a["applicant_id"])
        if a["status"] == "held" and d and d["event"] == "hold_released":
            flag = {"rule": "released_by_reviewer", "detail": d["reason"], "lowers_confidence": []}
            a = {**a, "status": "ready", "flags": a["flags"] + [flag]}
        applicants.append(a)
    ready = sum(1 for a in applicants if a["status"] == "ready")
    return {**intake, "applicants": applicants,
            "counts": {**intake["counts"], "ready": ready, "held": len(applicants) - ready}}


def identities(csv_path: Path, ids: list[str]) -> dict[str, dict]:
    """Name and location from the raw CSV, for the given IDs only. Used once the final 3 is recorded."""
    wanted, found = set(ids), {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            i = (row.get("applicant_id") or "").strip()
            if i in wanted:
                found[i] = {k: (row.get(k) or "").strip() for k in ("name", "city", "state", "high_school")}
    return found
