"""Score one applicant with the LLM: render the prompt, validate the reply, retry, total in code.

    python -m app.scoring SCH-001

This calls Gemini, which costs money. The model returns criterion scores only. The weighted
total is computed here from integer percentage weights, so it's exact and equal totals tie.
A reply that fails validation is never patched; the call is retried, and after the last failed
attempt the applicant is held for a person.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from app.audit import AuditLog
from app.intake import CRITERIA, run_intake
from app.llm import LLM, GeminiClient
from app.redaction import FREE_TEXT_FIELDS

CONFIDENCE = ("low", "medium", "high")
MAX_EVIDENCE = 3
PLACEHOLDER = "{{applicant_data}}"
# Evidence quotes must come from the applicant's or recommender's own text.
QUOTE_FIELDS = FREE_TEXT_FIELDS

DEFAULT_CSV = Path("data/raw/scholarship_candidates.csv")
DEFAULT_CONFIG = Path("config/scoring.json")
DEFAULT_AUDIT = Path("data/audit/audit.jsonl")


@dataclass(frozen=True)
class ScoringConfig:
    version: str
    model: str
    temperature: float
    max_attempts: int
    weights_percent: dict[str, int]
    prompt_version: str
    prompt: str

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def load_config(path: Path = DEFAULT_CONFIG, model: str | None = None) -> ScoringConfig:
    """Weights, temperature and attempts from the versioned config; the model from GEMINI_MODEL."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = raw["weights_percent"]
    if set(weights) != set(CRITERIA):
        raise ValueError(f"weights must cover exactly {', '.join(CRITERIA)}")
    if any(type(w) is not int or w < 0 for w in weights.values()) or sum(weights.values()) != 100:
        raise ValueError("weights_percent must be non-negative whole numbers summing to 100")
    if type(raw["max_attempts"]) is not int or raw["max_attempts"] < 1:
        raise ValueError("max_attempts must be a whole number of at least 1")
    model = model or os.environ.get("GEMINI_MODEL")
    if not model:
        raise ValueError("GEMINI_MODEL isn't set")
    prompt_path = Path(raw["prompt"])
    prompt = prompt_path.read_text(encoding="utf-8")
    if prompt.count(PLACEHOLDER) != 1:
        raise ValueError(f"{prompt_path} must contain {PLACEHOLDER} exactly once")
    return ScoringConfig(
        version=str(raw["version"]),
        model=model,
        temperature=float(raw["temperature"]),
        max_attempts=raw["max_attempts"],
        weights_percent=dict(weights),
        prompt_version=prompt_path.stem,
        prompt=prompt,
    )


