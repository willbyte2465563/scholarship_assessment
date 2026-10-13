"""Offline tests for the eval harness. The model is always a fake here; nothing touches the network."""

import json
import re
from pathlib import Path

import pytest

from app.audit import verify
from app.intake import CRITERIA, NEED, READINESS, run_intake
from app.llm import LLMReply
from app.redaction import redact
from app.scoring import load_config
from evals import metrics, perturb
from evals.run import (
    describe, estimate, execute, load_golden, main, plan_golden, plan_suite, summarize_golden, summarize_suite,
)
from tests.conftest import make_row, write_csv
from tests.fakes import good_reply

SEED = 7
CFG = {
    "version": "test", "seed": SEED, "sample_size": 2, "consistency_runs": 3, "golden_runs": 3,
    "thresholds": {"criterion": 1, "total": 0.5, "need_gap": 2, "readiness_drop": 1, "isolation": 1},
    "pricing": {"model": "gemini-test", "input_usd_per_million": 0.75, "output_usd_per_million": 3.75,
                "note": "test price", "source": "https://example.test/pricing", "checked": "2026-10-13"},
    "estimate": {"chars_per_token": 4, "output_tokens_per_call": 500, "thinking_tokens_per_call": 1200,
                 "measured_from": "test",
                 "retry_margin": 0.1},
}
NO_FIXED = {"same_application_different_name": [], "same_content_different_fluency": [], "instruction_removed": {}}
ROUGH_ESSAY = "The team was ready, and the coach was proud. We won the game. It was a good day, and I was happy."


class ScriptedLLM:
    """Echoes rewrites, scores need from the need statement, and calls the applicant 'his' when it
    sees a sports mention, so every eval path can be exercised."""

    def __init__(self, block_if: str | None = None):
        self.block_if = block_if
        self.calls = 0

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        self.calls += 1
        if self.block_if and self.block_if in prompt:
            return LLMReply(None, "blocked: SAFETY")
        if "<essay>" in prompt and "Rewrite it" in prompt:
            essay = json.loads(prompt.rsplit("<essay>", 1)[1].split("</essay>")[0])
            return LLMReply(json.dumps({"essay": essay}), None, "fake", {"input": 100, "output": 50, "thinking": 0})
        need = 9 if "no savings" in prompt else 2 if "cover the full cost" in prompt else 5
        reply = good_reply(scores=(7, need, 6, 5, 6))
        if "Varsity football" in prompt:
            reply["scores"][0]["rationale"] = "His grades are solid."
        return LLMReply(json.dumps(reply), None, "fake", {"input": 1000, "output": 400, "thinking": 100})


@pytest.fixture
def csv_path(tmp_path) -> Path:
    rows = [
        make_row("E-001", 1, name="Ana Ruiz", essay_text=ROUGH_ESSAY + " Ana Ruiz agreed. Ana smiled.",
                 recommendation_summary="She is careful. Her work is exact."),
        make_row("E-002", 2, essay_text=ROUGH_ESSAY.replace("team", "crew")),
        make_row("E-003", 3, essay_text=""),
    ]
    return write_csv(tmp_path / "applicants.csv", rows)


@pytest.fixture
def intake(csv_path):
    return run_intake(csv_path)


@pytest.fixture
def raw(csv_path):
    from evals.run import load_raw
    return load_raw(csv_path)


@pytest.fixture
def scoring():
    return load_config(model="gemini-test")


# Perturbations


def test_name_and_pronoun_swap_leaves_the_view_unchanged(raw, intake):
    a = next(x for x in intake["applicants"] if x["applicant_id"] == "E-001")
    swapped_row = perturb.swap_identity(raw["E-001"], SEED)
    assert swapped_row["name"] != "Ana Ruiz"
    assert "Ana" not in swapped_row["essay_text"]
    assert re.search(r"\bHe\b", swapped_row["recommendation_summary"])
    assert redact(swapped_row, a["ref"])["view_sha256"] == a["redaction"]["view_sha256"]


def test_rough_essay_is_deterministic_and_changes_only_the_essay(intake):
    view = intake["applicants"][0]["view"]
    first, again = perturb.rough_essay(view, SEED), perturb.rough_essay(view, SEED)
    assert first == again
    assert first["essay_text"] != view["essay_text"]
    assert {k: v for k, v in first.items() if k != "essay_text"} == {k: v for k, v in view.items() if k != "essay_text"}


def test_rough_essay_with_nothing_to_roughen():
    assert perturb.rough_essay({"essay_text": "I work nights so I study on my break"}, SEED) is None


