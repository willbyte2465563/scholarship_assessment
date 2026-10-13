"""Reviewer state read back from the audit log: holds, overrides, re-ranking and the final 3."""

import pytest

from app.audit import AuditLog
from app.intake import NEED, run_intake
from app.review import (
    apply_hold_decisions, comparison, current_ranking, final_for, final_snapshot, held_groups, hold_decisions, identities,
    latest_run, load_run, overrides_for, pending_rescore, plain_hold_reasons, ranking_effect, read_log, validate_final,
)
from app.pricing import load_pricing
from app.scoring import load_config


@pytest.fixture
def run(scored_run):
    return load_run(latest_run(scored_run["runs"]))


@pytest.fixture
def scoring():
    return load_config(model="gemini-3.8-flash")


def _by_id(rows):
    return {r["applicant_id"]: r for r in rows}


def _override(applicant_id, criterion, model_score, score, run_id="run", reviewer="Rev"):
    return {"event": "score_override", "batch_run": run_id, "applicant_id": applicant_id, "criterion": criterion,
            "model_score": model_score, "score": score, "reason": "because", "reviewer": reviewer}


# Reading the run and the log


def test_latest_run_is_the_newest_folder_with_a_ranking(scored_run, run):
    assert run["id"] == next(scored_run["runs"].iterdir()).name
    assert len(run["ranking"]) == 20 and [h["applicant_id"] for h in run["held"]] == ["R-021", "R-022", "R-023"]


def test_latest_hold_decision_wins():
    entries = [
        {"event": "hold_excluded", "applicant_id": "X", "reason": "a"},
        {"event": "hold_released", "applicant_id": "X", "reason": "b"},
        {"event": "hold_excluded", "applicant_id": "Y", "reason": "c"},
    ]
    assert {k: v["event"] for k, v in hold_decisions(entries).items()} == {"X": "hold_released", "Y": "hold_excluded"}


def test_overrides_and_final_belong_to_their_run():
    entries = [_override("X", NEED, 5, 9, run_id="old"), _override("X", NEED, 5, 7, run_id="run"),
               _override("X", NEED, 5, 8, run_id="run"), {"event": "final_3_recorded", "batch_run": "old"}]
    assert overrides_for(entries, "run")[("X", NEED)]["score"] == 8
    assert final_for(entries, "run") is None and final_for(entries, "old") is not None


# Overrides re-rank


def test_without_overrides_the_ranking_is_the_models(run, scoring):
    current = current_ranking(run, {}, scoring)
    for row, model in zip(current, run["ranking"]):
        assert (row["applicant_id"], row["rank"], row["shortlisted"]) == (model["applicant_id"], model["rank"], model["shortlisted"])
        assert row["adjusted"] is False and row["overridden"] == []


def test_an_override_re_ranks_and_marks_every_rank_it_moved(run, scoring):
    # R-002 scores 5 everywhere (rank 9 of 20); raising need to 10 adds 1.25 to the total.
    current = _by_id(current_ranking(run, {("R-002", NEED): _override("R-002", NEED, 5, 10)}, scoring))
    r2 = current["R-002"]
    assert (r2["model_rank"], r2["rank"], r2["total"], r2["model_total"]) == (9, 5, 6.25, 5.0)
    assert r2["scores"][NEED] == 10 and r2["model_scores"][NEED] == 5
    assert r2["overridden"] == [NEED] and r2["adjusted"] is True
    pushed = current["R-003"]  # one of the 6.0s, pushed from 5 to 6 by someone else's override
    assert (pushed["model_rank"], pushed["rank"], pushed["adjusted"], pushed["overridden"]) == (5, 6, True, [])
    assert current["R-004"]["adjusted"] is False  # the top tie doesn't move


# The final 3


def test_valid_final_3(run, scoring):
    current = current_ranking(run, {}, scoring)
    assert validate_final(["R-003", "R-008", "R-013"], "Strongest need and persistence.", False, current) == []


