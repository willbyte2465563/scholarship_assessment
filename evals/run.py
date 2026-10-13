"""Run the evals. Without --yes it's a dry run: the sample, every planned call and the estimated
cost. Nothing is called.

    python -m evals.run                          # dry run of the eval suite
    python -m evals.run --yes                    # run it (calls Gemini, costs money)
    python -m evals.run --golden tuning          # dry run of the golden comparison
    python -m evals.run --golden tuning --yes
    python -m evals.run --golden holdout --yes   # the final check, once a prompt is settled

Results go to evals/results/<prompt_version>/<run>/: every call (calls.jsonl), a summary (JSON and
markdown) and the run's own audit log. Production's audit log is never touched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.audit import AuditLog
from app.intake import CRITERIA, NEED, READINESS, run_intake
from app.llm import LLM, GeminiClient
from app.redaction import fingerprint, redact
from app.scoring import DEFAULT_CSV, ScoringConfig, load_config, render_prompt, score_applicant
from evals import metrics, perturb

EVALS = Path("evals")
CONFIG = EVALS / "config.json"
FIXED = EVALS / "fixtures" / "fixed_cases.json"
GOLDEN = EVALS / "golden"
RESULTS = EVALS / "results"
REWRITE_PROMPT = Path("prompts/rewrite_plain_v1.md")
REWRITE_CACHE = EVALS / "cache" / "rewrite_plain_v1.json"

# Evals where the score shouldn't move, compared against the applicant's own baseline.
INVARIANT = ["name_pronoun_swap", "rough_essay", "plain_essay", "sports_mention", "fancy_school", "injected_instruction"]
PAIRS = ["same_application_different_name", "same_content_different_fluency"]
PLACEHOLDERS = ["[applicant]", "[city]", "[high school]"]
REWRITE_LENGTH = (0.75, 1.25)  # the prompt asks for within 20%; a little slack before retrying


@dataclass
class Call:
    eval: str
    applicant_id: str
    variant: str
    view: dict | None  # None until its essay rewrite exists


def _sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_raw(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {(r.get("applicant_id") or "").strip(): r for r in csv.DictReader(f)}


def load_golden(split: str) -> dict[str, dict]:
    path = GOLDEN / f"{split}.jsonl"
    if not path.exists():
        return {}
    golden = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        scores = row.get("scores", {})
        if set(scores) != set(CRITERIA) or any(type(s) is not int or not 0 <= s <= 10 for s in scores.values()):
            raise ValueError(f"{path}:{n}: needs a whole-number score from 0 to 10 for each of {', '.join(CRITERIA)}")
        golden[row["applicant_id"]] = scores
    return golden


def plan_suite(cfg: dict, intake: dict, raw: dict[str, dict], fixed: dict, rewrites: dict) -> dict:
    ready = {a["applicant_id"]: a for a in intake["applicants"] if a["status"] == "ready"}
    fixed_ids = {i for group in PAIRS for pair in fixed[group] for i in pair} | set(fixed["instruction_removed"])
    pool = sorted(set(ready) - fixed_ids)
    sample = sorted(random.Random(cfg["seed"]).sample(pool, min(cfg["sample_size"], len(pool))))
    seed, calls, notes = cfg["seed"], [], []

    for i in sample:
        a, view = ready[i], ready[i]["view"]
        calls += [Call("consistency", i, f"run {k}", view) for k in range(1, cfg["consistency_runs"] + 1)]
        swapped = redact(perturb.swap_identity(raw[i], seed), a["ref"])
        if swapped["view_sha256"] == a["redaction"]["view_sha256"]:
            notes.append({"eval": "name_pronoun_swap", "applicant_id": i,
                          "note": "view identical after redaction, so it passes with no call"})
        else:
            calls.append(Call("name_pronoun_swap", i, "swapped", swapped["view"]))
        rough = perturb.rough_essay(view, seed)
        if rough:
            calls.append(Call("rough_essay", i, "rough", rough))
        else:
            notes.append({"eval": "rough_essay", "applicant_id": i,
                          "note": "essay is already unpolished (few articles or commas), so roughening changed nothing"})
        cached = rewrites.get(i)
        fresh = cached and cached["source_sha256"] == _sha(view["essay_text"])
        calls.append(Call("plain_essay", i, "plain", perturb.with_essay(view, cached["essay"]) if fresh else None))
        calls += [
            Call("sports_mention", i, "added", perturb.add_sports(view)),
            Call("fancy_school", i, "added", perturb.add_fancy_school(view)),
            Call("injected_instruction", i, "added", perturb.add_injection(view)),
            Call("need", i, "high", perturb.set_need(view, high=True)),
            Call("need", i, "low", perturb.set_need(view, high=False)),
            Call("work_hours", i, f"+{perturb.EXTRA_WORK_HOURS} hours", perturb.more_work_hours(view)),
            Call("criterion_isolation", i, "academics swapped", perturb.swap_academics(view)),
        ]

    for group in PAIRS:
        for first, second in fixed[group]:
            if first in ready and second in ready:
                calls += [Call(group, first, "first", ready[first]["view"]),
                          Call(group, second, "second", ready[second]["view"])]
            else:
                notes.append({"eval": group, "applicant_id": f"{first}/{second}", "note": "not ready at intake; skipped"})
    for i, overrides in fixed["instruction_removed"].items():
        if i in ready:
            calls += [Call("instruction_removed", i, "as submitted", ready[i]["view"]),
                      Call("instruction_removed", i, "removed", {**ready[i]["view"], **overrides})]
        else:
            notes.append({"eval": "instruction_removed", "applicant_id": i, "note": "not ready at intake; skipped"})

    return {"sample": sample, "calls": calls, "notes": notes, "ready": ready,
            "pairs": {g: fixed[g] for g in PAIRS}, "instruction_removed": list(fixed["instruction_removed"]),
            "rewrites_needed": [c.applicant_id for c in calls if c.view is None]}


def plan_golden(cfg: dict, intake: dict, golden: dict) -> dict:
    ready = {a["applicant_id"]: a for a in intake["applicants"] if a["status"] == "ready"}
    missing = sorted(set(golden) - set(ready))
    if missing:
        raise ValueError(f"golden applicants not ready at intake: {', '.join(missing)}")
    calls = [Call("golden", i, f"run {k}", ready[i]["view"])
             for i in sorted(golden) for k in range(1, cfg["golden_runs"] + 1)]
    return {"sample": sorted(golden), "calls": calls, "notes": [], "ready": ready, "rewrites_needed": []}


def estimate(cfg: dict, scoring: ScoringConfig, plan: dict) -> dict:
    est, price = cfg["estimate"], cfg["pricing"]
    chars = sum(len(render_prompt(scoring.prompt, c.view or plan["ready"][c.applicant_id]["view"]))
                for c in plan["calls"])
    if plan["rewrites_needed"]:
        template = REWRITE_PROMPT.read_text(encoding="utf-8")
        chars += sum(len(template) + len(plan["ready"][i]["view"]["essay_text"]) for i in plan["rewrites_needed"])
    calls = len(plan["calls"]) + len(plan["rewrites_needed"])
    margin = 1 + est["retry_margin"]
    tokens_in = chars / est["chars_per_token"] * margin
    out_low = calls * est["output_tokens_per_call"] * margin
    out_high = calls * (est["output_tokens_per_call"] + est["thinking_tokens_per_call_high"]) * margin

    def usd(tokens_out: float) -> float:
        return (tokens_in * price["input_usd_per_million"] + tokens_out * price["output_usd_per_million"]) / 1e6

    return {"calls": calls, "calls_with_retries": math.ceil(calls * margin), "input_tokens": round(tokens_in),
            "output_tokens": [round(out_low), round(out_high)], "usd": [round(usd(out_low), 2), round(usd(out_high), 2)]}


def describe(cfg: dict, scoring: ScoringConfig, plan: dict, est: dict, title: str) -> str:
    price = cfg["pricing"]
    counts: dict[str, int] = defaultdict(int)
    for c in plan["calls"]:
        counts[c.eval] += 1
    lines = [
        f"{title}: prompt {scoring.prompt_version}, model {scoring.model}, scoring config {scoring.version}, "
        f"eval config {cfg['version']}, seed {cfg['seed']}",
        f"Applicants ({len(plan['sample'])}): {', '.join(plan['sample'])}",
        "",
        "Planned calls:",
    ]
    lines += [f"  {name:<34}{n:>4}" for name, n in counts.items()]
    if plan["rewrites_needed"]:
        lines.append(f"  {'plain-essay rewrites (cached after)':<34}{len(plan['rewrites_needed']):>4}")
    lines.append(f"  {'total':<34}{est['calls']:>4}   (up to {est['calls_with_retries']} with retries)")
    for note in plan["notes"]:
        lines.append(f"  note: {note['eval']} {note['applicant_id']}: {note['note']}")
    lines += [
        "",
        f"Estimated tokens: ~{est['input_tokens']:,} input, ~{est['output_tokens'][0]:,} to "
        f"~{est['output_tokens'][1]:,} output (the high end allows "
        f"{cfg['estimate']['thinking_tokens_per_call_high']:,} thinking tokens per call; real usage is logged)",
        f"Estimated cost: ${est['usd'][0]:.2f} to ${est['usd'][1]:.2f}, including a "
        f"{cfg['estimate']['retry_margin']:.0%} retry margin",
        f"Price: ${price['input_usd_per_million']}/M input, ${price['output_usd_per_million']}/M output for "
        f"{price['model']}. {price['note']}",
        f"Source: {price['source']} (checked {price['checked']})",
    ]
    if price["model"] != scoring.model:
        lines.append(f"WARNING: the price is for {price['model']} but scoring uses {scoring.model}")
    return "\n".join(lines)


def _rewrite(llm: LLM, scoring: ScoringConfig, log: AuditLog, applicant_id: str, essay: str) -> str | None:
    template = REWRITE_PROMPT.read_text(encoding="utf-8")
    data = json.dumps(essay, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    prompt = template.replace("{{essay}}", data)
    words = len(essay.split())
    for attempt in range(1, scoring.max_attempts + 1):
        reply = llm.generate(prompt, model=scoring.model, temperature=scoring.temperature)
        text, problem = None, reply.failure
        if not problem:
            try:
                text = json.loads(reply.text or "").get("essay")
            except (ValueError, AttributeError):
                problem = "reply is not a JSON object"
        if not problem:
            if not isinstance(text, str) or not text.strip():
                problem = "no essay in the reply"
            elif not REWRITE_LENGTH[0] <= len(text.split()) / words <= REWRITE_LENGTH[1]:
                problem = f"length changed too much ({len(text.split())} words vs {words})"
            elif any(essay.count(p) != text.count(p) for p in PLACEHOLDERS):
                problem = "placeholders changed"
        log.append("rewrite_attempt", {
            "applicant_id": applicant_id, "prompt_version": REWRITE_PROMPT.stem, "prompt_sha256": _sha(template),
            "source_sha256": _sha(essay), "model": scoring.model, "attempt": attempt, "usage": reply.usage,
            "failure": reply.failure, "problem": problem, "raw_output": reply.text,
        })
        if not problem:
            return text.strip()
    return None


def execute(plan: dict, llm: LLM, scoring: ScoringConfig, out_dir: Path, cache_path: Path = REWRITE_CACHE) -> list[dict]:
    """Generate any missing rewrites, then score every planned call. Returns one result per call."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(out_dir / "audit.jsonl")
    if plan["rewrites_needed"]:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        for i in plan["rewrites_needed"]:
            essay = plan["ready"][i]["view"]["essay_text"]
            text = _rewrite(llm, scoring, log, i, essay)
            if text:
                cache[i] = {"source_sha256": _sha(essay), "essay": text, "model": scoring.model,
                            "prompt_sha256": _sha(REWRITE_PROMPT.read_text(encoding="utf-8"))}
                for c in plan["calls"]:
                    if c.eval == "plain_essay" and c.applicant_id == i:
                        c.view = perturb.with_essay(plan["ready"][i]["view"], text)
            else:
                plan["notes"].append({"eval": "plain_essay", "applicant_id": i, "note": "rewrite failed; skipped"})
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    results = []
    for c in plan["calls"]:
        if c.view is None:
            continue
        base = plan["ready"][c.applicant_id]
        applicant = {
            "applicant_id": f"{c.applicant_id} [{c.eval}: {c.variant}]",
            "ref": base["ref"],
            "redaction": {"view_sha256": fingerprint({k: v for k, v in c.view.items() if k != "ref"}),
                          "redaction_version": base["redaction"]["redaction_version"]},
            "view": c.view,
        }
        r = score_applicant(applicant, llm, scoring, log)
        results.append({
            "eval": c.eval, "applicant_id": c.applicant_id, "variant": c.variant, "status": r["status"],
            "scores": {k: r["criteria"][k]["score"] for k in CRITERIA} if r["status"] == "scored" else None,
            "total": r.get("total"), "flags": r.get("flags", []), "reason": r.get("reason"),
        })
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return results


