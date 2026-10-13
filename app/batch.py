"""Score every ready applicant several times, take medians, then rank and shortlist.

    python -m app.batch                                  # dry run: counts and cost, no calls
    python -m app.batch --yes                            # score and rank (calls Gemini, costs money)
    python -m app.batch --yes --resume 20261013T170000Z  # carry on after a crash

Each finished call goes into data/scoring/<run>/calls.jsonl straight away, so a crash only loses
calls in flight. A resumed run reuses a scored call only if its input, prompt, config, model and
run number all match; held calls are tried again. One applicant failing never stops the batch.
If any of an applicant's runs ends held, the whole applicant is held, not ranked on fewer runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from app import pricing
from app.audit import AuditLog
from app.intake import CRITERIA, run_intake
from app.llm import LLM, Backoff, GeminiClient
from app.ranking import build_ranking, noise_band
from app.redaction import fingerprint
from app.scoring import DEFAULT_AUDIT, DEFAULT_CSV, ScoringConfig, load_config, render_prompt, score_applicant, weighted_total

RUNS_DIR = Path("data/scoring")


def call_key(applicant: dict, run: int, scoring: ScoringConfig) -> str:
    return fingerprint({
        "view_sha256": applicant["redaction"]["view_sha256"], "prompt_sha256": scoring.prompt_sha256,
        "config_version": scoring.version, "model": scoring.model, "run": run,
    })


def load_done(path: Path) -> dict[str, dict]:
    """Finished calls by key; the last record for a key wins."""
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # the last line, cut off by a crash
            done[record["key"]] = record
    return done


def planned(intake: dict, scoring: ScoringConfig, done: dict) -> tuple[list, list]:
    """(every call this batch needs, the ones not already scored)."""
    ready = [a for a in intake["applicants"] if a["status"] == "ready"]
    calls = [(a, k) for a in ready for k in range(1, scoring.batch["runs_per_applicant"] + 1)]
    todo = [(a, k) for a, k in calls if done.get(call_key(a, k, scoring), {}).get("status") != "scored"]
    return calls, todo


def score_all(intake: dict, llm: LLM, scoring: ScoringConfig, log: AuditLog, run_dir: Path, run_id: str,
              workers: int) -> list[dict]:
    run_dir.mkdir(parents=True, exist_ok=True)
    calls_path = run_dir / "calls.jsonl"
    done = load_done(calls_path)
    calls, todo = planned(intake, scoring, done)
    lock = threading.Lock()

    def work(task: tuple[dict, int]) -> None:
        applicant, run = task
        try:
            r = score_applicant(applicant, llm, scoring, log, context={"batch_run": run_id, "run": run})
        except Exception as e:  # one applicant must never stop the batch
            r = {"status": "held", "reason": f"internal error: {type(e).__name__}"}
        record = {
            "key": call_key(applicant, run, scoring), "applicant_id": applicant["applicant_id"], "run": run,
            "status": r["status"],
            "scores": {c: r["criteria"][c]["score"] for c in CRITERIA} if r["status"] == "scored" else None,
            "total": r.get("total"), "flags": r.get("flags", []), "reason": r.get("reason"),
        }
        with lock:
            with calls_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            done[record["key"]] = record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, todo))
    return [done[call_key(a, k, scoring)] for a, k in calls]


def aggregate(intake: dict, records: list[dict], scoring: ScoringConfig) -> list[dict]:
    """One entry per applicant: held (at intake or in scoring) with reasons, or scored with the
    median per criterion, the spread, every run's total, and the total computed from the medians."""
    runs_needed = scoring.batch["runs_per_applicant"]
    by_id = defaultdict(list)
    for r in records:
        by_id[r["applicant_id"]].append(r)
    out = []
    for a in intake["applicants"]:
        base = {"applicant_id": a["applicant_id"], "intake_flags": sorted({f["rule"] for f in a["flags"]})}
        if a["status"] == "held":
            out.append({**base, "status": "held", "stage": "intake",
                        "reasons": [f"{h['rule']}: {h['detail']}" for h in a["holds"]]})
            continue
        runs = sorted(by_id[a["applicant_id"]], key=lambda r: r["run"])
        failed = [r for r in runs if r["status"] != "scored"]
        if failed or len(runs) < runs_needed:
            out.append({**base, "status": "held", "stage": "scoring",
                        "reasons": [f"run {r['run']}: {r['reason']}" for r in failed] or ["incomplete"]})
            continue
        medians = {c: median(r["scores"][c] for r in runs) for c in CRITERIA}
        totals = [r["total"] for r in runs]
        out.append({
            **base,
            "status": "scored",
            "scores": medians,
            "spread": {c: max(r["scores"][c] for r in runs) - min(r["scores"][c] for r in runs) for c in CRITERIA},
            "run_totals": totals,
            "total_spread": round(max(totals) - min(totals), 2),
            "total": weighted_total(medians, scoring.weights_percent),
            "scoring_flags": sorted({f["rule"] for r in runs for f in r["flags"]}),
        })
    return out