def test_additions_append_without_touching_other_fields():
    view = {"extracurriculars": None, "recommendation_summary": "Reliable.", "financial_need_statement": "x",
            "work_hours_per_week": None}
    assert perturb.add_sports(view)["extracurriculars"] == perturb.SPORTS_ACTIVITY
    assert perturb.add_fancy_school(view)["recommendation_summary"] == f"Reliable. {perturb.FANCY_SCHOOL}"
    assert perturb.add_injection(view)["recommendation_summary"].endswith(perturb.INJECTION)
    assert perturb.set_need(view, high=True)["financial_need_statement"] == perturb.NEED_HIGH
    assert perturb.more_work_hours(view)["work_hours_per_week"] == 20
    assert view["recommendation_summary"] == "Reliable."


def test_the_injection_is_one_intake_would_flag():
    assert perturb.flagged_by_intake(perturb.INJECTION)
    assert perturb.flagged_by_intake("Practical and reliable.") == []


@pytest.mark.parametrize("gpa, expected_gpa, expected_rank", [(3.9, 2.8, "top 60%"), (3.0, 3.96, "top 2%"), (None, 3.96, "top 2%")])
def test_swap_academics_flips_strong_and_weak(gpa, expected_gpa, expected_rank):
    view = {"gpa": gpa, "gpa_scale": 4, "test_score": 1300, "class_rank_percentile": "top 10%", "financial_need_statement": "x"}
    swapped = perturb.swap_academics(view)
    assert (swapped["gpa"], swapped["class_rank_percentile"]) == (expected_gpa, expected_rank)
    assert swapped["financial_need_statement"] == "x"


# Pass/fail rules


def _run(scores, total=None):
    s = dict(zip(CRITERIA, scores))
    return {"scores": s, "total": total if total is not None else sum(s.values()) / 5}


@pytest.mark.parametrize("delta, spread_, failed", [(1, 0, False), (2, 0, True), (2, 2, False), (3, 2, True)])
def test_a_move_fails_only_beyond_both_threshold_and_spread(delta, spread_, failed):
    base, variant = _run((5, 5, 5, 5, 5), 5), _run((5 + delta, 5, 5, 5, 5), 5)
    noise = {**dict.fromkeys(CRITERIA, 0), READINESS: spread_, "total": 0}
    row = metrics.invariance(base, variant, noise, CFG["thresholds"])["by_criterion"][READINESS]
    assert row["failed"] is failed
    assert (row["delta"], row["threshold"], row["spread"]) == (delta, 1, spread_)


@pytest.mark.parametrize("high, low, spread_, passed", [(8, 5, 0, True), (6, 5, 0, False), (7, 5, 3, False)])
def test_need_must_move_by_more_than_threshold_and_noise(high, low, spread_, passed):
    noise = {**dict.fromkeys(CRITERIA, 0), NEED: spread_}
    assert metrics.need_moves(_run((5, high, 5, 5, 5)), _run((5, low, 5, 5, 5)), noise, CFG["thresholds"])["passed"] is passed


def test_spread_and_baseline():
    runs = [_run((5, 5, 5, 5, 5), 5.0), _run((7, 5, 5, 5, 5), 5.6), _run((6, 5, 5, 5, 5), 5.3)]
    assert metrics.spread(runs)[READINESS] == 2 and metrics.spread(runs)["total"] == 0.6
    assert metrics.baseline(runs)["scores"][READINESS] == 6 and metrics.baseline(runs)["total"] == 5.3


def test_golden_agreement(scoring):
    model = {"X": dict(zip(CRITERIA, (7, 5, 5, 5, 5))), "Y": dict(zip(CRITERIA, (3, 3, 3, 3, 3)))}
    human = {"X": dict(zip(CRITERIA, (5, 5, 5, 5, 5))), "Y": dict(zip(CRITERIA, (3, 4, 3, 3, 3)))}
    ag = metrics.golden_agreement(model, human, scoring.weights_percent)
    assert ag["by_criterion"][READINESS] == {"mean_abs_gap": 1.0, "within_1": "1/2"}
    assert ag["same_order"] is True


# Planning and the dry run


