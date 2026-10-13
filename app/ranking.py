"""Ranking, shortlist and close calls. Plain functions over {applicant_id, total, ...} entries.

Totals are compared in hundredths, as integers, so equal totals are exactly equal. Equal totals
share a rank and are never broken: not by ID, not by any score, not by submission time. Tied
entries are listed by ID only so the output is stable; their shared rank is what counts.
"""

from __future__ import annotations

import math


def _c(total: float) -> int:
    return round(total * 100)


def noise_band(total_spreads: list[float], percentile: int, floor: float) -> float:
    """Run-to-run noise on totals: the given percentile (nearest rank) of per-applicant spreads,
    never below the floor, so the band can't shrink to nothing."""
    if not total_spreads:
        return floor
    ordered = sorted(total_spreads)
    value = ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]
    return round(max(floor, value), 2)


def rank(entries: list[dict]) -> list[dict]:
    """Competition ranking (1, 2, 2, 4): your rank is 1 plus the number of totals above yours."""
    ordered = sorted(entries, key=lambda e: (-_c(e["total"]), e["applicant_id"]))
    counts: dict[int, int] = {}
    for e in ordered:
        counts[_c(e["total"])] = counts.get(_c(e["total"]), 0) + 1
    out, above = [], 0
    for n, e in enumerate(ordered):
        if n and _c(e["total"]) != _c(ordered[n - 1]["total"]):
            above = n
        out.append({**e, "rank": above + 1, "tied": counts[_c(e["total"])] > 1})
    return out


def build_ranking(entries: list[dict], *, shortlist_size: int, close_call_position: int, noise: float) -> list[dict]:
    """Rank, then mark the shortlist, the close calls around the cutoff, and any tie the
    committee has to settle."""
    ranked = rank(entries)
    band = _c(noise)

    shortlist_floor = None
    if len(ranked) >= shortlist_size:
        shortlist_floor = _c(ranked[shortlist_size - 1]["total"]) - band

    close, committee = set(), set()
    if len(ranked) >= close_call_position:
        cutoff = _c(ranked[close_call_position - 1]["total"])
        near = [e["applicant_id"] for e in ranked if abs(_c(e["total"]) - cutoff) <= band]
        if len(near) > 1:
            close = set(near)
        above = sum(1 for e in ranked if _c(e["total"]) > cutoff)
        at_or_above = sum(1 for e in ranked if _c(e["total"]) >= cutoff)
        if above < close_call_position < at_or_above:  # more people tied at the cutoff than places left
            committee = {e["applicant_id"] for e in ranked if _c(e["total"]) == cutoff}

    out = []
    for e in ranked:
        if e["rank"] <= shortlist_size:
            reason = f"top {shortlist_size}"
        elif shortlist_floor is not None and _c(e["total"]) >= shortlist_floor:
            reason = f"within noise of #{shortlist_size}"
        else:
            reason = None
        out.append({
            **e,
            "shortlisted": reason is not None,
            "shortlist_reason": reason,
            "close_call": e["applicant_id"] in close,
            "committee_tie": e["applicant_id"] in committee,
        })
    return out