@pytest.mark.parametrize("picks, reason, ack, expected", [
    (["R-003", "R-008"], "r", False, "Pick exactly 3 applicants (you picked 2)."),
    (["R-003", "R-008", "R-013", "R-018"], "r", False, "Pick exactly 3 applicants (you picked 4)."),
    (["R-003", "R-003", "R-008"], "r", False, "Pick exactly 3 applicants (you picked 2)."),
    (["R-003", "R-008", "R-005"], "r", False, "Not on the current shortlist: R-005."),
    (["R-003", "R-008", "R-013"], "  ", False, "Give a reason for the final 3."),
    (["R-004", "R-009", "R-003"], "r", False,
     "Your pick includes the tie at #3 (R-004, R-009, R-014, R-019). Confirm you're settling it as the committee."),
])
def test_invalid_final_3(run, scoring, picks, reason, ack, expected):
    assert expected in validate_final(picks, reason, ack, current_ranking(run, {}, scoring))


def test_the_tie_at_three_can_be_settled_with_acknowledgement(run, scoring):
    current = current_ranking(run, {}, scoring)
    assert validate_final(["R-004", "R-009", "R-014"], "Settled as committee.", True, current) == []


def test_snapshot_records_what_the_reviewer_saw(run, scoring):
    overrides = {("R-002", NEED): _override("R-002", NEED, 5, 10)}
    snap = final_snapshot(run, current_ranking(run, overrides, scoring), overrides)
    assert snap["run"] == run["id"] and snap["noise"] == run["noise"] and len(snap["ranking_sha256"]) == 64
    row = next(r for r in snap["shortlist"] if r["applicant_id"] == "R-002")
    assert row["rank"] == 5 and row["model_rank"] == 9 and row["adjusted"] is True
    assert snap["overrides"] == [{"applicant_id": "R-002", "criterion": NEED, "model_score": 5, "score": 10,
                                  "reason": "because", "reviewer": "Rev"}]
    assert all(r["shortlisted"] for r in snap["shortlist"])


# Comparison


def test_comparison_shows_medians_runs_and_the_median_runs_rationale(scored_run, run, scoring):
    views = {a["applicant_id"]: a for a in run_intake(scored_run["csv"])["applicants"]}
    rows = comparison(run, read_log(scored_run["audit"]), ["R-003", "R-004"], current_ranking(run, {}, scoring), views)
    r3 = rows[0]
    assert r3["applicant_id"] == "R-003" and r3["view"]["essay_text"].startswith("word3x0")
    readiness = r3["criteria"]["academic_readiness"]
    assert readiness["median"] == 6 and readiness["spread"] == 1 and sorted(readiness["runs"]) == [6, 6, 7]
    assert readiness["rationale"].endswith("reasoning for academic_readiness.")
    assert readiness["runs"][readiness["score_from_run"] - 1] == 6  # a run that scored the median, not the 7
    assert [e["text"] for e in readiness["evidence"]] == ["word3x0 word3x1", "gpa"]
    assert "name" not in r3["view"] and r3["view_changed"] is False


# Holds and identity


def test_released_applicant_is_ready_for_the_next_batch_and_excluded_stays_held(scored_run):
    intake = run_intake(scored_run["csv"])
    released = apply_hold_decisions(intake, [{"event": "hold_released", "applicant_id": "R-021", "reason": "essay on file"}])
    r21 = next(a for a in released["applicants"] if a["applicant_id"] == "R-021")
    assert r21["status"] == "ready"
    assert {"rule": "released_by_reviewer", "detail": "essay on file", "lowers_confidence": []} in r21["flags"]
    excluded = apply_hold_decisions(intake, [{"event": "hold_excluded", "applicant_id": "R-021", "reason": "dup"}])
    assert next(a for a in excluded["applicants"] if a["applicant_id"] == "R-021")["status"] == "held"


def test_identities_only_for_the_ids_asked(scored_run):
    found = identities(scored_run["csv"], ["R-001", "R-002"])
    assert set(found) == {"R-001", "R-002"}
    assert found["R-001"] == {"name": "Zed1 Quill1", "city": "Town1x", "state": "MO", "high_school": "Academy1x"}


def test_the_log_is_the_store(scored_run, run):
    log = AuditLog(scored_run["audit"])
    log.append("hold_released", {"applicant_id": "R-021", "reason": "r", "reviewer": "Rev"})
    assert hold_decisions(read_log(scored_run["audit"]))["R-021"]["event"] == "hold_released"


# Held applicants, plainly