def usage_totals(audit_path: Path, price: dict) -> dict:
    totals = {"input": 0, "output": 0, "thinking": 0}
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        for k, v in (json.loads(line).get("usage") or {}).items():
            totals[k] += v or 0
    usd = (totals["input"] * price["input_usd_per_million"]
           + (totals["output"] + totals["thinking"]) * price["output_usd_per_million"]) / 1e6
    return {"tokens": totals, "usd": round(usd, 4)}


def summarize_suite(cfg: dict, plan: dict, results: list[dict]) -> dict:
    th = cfg["thresholds"]
    found: dict[tuple, dict] = {}
    for r in results:
        if r["status"] == "scored":
            found[(r["eval"], r["applicant_id"], r["variant"])] = r

    def one(e: str, i: str) -> dict | None:
        return next((r for (ev, ai, _), r in found.items() if ev == e and ai == i), None)

    def not_run(i: str) -> dict:
        return {"applicant_id": i, "passed": None, "note": "a call was held; see held calls"}

    noise, base = {}, {}
    for i in plan["sample"]:
        runs = [r for (ev, ai, _), r in found.items() if ev == "consistency" and ai == i]
        if len(runs) == cfg["consistency_runs"]:
            noise[i], base[i] = metrics.spread(runs), metrics.baseline(runs)
    pooled = metrics.pooled_spread(list(noise.values()))
    free = {n["applicant_id"] for n in plan["notes"] if n["eval"] == "name_pronoun_swap"}
    injection_flagged = perturb.flagged_by_intake(perturb.INJECTION)

    evals: dict[str, list] = defaultdict(list)
    for i in plan["sample"]:
        if i not in base:
            evals["consistency"].append({"applicant_id": i, "passed": None, "note": "a baseline run was held"})
            continue
        for e in INVARIANT:
            if e == "name_pronoun_swap" and i in free:
                evals[e].append({"applicant_id": i, "passed": True, "note": "view identical after redaction"})
                continue
            v = one(e, i)
            if not v:
                if any(c.eval == e and c.applicant_id == i and c.view for c in plan["calls"]):
                    evals[e].append(not_run(i))
                continue
            result = {"applicant_id": i, **metrics.invariance(base[i], v, noise[i], th)}
            if e == "injected_instruction":
                result["flagged_at_intake"] = injection_flagged
                result["passed"] = result["passed"] and bool(injection_flagged)
            evals[e].append(result)
        high, low = found.get(("need", i, "high")), found.get(("need", i, "low"))
        evals["need"].append({"applicant_id": i, **metrics.need_moves(high, low, noise[i], th)} if high and low else not_run(i))
        v = one("work_hours", i)
        evals["work_hours"].append(
            {"applicant_id": i, **metrics.no_drop(base[i], v, READINESS, noise[i], th["readiness_drop"])} if v else not_run(i))
        v = one("criterion_isolation", i)
        evals["criterion_isolation"].append(
            {"applicant_id": i, **metrics.stays_put(base[i], v, NEED, noise[i], th["isolation"])} if v else not_run(i))

    # Fixed cases have no repeats of their own, so they're judged against the sample's widest spread.
    for e in PAIRS:
        for first, second in plan["pairs"][e]:
            a, b = found.get((e, first, "first")), found.get((e, second, "second"))
            label = f"{first} vs {second}"
            evals[e].append({"applicant_id": label, **metrics.invariance(a, b, pooled, th)} if a and b else not_run(label))
    for i in plan["instruction_removed"]:
        clean, submitted = found.get(("instruction_removed", i, "removed")), found.get(("instruction_removed", i, "as submitted"))
        evals["instruction_removed"].append(
            {"applicant_id": i, **metrics.invariance(clean, submitted, pooled, th)} if clean and submitted else not_run(i))

    return {
        "consistency": {i: {"baseline": base[i], "spread": noise[i]} for i in base},
        "pooled_spread": pooled,
        "evals": {e: {"passed": sum(1 for x in rows if x["passed"]), "failed": sum(1 for x in rows if x["passed"] is False),
                      "results": rows} for e, rows in evals.items()},
        "pronoun_flags": [
            {"eval": r["eval"], "applicant_id": r["applicant_id"], "variant": r["variant"], "found": f["found"]}
            for r in results for f in r["flags"] if f["rule"] == "gendered_pronoun_in_rationale"
        ],
        "held": [{k: r[k] for k in ("eval", "applicant_id", "variant", "reason")} for r in results if r["status"] == "held"],
        "notes": plan["notes"],
    }