def test_plan_covers_every_eval_for_each_sampled_applicant(intake, raw):
    plan = plan_suite(CFG, intake, raw, NO_FIXED, rewrites={})
    assert plan["sample"] == ["E-001", "E-002"]  # E-003 is held at intake
    per_applicant = {"consistency": 3, "rough_essay": 1, "plain_essay": 1, "sports_mention": 1, "fancy_school": 1,
                     "injected_instruction": 1, "need": 2, "work_hours": 1, "criterion_isolation": 1}
    for i in plan["sample"]:
        counts = {}
        for c in plan["calls"]:
            if c.applicant_id == i:
                counts[c.eval] = counts.get(c.eval, 0) + 1
        assert counts == per_applicant
    assert plan["rewrites_needed"] == ["E-001", "E-002"]
    assert {n["eval"] for n in plan["notes"]} == {"name_pronoun_swap"}  # free passes, no calls


def test_dry_run_estimate_names_its_price_source(intake, raw, scoring):
    plan = plan_suite(CFG, intake, raw, NO_FIXED, rewrites={})
    est = estimate(CFG, scoring, plan)
    assert est["calls"] == 24 + 2
    assert est["output_tokens"] == 26 * 1700
    assert est["usd"] < est["usd_with_retries"]
    text = describe(CFG, scoring, plan, est, "Eval suite")
    assert "https://example.test/pricing" in text and "checked 2026-10-13" in text
    assert "WARNING" not in text


def test_dry_run_command_calls_nothing(csv_path, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")

    class Boom:
        def generate(self, *a, **k):
            raise AssertionError("the dry run called the model")

    assert main(["--csv", str(csv_path), "--results", str(tmp_path / "results")], llm=Boom()) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was called" in out and "Source: https://ai.google.dev/gemini-api/docs/pricing" in out
    assert "Golden set: not part of the suite" in out
    assert not (tmp_path / "results").exists()


def test_golden_needs_hand_scores(csv_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_MODEL", "m")
    monkeypatch.setattr("evals.run.GOLDEN", Path("does/not/exist"))
    assert main(["--golden", "tuning", "--csv", str(csv_path)]) == 2
    assert "No hand scores yet" in capsys.readouterr().out


def test_golden_rows_are_validated(tmp_path, monkeypatch):
    monkeypatch.setattr("evals.run.GOLDEN", tmp_path)
    (tmp_path / "tuning.jsonl").write_text(json.dumps({"applicant_id": "E-001", "scores": {"academic_readiness": 11}}))
    with pytest.raises(ValueError, match="whole-number score"):
        load_golden("tuning")


# Running against the fake


def test_suite_runs_end_to_end(intake, raw, scoring, tmp_path):
    plan = plan_suite(CFG, intake, raw, NO_FIXED, rewrites={})
    results = execute(plan, ScriptedLLM(), scoring, tmp_path / "run", cache_path=tmp_path / "cache.json")
    summary = summarize_suite(CFG, plan, results)

    assert set(json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))) == {"E-001", "E-002"}
    assert verify(tmp_path / "run" / "audit.jsonl") is None
    evals = summary["evals"]
    for e in ("name_pronoun_swap", "rough_essay", "plain_essay", "fancy_school", "need", "work_hours", "criterion_isolation"):
        assert evals[e] == {**evals[e], "passed": 2, "failed": 0}, e
    assert evals["injected_instruction"]["results"][0]["flagged_at_intake"]
    assert summary["consistency"]["E-001"]["spread"][READINESS] == 0
    assert {p["eval"] for p in summary["pronoun_flags"]} == {"sports_mention"}
    assert summary["held"] == []


def test_held_calls_are_reported_not_counted_as_passes(intake, raw, scoring, tmp_path):
    plan = plan_suite(CFG, intake, raw, NO_FIXED, rewrites={})
    results = execute(plan, ScriptedLLM(block_if="no savings"), scoring, tmp_path / "run", cache_path=tmp_path / "c.json")
    summary = summarize_suite(CFG, plan, results)
    assert summary["evals"]["need"]["passed"] == 0
    assert all(r["passed"] is None for r in summary["evals"]["need"]["results"])
    assert {h["eval"] for h in summary["held"]} == {"need"}


def test_golden_runs_three_times_and_compares_medians(intake, scoring, tmp_path):
    golden = {"E-001": dict(zip(CRITERIA, (7, 5, 6, 5, 6)))}
    plan = plan_golden(CFG, intake, golden)
    assert len(plan["calls"]) == 3
    results = execute(plan, ScriptedLLM(), scoring, tmp_path / "golden", cache_path=tmp_path / "c.json")
    summary = summarize_golden(scoring, plan, results, golden)
    assert summary["agreement"]["by_criterion"][READINESS] == {"mean_abs_gap": 0.0, "within_1": "1/1"}