def render_prompt(template: str, view: dict) -> str:
    """Every view field is present, null when missing. < and > are escaped (still valid JSON)
    so nothing in the application can close the <applicant_data> block early."""
    data = json.dumps(view, indent=2, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return template.replace(PLACEHOLDER, data)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def validate(text: str | None, view: dict) -> tuple[dict | None, list[str]]:
    """The criterion entries keyed by name, or the list of everything wrong with the reply."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None, ["reply is not valid JSON"]
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        return None, ["reply has no 'scores' list"]

    sources = [_normalize(view[f]) for f in QUOTE_FIELDS if isinstance(view.get(f), str)]
    fields = {_normalize(k) for k in view if k != "ref"}
    problems, by_criterion = [], {}
    for n, item in enumerate(data["scores"]):
        if not isinstance(item, dict):
            problems.append(f"scores[{n}] is not an object")
            continue
        c = item.get("criterion")
        if c not in CRITERIA:
            problems.append(f"scores[{n}]: unknown criterion {c!r}")
            continue
        if c in by_criterion:
            problems.append(f"{c}: scored more than once")
            continue
        by_criterion[c] = item
        score = item.get("score")
        if type(score) is not int or not 0 <= score <= 10:
            problems.append(f"{c}: score must be a whole number from 0 to 10, got {score!r}")
        if item.get("confidence") not in CONFIDENCE:
            problems.append(f"{c}: confidence must be one of {', '.join(CONFIDENCE)}")
        if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
            problems.append(f"{c}: rationale is missing")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
            problems.append(f"{c}: evidence must be a list of 1 to {MAX_EVIDENCE} items")
            continue
        for e in evidence:
            if not isinstance(e, str) or not e.strip():
                problems.append(f"{c}: empty evidence item")
            elif _normalize(e) not in fields and not any(_normalize(e) in s for s in sources):
                problems.append(f"{c}: evidence is neither a quote from the application nor a field: {e[:60]!r}")
    for c in CRITERIA:
        if c not in by_criterion:
            problems.append(f"{c}: not scored")
    if problems:
        return None, problems
    return {c: by_criterion[c] for c in CRITERIA}, []


_GENDERED = re.compile(r"\b(he|she|him|his|her|hers|himself|herself)\b", re.IGNORECASE)


def gendered_pronouns(criteria: dict[str, dict]) -> dict[str, list[str]]:
    """Rationales that call the applicant he or she. The view never says, so the model guessed.
    A flag for the reviewer, not a reason to reject the reply."""
    found = {}
    for c, item in criteria.items():
        words = sorted({w.lower() for w in _GENDERED.findall(item.get("rationale") or "")})
        if words:
            found[c] = words
    return found


def weighted_total(scores: dict[str, int], weights_percent: dict[str, int]) -> float:
    """Integer arithmetic, then one division, so equal inputs give exactly equal totals."""
    return sum(weights_percent[c] * scores[c] for c in CRITERIA) / 100


def score_applicant(applicant: dict, llm: LLM, config: ScoringConfig, log: AuditLog) -> dict:
    """One applicant, one call per attempt. Every attempt is logged, then the result or the hold."""
    view = applicant["view"]
    prompt = render_prompt(config.prompt, view)
    base = {
        "applicant_id": applicant["applicant_id"],
        "ref": applicant["ref"],
        "view_sha256": applicant["redaction"]["view_sha256"],
        "redaction_version": applicant["redaction"]["redaction_version"],
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_version": config.prompt_version,
        "prompt_sha256": config.prompt_sha256,
        "config_version": config.version,
        "model": config.model,
        "temperature": config.temperature,
    }
    entries, last_problem = [], None
    for attempt in range(1, config.max_attempts + 1):
        reply = llm.generate(prompt, model=config.model, temperature=config.temperature)
        scored, problems = (None, []) if reply.failure else validate(reply.text, view)
        entries.append(log.append("score_attempt", {
            **base,
            "attempt": attempt,
            "model_version": reply.model_version,
            "usage": reply.usage,
            "raw_output": reply.text,
            "failure": reply.failure,
            "problems": problems,
        })["seq"])
        if scored:
            scores = {c: scored[c]["score"] for c in CRITERIA}
            total = weighted_total(scores, config.weights_percent)
            flags = []
            if pronouns := gendered_pronouns(scored):
                flags.append({"rule": "gendered_pronoun_in_rationale", "found": pronouns})
            entries.append(log.append("score_result", {
                **base,
                "attempts": attempt,
                "scores": scores,
                "confidence": {c: scored[c]["confidence"] for c in CRITERIA},
                "weights_percent": config.weights_percent,
                "total": total,
                "flags": flags,
            })["seq"])
            return {
                "applicant_id": applicant["applicant_id"], "status": "scored", "attempts": attempt,
                "total": total, "criteria": scored, "flags": flags, "audit_seq": entries,
            }
        last_problem = reply.failure or "; ".join(problems)

    reason = f"no valid reply after {config.max_attempts} attempts; last: {last_problem}"
    entries.append(log.append("score_held", {**base, "attempts": config.max_attempts, "reason": reason})["seq"])
    return {"applicant_id": applicant["applicant_id"], "status": "held", "reason": reason, "audit_seq": entries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one applicant with Gemini (costs money).")
    parser.add_argument("applicant_id")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    matches = [a for a in run_intake(args.csv)["applicants"] if a["applicant_id"] == args.applicant_id]
    if not matches:
        print(f"{args.applicant_id}: not found in {args.csv}")
        return 2
    applicant = matches[0]
    if applicant["status"] == "held":
        reasons = "; ".join(f"{h['rule']}: {h['detail']}" for h in applicant["holds"])
        print(f"{args.applicant_id}: held at intake, not scored ({reasons})")
        return 1
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY isn't set")
        return 2

    result = score_applicant(applicant, GeminiClient(api_key), config, AuditLog(args.audit))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "scored" else 1


if __name__ == "__main__":
    sys.exit(main())
