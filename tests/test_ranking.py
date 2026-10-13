import itertools
import random

import pytest

from app.ranking import build_ranking, noise_band, rank


def _entries(*totals):
    return [{"applicant_id": f"A-{n:03d}", "total": t} for n, t in enumerate(totals, start=1)]


def _by_id(ranked):
    return {e["applicant_id"]: e for e in ranked}


# Ranks


def test_equal_totals_share_a_rank_and_the_next_rank_skips():
    ranked = rank(_entries(8.0, 7.5, 7.5, 7.0))
    assert [(e["rank"], e["tied"]) for e in ranked] == [(1, False), (2, True), (2, True), (4, False)]


def test_rank_does_not_depend_on_input_order():
    entries = _entries(6.1, 7.45, 7.45, 5.0, 7.45, 8.2)
    expected = {e["applicant_id"]: e["rank"] for e in rank(entries)}
    for _ in range(20):
        shuffled = entries[:]
        random.shuffle(shuffled)
        assert {e["applicant_id"]: e["rank"] for e in rank(shuffled)} == expected


def test_ties_are_never_broken_by_anything_else():
    a = {"applicant_id": "A-001", "total": 7.0, "scores": {"academic_readiness": 9}, "submitted_at": "early"}
    b = {"applicant_id": "Z-999", "total": 7.0, "scores": {"academic_readiness": 2}, "submitted_at": "late"}
    assert {e["applicant_id"]: e["rank"] for e in rank([b, a])} == {"A-001": 1, "Z-999": 1}


def test_totals_that_are_equal_in_hundredths_tie():
    # 7.45 reached by different weighted sums is still exactly 7.45
    assert [e["rank"] for e in rank(_entries(745 / 100, 7.45, 7.449999999999))] == [1, 1, 1]


# Noise band


def test_noise_is_the_90th_percentile_of_total_spread():
    spreads = [0.0, 0.2, 0.25, 0.3, 0.4, 0.45, 0.5, 0.5, 0.6, 0.65]
    assert noise_band(spreads, percentile=90, floor=0.25) == 0.6  # nearest rank: 9th of 10


def test_noise_never_falls_below_the_floor():
    assert noise_band([0.0, 0.0, 0.1], percentile=90, floor=0.25) == 0.25
    assert noise_band([], percentile=90, floor=0.25) == 0.25


# Shortlist


def _ranking(totals, noise=0.25, size=10, position=3):
    return _by_id(build_ranking(_entries(*totals), shortlist_size=size, close_call_position=position, noise=noise))


def test_shortlist_is_top_ten_plus_anyone_within_noise_of_tenth():
    totals = [9.0, 8.8, 8.6, 8.4, 8.2, 8.0, 7.8, 7.6, 7.4, 5.0, 4.8, 4.7]
    r = _ranking(totals)
    assert [r[f"A-{n:03d}"]["shortlist_reason"] for n in range(10, 13)] == ["top 10", "within noise of #10", None]
    assert sum(e["shortlisted"] for e in r.values()) == 11


def test_everyone_tied_with_tenth_is_shortlisted():
    totals = [9.0, 8.8, 8.6, 8.4, 8.2, 8.0, 7.8, 7.6, 7.4, 5.0, 5.0, 3.0]
    r = _ranking(totals, noise=0.25)
    assert r["A-010"]["shortlist_reason"] == r["A-011"]["shortlist_reason"] == "top 10"
    assert r["A-012"]["shortlisted"] is False


def test_fewer_than_ten_means_everyone_is_shortlisted():
    assert all(e["shortlisted"] for e in _ranking([7.0, 6.0, 5.0]).values())


# Close calls and the committee mark


def test_close_calls_are_within_noise_of_third_on_either_side():
    r = _ranking([9.0, 7.2, 7.0, 6.8, 6.7], noise=0.25)
    assert {i for i, e in r.items() if e["close_call"]} == {"A-002", "A-003", "A-004"}


def test_no_close_call_when_nobody_is_near_third():
    r = _ranking([9.0, 8.0, 7.0, 6.0], noise=0.25)
    assert not any(e["close_call"] for e in r.values())


def test_a_tie_straddling_third_goes_to_the_committee():
    r = _ranking([9.0, 8.0, 7.0, 7.0, 6.0])
    assert {i for i, e in r.items() if e["committee_tie"]} == {"A-003", "A-004"}
    assert r["A-003"]["rank"] == r["A-004"]["rank"] == 3


def test_a_tie_inside_the_top_three_is_not_a_committee_tie():
    r = _ranking([9.0, 8.0, 8.0, 7.0])
    assert not any(e["committee_tie"] for e in r.values())


@pytest.mark.parametrize("totals", list(itertools.permutations([9.0, 8.0, 7.0, 7.0, 6.0])))
def test_marks_do_not_depend_on_input_order(totals):
    entries = [{"applicant_id": f"A-{int(t * 10)}-{n}", "total": t} for n, t in enumerate(totals)]
    out = build_ranking(entries, shortlist_size=10, close_call_position=3, noise=0.25)
    assert sorted(e["total"] for e in out if e["committee_tie"]) == [7.0, 7.0]