def rank_batch(aggregates: list[dict], scoring: ScoringConfig) -> dict:
    cfg = scoring.ranking
    scored = [a for a in aggregates if a["status"] == "scored"]
    noise = noise_band([a["total_spread"] for a in scored], cfg["noise_percentile"], cfg["noise_floor"])
    ranking = build_ranking(scored, shortlist_size=cfg["shortlist_size"],
                            close_call_position=cfg["close_call_position"], noise=noise)
    return {"noise": noise, "ranking": ranking, "held": [a for a in aggregates if a["status"] == "held"]}


def render_summary(meta: dict, result: dict) -> str:
    ranking, held = result["ranking"], result["held"]
    lines = [f"# Scoring run {meta['run']}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items() if k != "run"]
    lines += [
        f"- noise band: {result['noise']} (90th percentile of run-to-run total spread, floor applied)",
        f"- ranked: {len(ranking)}, shortlisted: {sum(e['shortlisted'] for e in ranking)}, held: {len(held)}",
        "",
        "Equal totals share a rank and are never broken; tied applicants are listed by ID only.",
        "",
        "| Rank | Applicant | Total | Spread | Shortlist | Close call (#3) | Committee tie | Flags |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in ranking:
        flags = ", ".join(e["intake_flags"] + e["scoring_flags"])
        lines.append(
            f"| {e['rank']}{' (tied)' if e['tied'] else ''} | {e['applicant_id']} | {e['total']:.2f} | "
            f"{e['total_spread']:.2f} | {e['shortlist_reason'] or ''} | {'yes' if e['close_call'] else ''} | "
            f"{'yes' if e['committee_tie'] else ''} | {flags} |"
        )
    lines += ["", f"## Held ({len(held)})", ""]
    lines += [f"- {h['applicant_id']} ({h['stage']}): {'; '.join(h['reasons'])}" for h in held]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, llm: LLM | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score and rank every ready applicant (dry run unless --yes).")
    parser.add_argument("--yes", action="store_true", help="actually call the model")
    parser.add_argument("--resume", metavar="RUN", help="continue an earlier run")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args(argv)

    scoring = load_config()
    price_cfg = pricing.load_pricing()
    run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.runs_dir / run_id
    if args.resume and not run_dir.exists():
        print(f"No run {args.resume} in {args.runs_dir}")
        return 2
    workers = args.workers or scoring.batch["workers"]

    intake = run_intake(args.csv)
    calls, todo = planned(intake, scoring, load_done(run_dir / "calls.jsonl"))
    held = [a["applicant_id"] for a in intake["applicants"] if a["status"] == "held"]
    chars = sum(len(render_prompt(scoring.prompt, a["view"])) for a, _ in todo)
    est = pricing.estimate(chars, len(todo), price_cfg)
    print("\n".join([
        f"Batch scoring: prompt {scoring.prompt_version}, model {scoring.model}, scoring config {scoring.version}, "
        f"run {run_id}{' (resumed)' if args.resume else ''}",
        f"Applicants: {intake['counts']['applicants']}, {intake['counts']['ready']} ready, "
        f"{len(held)} held at intake ({', '.join(held) or 'none'})",
        f"Calls: {scoring.batch['runs_per_applicant']} runs per ready applicant = {len(calls)}, "
        f"{len(calls) - len(todo)} already done, {len(todo)} to make, {workers} at a time",
        "",
        *pricing.describe(est, price_cfg, scoring.model),
    ]))
    if not args.yes:
        print("\nDry run: nothing was called. Add --yes to run it.")
        return 0

    if llm is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("GEMINI_API_KEY isn't set")
            return 2
        b = scoring.batch["backoff"]
        llm = Backoff(GeminiClient(api_key), b["max_tries"], b["base_seconds"], b["cap_seconds"])
    log = AuditLog(args.audit)
    records = score_all(intake, llm, scoring, log, run_dir, run_id, workers)
    aggregates = aggregate(intake, records, scoring)
    result = rank_batch(aggregates, scoring)

    meta = {"run": run_id, "prompt_version": scoring.prompt_version, "prompt_sha256": scoring.prompt_sha256,
            "model": scoring.model, "scoring_config": scoring.version, "source_sha256": intake["source"]["sha256"],
            "intake_version": intake["intake_version"], "redaction_version": intake["redaction_version"]}
    ranking_doc = {"meta": meta, **result}
    (run_dir / "ranking.json").write_text(json.dumps(ranking_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(render_summary(meta, result), encoding="utf-8")
    log.append("batch_ranked", {
        **meta,
        "noise": result["noise"],
        "ranking_sha256": fingerprint(result["ranking"]),
        "shortlist": [e["applicant_id"] for e in result["ranking"] if e["shortlisted"]],
        "close_calls": [e["applicant_id"] for e in result["ranking"] if e["close_call"]],
        "committee_ties": [e["applicant_id"] for e in result["ranking"] if e["committee_tie"]],
        "held": [h["applicant_id"] for h in result["held"]],
        "rules": scoring.ranking,
    })
    print(f"\nRanked {len(result['ranking'])}, shortlisted {sum(e['shortlisted'] for e in result['ranking'])}, "
          f"held {len(result['held'])}. Results: {run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
