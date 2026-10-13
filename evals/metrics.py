"""Comparisons and pass/fail rules for the evals.

A "shouldn't change" comparison only fails when the move is bigger than both its threshold and
the measured run-to-run spread for that criterion, so ordinary noise never counts as a failure.
Both numbers are always reported.
"""

from __future__ import annotations

from statistics import median

from app.intake import CRITERIA, NEED
from app.scoring import weighted_total


def spread(runs: list[dict]) -> dict:
    """Max minus min per criterion and for the total, over repeated runs of the same input."""
    out = {c: max(r["scores"][c] for r in runs) - min(r["scores"][c] for r in runs) for c in CRITERIA}
    out["total"] = round(max(r["total"] for r in runs) - min(r["total"] for r in runs), 2)
    return out


def pooled_spread(spreads: list[dict]) -> dict:
    """For cases without their own repeats: the widest spread seen for each criterion in the sample."""
    keys = [*CRITERIA, "total"]
    return {k: max((s[k] for s in spreads), default=0) for k in keys}


def baseline(runs: list[dict]) -> dict:
    """Median per criterion and median total over repeated runs."""
    return {
        "scores": {c: median(r["scores"][c] for r in runs) for c in CRITERIA},
        "total": median(r["total"] for r in runs),
    }


def invariance(base: dict, variant: dict, noise: dict, thresholds: dict) -> dict:
    rows = {}
    for c in [*CRITERIA, "total"]:
        before = base["total"] if c == "total" else base["scores"][c]
        after = variant["total"] if c == "total" else variant["scores"][c]
        delta = round(after - before, 2)
        threshold = thresholds["total" if c == "total" else "criterion"]
        limit = max(threshold, noise[c])
        rows[c] = {"before": before, "after": after, "delta": delta, "threshold": threshold,
                   "spread": noise[c], "failed": abs(delta) > limit}
    return {"passed": not any(r["failed"] for r in rows.values()), "by_criterion": rows}


def need_moves(high: dict, low: dict, noise: dict, thresholds: dict) -> dict:
    """Higher need must score at least the threshold higher, and more than run-to-run noise."""
    gap = high["scores"][NEED] - low["scores"][NEED]
    required = max(thresholds["need_gap"], noise[NEED])
    return {"high": high["scores"][NEED], "low": low["scores"][NEED], "gap": gap,
            "threshold": thresholds["need_gap"], "spread": noise[NEED], "passed": gap >= required}


def no_drop(base: dict, variant: dict, criterion: str, noise: dict, limit: float) -> dict:
    """The criterion may go up, or down by no more than the larger of the limit and the noise."""
    delta = variant["scores"][criterion] - base["scores"][criterion]
    return {"criterion": criterion, "before": base["scores"][criterion], "after": variant["scores"][criterion],
            "delta": delta, "threshold": limit, "spread": noise[criterion],
            "passed": delta >= -max(limit, noise[criterion])}


def stays_put(base: dict, variant: dict, criterion: str, noise: dict, limit: float) -> dict:
    delta = variant["scores"][criterion] - base["scores"][criterion]
    return {"criterion": criterion, "before": base["scores"][criterion], "after": variant["scores"][criterion],
            "delta": delta, "threshold": limit, "spread": noise[criterion],
            "passed": abs(delta) <= max(limit, noise[criterion])}


def golden_agreement(model: dict[str, dict], human: dict[str, dict], weights_percent: dict[str, int]) -> dict:
    """model and human map applicant_id -> {criterion: score}. Mean absolute gap and share within 1
    per criterion, the gap on weighted totals, and whether both rank the applicants the same way."""
    ids = sorted(set(model) & set(human))
    per = {}
    for c in CRITERIA:
        gaps = [abs(model[i][c] - human[i][c]) for i in ids]
        per[c] = {"mean_abs_gap": round(sum(gaps) / len(gaps), 2) if gaps else None,
                  "within_1": f"{sum(g <= 1 for g in gaps)}/{len(gaps)}"}
    model_totals = {i: weighted_total(model[i], weights_percent) for i in ids}
    human_totals = {i: weighted_total(human[i], weights_percent) for i in ids}
    total_gaps = [abs(model_totals[i] - human_totals[i]) for i in ids]
    return {
        "applicants": ids,
        "by_criterion": per,
        "total_mean_abs_gap": round(sum(total_gaps) / len(total_gaps), 2) if ids else None,
        "same_order": sorted(ids, key=lambda i: -model_totals[i]) == sorted(ids, key=lambda i: -human_totals[i]),
        "totals": {i: {"model": model_totals[i], "human": human_totals[i]} for i in ids},
    }
