"""Batch scoring against a fake model: parallel, resumable, and one bad applicant never stops it."""

import dataclasses
import json
import re
import threading
from collections import defaultdict

import pytest

from app.audit import AuditLog, verify
from app.batch import aggregate, load_done, main, rank_batch, score_all
from app.intake import CRITERIA, run_intake
from app.llm import Backoff, LLMReply
from app.scoring import load_config, score_applicant
from tests.conftest import make_row, write_csv
from tests.fakes import FakeLLM, good_reply


class VaryingLLM:
    """Thread-safe. Each applicant's runs score base, base+1, base in call order, so the median is
    base and the spread is 1 whatever order the workers finish in."""

    def __init__(self, raise_for: int | None = None, block_after_first: int | None = None):
        self.raise_for, self.block_after_first = raise_for, block_after_first
        self.lock, self.seen, self.calls = threading.Lock(), defaultdict(int), 0

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        seed = int(re.search(r"word(\d+)x0", prompt).group(1))
        with self.lock:
            n = self.seen[seed]
            self.seen[seed] += 1
            self.calls += 1
        if seed == self.raise_for:
            raise RuntimeError("boom")
        if seed == self.block_after_first and n >= 1:
            return LLMReply(None, "blocked: SAFETY")
        base = seed % 5 + 3
        return LLMReply(json.dumps(good_reply(scores=(base + (n % 3 == 1),) * len(CRITERIA))), None, "fake")


class Boom:
    def generate(self, *a, **k):
        raise AssertionError("the model was called")


@pytest.fixture
def csv_path(tmp_path):
    rows = [make_row(f"B-00{s}", s) for s in (1, 2, 3)] + [make_row("B-004", 4, essay_text="")]
    return write_csv(tmp_path / "applicants.csv", rows)


@pytest.fixture
def intake(csv_path):
    return run_intake(csv_path)


@pytest.fixture
def scoring():
    return load_config(model="gemini-test")


def _run(intake, llm, scoring, tmp_path, workers=4):
    log = AuditLog(tmp_path / "audit.jsonl")
    records = score_all(intake, llm, scoring, log, tmp_path / "run", "run-1", workers)
    return records, aggregate(intake, records, scoring), log


def _by_id(aggregates):
    return {a["applicant_id"]: a for a in aggregates}


def test_every_ready_applicant_gets_three_runs_and_a_median(intake, scoring, tmp_path):
    llm = VaryingLLM()
    records, aggregates, _ = _run(intake, llm, scoring, tmp_path)
    a = _by_id(aggregates)
    assert llm.calls == 9 and len(records) == 9  # B-004 is held at intake and costs nothing
    assert a["B-001"]["scores"] == dict.fromkeys(CRITERIA, 4)
    assert a["B-001"]["spread"] == dict.fromkeys(CRITERIA, 1)
    assert a["B-001"]["total"] == 4.0 and a["B-001"]["total_spread"] == 1.0
    assert a["B-004"]["status"] == "held" and a["B-004"]["stage"] == "intake"


def test_results_do_not_depend_on_how_many_workers(intake, scoring, tmp_path):
    _, one, _ = _run(intake, VaryingLLM(), scoring, tmp_path / "one", workers=1)
    _, four, _ = _run(intake, VaryingLLM(), scoring, tmp_path / "four", workers=4)
    strip = lambda rows: [{k: v for k, v in r.items() if k != "run_totals"} | {"run_totals": sorted(r.get("run_totals", []))} for r in rows]
    assert strip(one) == strip(four)


def test_audit_chain_holds_under_parallel_writes(intake, scoring, tmp_path):
    _, _, log = _run(intake, VaryingLLM(), scoring, tmp_path)
    assert verify(log.path) is None
    entries = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 18 and {e["batch_run"] for e in entries} == {"run-1"}
    assert sorted(e["run"] for e in entries if e["event"] == "score_result") == [1, 1, 1, 2, 2, 2, 3, 3, 3]


def test_resume_skips_calls_already_scored(intake, scoring, tmp_path):
    first, _, _ = _run(intake, VaryingLLM(), scoring, tmp_path)
    again, _, _ = _run(intake, Boom(), scoring, tmp_path)
    assert again == first


def test_a_changed_input_is_not_reused(intake, scoring, tmp_path):
    _run(intake, VaryingLLM(), scoring, tmp_path)
    llm = VaryingLLM()
    _run(intake, llm, dataclasses.replace(scoring, version="other"), tmp_path)
    assert llm.calls == 9