def test_held_groups_put_same_person_copies_side_by_side(scored_run, run):
    applicants = {a["applicant_id"]: a for a in run_intake(scored_run["csv"])["applicants"]}
    groups = held_groups(run, applicants)
    assert [[r["applicant_id"] for r in g["rows"]] for g in groups] == [["R-021"], ["R-022", "R-023"]]
    pair = groups[1]
    assert pair["pair"] is True
    assert pair["differs"] == ["community_service_hours", "essay_text", "gpa", "recommendation_summary", "work_hours_per_week"]
    assert pair["rows"][0]["plain_reasons"] == ["Looks like the same person applied twice: same name and location as R-023."]
    assert groups[0]["rows"][0]["plain_reasons"] == ["No essay was submitted."]
    assert groups[0]["rows"][0]["applicant"]["view"]["essay_text"] is None


@pytest.mark.parametrize("stage, reasons, holds, expected", [
    ("intake", [], [{"rule": "missing_gpa_and_rank", "detail": "x"}],
     ["Neither a GPA nor a class rank was given, so grades can't show academic readiness."]),
    ("intake", [], [{"rule": "short_essay", "detail": "x"}], ["The essay is under half the usual length and may have been cut off."]),
    ("intake", [], [{"rule": "sanity_rank_within_class", "detail": "class rank is between 1 and class size"}],
     ["Fails a basic check: class rank is between 1 and class size."]),
    ("scoring", ["run 2: no valid reply after 3 attempts; last: blocked: SAFETY"], [],
     ["Scoring run 2 never produced a usable reply (last problem: blocked: SAFETY)."]),
    ("scoring", ["run 1: internal error: RuntimeError"], [], ["Scoring run 1 hit an internal error (RuntimeError)."]),
    ("scoring", ["expected 3 runs, found 4"], [], ["Scoring is incomplete: expected 3 runs, found 4."]),
])
def test_hold_reasons_in_plain_language(stage, reasons, holds, expected):
    assert plain_hold_reasons({"stage": stage, "reasons": reasons}, {"holds": holds}) == expected


# What an override would do


def test_ranking_effect_describes_moves_and_the_tie(run, scoring):
    before = current_ranking(run, {}, scoring)
    after = current_ranking(run, {("R-004", "academic_readiness"): _override("R-004", "academic_readiness", 7, 0)}, scoring)
    effect = ranking_effect(before, after, close_call_position=3, subject="R-004")
    assert effect[:2] == ["R-004 moves from #1 to #12", "R-003 moves from #5 to #4"]
    assert "the tie at #3 goes away" in effect


def test_an_override_that_moves_nothing_has_no_effect(run, scoring):
    before = current_ranking(run, {}, scoring)
    same = current_ranking(run, {("R-004", NEED): _override("R-004", NEED, 7, 7)}, scoring)
    assert ranking_effect(before, same, close_call_position=3) == []


# Evidence and pending rescores


def test_evidence_is_labelled_as_quote_or_field(scored_run, run, scoring):
    applicants = {a["applicant_id"]: a for a in run_intake(scored_run["csv"])["applicants"]}
    rows = comparison(run, read_log(scored_run["audit"]), ["R-003", "R-004"], current_ranking(run, {}, scoring), applicants)
    assert rows[0]["criteria"]["academic_readiness"]["evidence"] == [
        {"kind": "quote", "text": "word3x0 word3x1"},
        {"kind": "field", "text": "gpa", "value": 3.5},
    ]


def test_pending_rescore_counts_released_applicants_and_prices_them(scored_run, run, scoring):
    applicants = {a["applicant_id"]: a for a in run_intake(scored_run["csv"])["applicants"]}
    entries = [{"event": "hold_released", "applicant_id": "R-022", "reason": "r"},
               {"event": "hold_excluded", "applicant_id": "R-023", "reason": "r"}]
    pending = pending_rescore(entries, run, applicants, scoring, load_pricing())
    assert pending["ids"] == ["R-022"] and pending["calls"] == 3
    assert pending["command"] == f"python -m app.batch --yes --resume {run['id']}"
    assert 0 < pending["usd"] < 0.1
    assert pending_rescore([], run, applicants, scoring, load_pricing()) is None