def summarize_golden(scoring: ScoringConfig, plan: dict, results: list[dict], golden: dict) -> dict:
    by_id = defaultdict(list)
    for r in results:
        if r["status"] == "scored":
            by_id[r["applicant_id"]].append(r)
    model = {i: metrics.baseline(runs)["scores"] for i, runs in by_id.items()}
    return {
        "agreement": metrics.golden_agreement(model, golden, scoring.weights_percent),
        "model_medians": model,
        "spread": {i: metrics.spread(runs) for i, runs in by_id.items()},
        "held": [r["applicant_id"] for r in results if r["status"] == "held"],
    }


def _fmt_move(row: dict) -> str:
    mark = " ✗" if row["failed"] else ""
    return f"{row['delta']:+g} ({row['threshold']:g}/{row['spread']:g}){mark}"


def render_markdown(meta: dict, summary: dict) -> str:
    lines = [f"# Eval run {meta['run']}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items() if k != "run"]
    lines += ["", "Moves are shown as `change (threshold/spread)`. A shouldn't-change move fails (✗) only when "
              "it's bigger than both its threshold and the measured run-to-run spread.", ""]
    if "agreement" in summary:
        ag = summary["agreement"]
        lines += ["## Agreement with hand scores", "",
                  f"Applicants: {', '.join(ag['applicants'])}. Total gap: {ag['total_mean_abs_gap']}. "
                  f"Same order: {'yes' if ag['same_order'] else 'no'}.", "",
                  "| Criterion | Mean gap | Within 1 |", "|---|---|---|"]
        lines += [f"| {c} | {v['mean_abs_gap']} | {v['within_1']} |" for c, v in ag["by_criterion"].items()]
        return "\n".join(lines) + "\n"

    lines += ["## Consistency (3 runs of the same input)", "",
              "| Applicant | " + " | ".join(CRITERIA) + " | total |", "|---" * (len(CRITERIA) + 2) + "|"]
    for i, c in summary["consistency"].items():
        lines.append(f"| {i} | " + " | ".join(str(c["spread"][k]) for k in CRITERIA) + f" | {c['spread']['total']} |")
    lines += ["", "## Results", "", "| Eval | Passed | Failed |", "|---|---|---|"]
    lines += [f"| {e} | {v['passed']} | {v['failed']} |" for e, v in summary["evals"].items()]
    for e, v in summary["evals"].items():
        lines += ["", f"### {e}", ""]
        for row in v["results"]:
            verdict = {True: "pass", False: "FAIL", None: "not run"}[row["passed"]]
            if "by_criterion" in row:
                moves = ", ".join(f"{c}: {_fmt_move(m)}" for c, m in row["by_criterion"].items())
                lines.append(f"- {row['applicant_id']}: {verdict}. {moves}")
            elif "gap" in row:
                lines.append(f"- {row['applicant_id']}: {verdict}. need high {row['high']}, low {row['low']}, "
                             f"gap {row['gap']} (needs ≥ {row['threshold']}, spread {row['spread']})")
            elif "criterion" in row:
                lines.append(f"- {row['applicant_id']}: {verdict}. {row['criterion']} {row['before']} → {row['after']} "
                             f"({row['delta']:+g}; threshold {row['threshold']}, spread {row['spread']})")
            else:
                lines.append(f"- {row['applicant_id']}: {verdict}. {row.get('note', '')}")
    lines += ["", f"## Gendered pronouns in rationales: {len(summary['pronoun_flags'])}", ""]
    lines += [f"- {p['applicant_id']} [{p['eval']}: {p['variant']}]: {p.get('found')}" for p in summary["pronoun_flags"]]
    lines += ["", f"## Held calls: {len(summary['held'])}", ""]
    lines += [f"- {h['applicant_id']} [{h['eval']}: {h['variant']}]: {h['reason']}" for h in summary["held"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, llm: LLM | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evals (dry run unless --yes).")
    parser.add_argument("--yes", action="store_true", help="actually call the model")
    parser.add_argument("--golden", choices=["tuning", "holdout"])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    scoring = load_config()
    intake = run_intake(args.csv)
    if args.golden:
        golden = load_golden(args.golden)
        if not golden:
            print(f"No hand scores yet: add {GOLDEN / (args.golden + '.jsonl')} (format in {GOLDEN / 'README.md'}).")
            return 2
        plan = plan_golden(cfg, intake, golden)
        title = f"Golden comparison ({args.golden})"
    else:
        fixed = json.loads(FIXED.read_text(encoding="utf-8"))
        rewrites = json.loads(REWRITE_CACHE.read_text(encoding="utf-8")) if REWRITE_CACHE.exists() else {}
        plan = plan_suite(cfg, intake, load_raw(args.csv), fixed, rewrites)
        title = "Eval suite"
    est = estimate(cfg, scoring, plan)
    print(describe(cfg, scoring, plan, est, title))
    if not args.golden:
        added = [s for s in ("tuning", "holdout") if (GOLDEN / f"{s}.jsonl").exists()]
        print(f"\nGolden set: not part of the suite; run --golden tuning separately. "
              f"Hand scores added so far: {', '.join(added) or 'none'}.")
    if not args.yes:
        print("\nDry run: nothing was called. Add --yes to run it.")
        return 0

    if llm is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("GEMINI_API_KEY isn't set")
            return 2
        llm = GeminiClient(api_key)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + (f"-golden-{args.golden}" if args.golden else "-suite")
    out_dir = args.results / scoring.prompt_version / run
    results = execute(plan, llm, scoring, out_dir)
    summary = summarize_golden(scoring, plan, results, golden) if args.golden else summarize_suite(cfg, plan, results)
    meta = {"run": run, "prompt_version": scoring.prompt_version, "prompt_sha256": scoring.prompt_sha256,
            "model": scoring.model, "scoring_config": scoring.version, "eval_config": cfg["version"],
            "seed": cfg["seed"], "applicants": ", ".join(plan["sample"]),
            "usage": usage_totals(out_dir / "audit.jsonl", cfg["pricing"])}
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, **summary}, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    (out_dir / "summary.md").write_text(render_markdown(meta, summary), encoding="utf-8")
    print(f"\nResults: {out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