def test_a_line_cut_off_by_a_crash_is_ignored(intake, scoring, tmp_path):
    _run(intake, VaryingLLM(), scoring, tmp_path)
    with (tmp_path / "run" / "calls.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"key": "abc", "appl')
    assert len(load_done(tmp_path / "run" / "calls.jsonl")) == 9


def test_one_applicant_raising_does_not_stop_the_batch(intake, scoring, tmp_path):
    _, aggregates, _ = _run(intake, VaryingLLM(raise_for=2), scoring, tmp_path)
    a = _by_id(aggregates)
    assert a["B-002"]["status"] == "held" and a["B-002"]["stage"] == "scoring"
    assert all("internal error: RuntimeError" in r for r in a["B-002"]["reasons"])
    assert a["B-001"]["status"] == a["B-003"]["status"] == "scored"


def test_any_failed_run_holds_the_whole_applicant(intake, scoring, tmp_path):
    _, aggregates, _ = _run(intake, VaryingLLM(block_after_first=3), scoring, tmp_path)
    a = _by_id(aggregates)["B-003"]
    assert a["status"] == "held" and len(a["reasons"]) == 2
    assert all("blocked: SAFETY" in r for r in a["reasons"])


def test_held_runs_are_tried_again_on_resume(intake, scoring, tmp_path):
    _run(intake, VaryingLLM(block_after_first=3), scoring, tmp_path)
    llm = VaryingLLM()
    _, aggregates, _ = _run(intake, llm, scoring, tmp_path)
    assert llm.calls == 2 and _by_id(aggregates)["B-003"]["status"] == "scored"


def test_ranking_uses_medians_and_reports_the_noise(intake, scoring, tmp_path):
    _, aggregates, _ = _run(intake, VaryingLLM(), scoring, tmp_path)
    result = rank_batch(aggregates, scoring)
    assert [(e["applicant_id"], e["rank"]) for e in result["ranking"]] == [("B-003", 1), ("B-002", 2), ("B-001", 3)]
    assert result["noise"] == 1.0  # every spread is 1.0, above the 0.25 floor
    assert [h["applicant_id"] for h in result["held"]] == ["B-004"]


# Backoff


def test_rate_limit_is_waited_out_without_using_an_attempt(intake, scoring, tmp_path):
    waits = []
    inner = FakeLLM(LLMReply(None, "api_error: 429 RESOURCE_EXHAUSTED"), good_reply())
    llm = Backoff(inner, max_tries=6, base_seconds=2, cap_seconds=60, sleep=waits.append, jitter=lambda: 0.0)
    applicant = next(a for a in intake["applicants"] if a["applicant_id"] == "B-001")
    log = AuditLog(tmp_path / "audit.jsonl")
    result = score_applicant(applicant, llm, scoring, log)
    assert result["status"] == "scored" and result["attempts"] == 1
    assert waits == [1.0]
    assert json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])["backoff_retries"] == 1


def test_backoff_gives_up_after_max_tries_and_never_retries_a_block():
    waits = []
    stuck = Backoff(FakeLLM(*[LLMReply(None, "api_error: 503 UNAVAILABLE")] * 3), max_tries=3, sleep=waits.append,
                    jitter=lambda: 1.0)
    reply = stuck.generate("p", model="m", temperature=0)
    assert reply.failure.startswith("api_error: 503") and reply.backoff_retries == 2 and waits == [2.0, 4.0]
    blocked = Backoff(FakeLLM(LLMReply(None, "blocked: SAFETY")), sleep=waits.append)
    assert blocked.generate("p", model="m", temperature=0).backoff_retries == 0


# Command


def test_dry_run_calls_nothing(csv_path, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    assert main(["--csv", str(csv_path), "--runs-dir", str(tmp_path / "runs")], llm=Boom()) == 0
    out = capsys.readouterr().out
    assert "3 ready, 1 held at intake (B-004)" in out and "9 to make" in out
    assert "Source: https://ai.google.dev/gemini-api/docs/pricing" in out and "Dry run: nothing was called" in out
    assert not (tmp_path / "runs").exists()


def test_full_run_writes_ranking_and_records_the_shortlist(csv_path, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    args = ["--yes", "--csv", str(csv_path), "--runs-dir", str(tmp_path / "runs"), "--audit", str(tmp_path / "audit.jsonl")]
    assert main(args, llm=VaryingLLM()) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    ranking = json.loads((run_dir / "ranking.json").read_text(encoding="utf-8"))
    assert [e["applicant_id"] for e in ranking["ranking"]] == ["B-003", "B-002", "B-001"]
    last = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["event"] == "batch_ranked" and last["shortlist"] == ["B-003", "B-002", "B-001"]
    assert verify(tmp_path / "audit.jsonl") is None
    assert "B-004 (intake)" in (run_dir / "summary.md").read_text(encoding="utf-8")
