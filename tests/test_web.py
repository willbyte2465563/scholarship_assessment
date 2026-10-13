"""The reviewer UI, driven through FastAPI's test client against a real (fake-model) run."""

import json

import pytest
from fastapi.testclient import TestClient

from app.audit import verify
from app.web import create_app
from tests.conftest import reviewer_rows

IDENTITIES = [(r["name"], r["city"], r["high_school"]) for r in reviewer_rows()]
PAGES = ["/", "/held", "/shortlist", "/compare?ids=R-003&ids=R-004&ids=R-002", "/final", "/audit"]


@pytest.fixture
def client(scored_run):
    app = create_app(audit_path=scored_run["audit"], runs_dir=scored_run["runs"], csv_path=scored_run["csv"])
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("reviewer", "Rev Iewer")
    return c


@pytest.fixture
def log_entries(scored_run):
    def read():
        return [json.loads(line) for line in scored_run["audit"].read_text(encoding="utf-8").splitlines()]
    return read


def _final(client, ids, reason="Strongest fit with the donor's intent.", ack=False):
    data = {"ids": ids, "reason": reason}
    if ack:
        data["acknowledge_tie"] = "yes"
    return client.post("/final", data=data)


# Pages and blindness


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client, path):
    assert client.get(path).status_code == 200


def test_reviewers_work_blind_until_the_final_3_is_recorded(client):
    for path in PAGES:
        page = client.get(path).text
        for name, city, school in IDENTITIES:
            assert name not in page and city not in page and school not in page, (path, name)
    assert _final(client, ["R-003", "R-008", "R-013"]).status_code == 303
    final = client.get("/final").text
    shown = {name for name, _, _ in IDENTITIES if name in final}
    assert shown == {"Zed3 Quill3", "Zed8 Quill8", "Zed13 Quill13"}
    assert "Town3x" in final and "Academy3x" in final
    for path in PAGES[:-2]:  # still blind everywhere else
        assert not any(name in client.get(path).text for name, _, _ in IDENTITIES)


def test_applicant_text_is_escaped_not_rendered(client):
    page = client.get("/compare?ids=R-002&ids=R-003").text
    assert "&lt;script&gt;alert(" in page and "<script>alert(" not in page


def test_shortlist_shows_marks(client):
    page = client.get("/shortlist").text
    for i in ("R-004", "R-009", "R-014", "R-019", "R-001"):
        assert i in page
    assert "R-005" not in page.split("Full ranking")[0]  # not shortlisted
    assert "committee" in page.lower() and "close call" in page.lower()


# Held applicants


def test_release_a_held_applicant(client, log_entries):
    r = client.post("/held/R-021", data={"decision": "release", "reason": "Essay arrived by mail."})
    assert r.status_code == 303
    e = log_entries()[-1]
    assert (e["event"], e["applicant_id"], e["reason"], e["reviewer"]) == ("hold_released", "R-021", "Essay arrived by mail.", "Rev Iewer")
    assert "score anyway" in client.get("/held").text.lower()


@pytest.mark.parametrize("cookie, data, message", [
    (None, {"decision": "release", "reason": "r"}, "Enter your name"),
    ("Rev", {"decision": "release", "reason": "  "}, "Give a reason"),
    ("Rev", {"decision": "promote", "reason": "r"}, "Choose"),
])
def test_hold_decisions_need_a_name_a_reason_and_a_choice(scored_run, log_entries, cookie, data, message):
    c = TestClient(create_app(audit_path=scored_run["audit"], runs_dir=scored_run["runs"], csv_path=scored_run["csv"]),
                   follow_redirects=False)
    if cookie:
        c.cookies.set("reviewer", cookie)
    before = len(log_entries())
    r = c.post("/held/R-021", data=data)
    assert r.status_code == 400 and message in r.text
    assert len(log_entries()) == before


def test_setting_the_reviewer_name(scored_run):
    c = TestClient(create_app(audit_path=scored_run["audit"], runs_dir=scored_run["runs"], csv_path=scored_run["csv"]),
                   follow_redirects=False)
    r = c.post("/reviewer", data={"name": "Ada Reviewer"})
    assert r.status_code == 303 and r.cookies.get("reviewer")


# Compare and override


def test_comparing_needs_two_to_four_ranked_applicants(client):
    assert client.get("/compare?ids=R-003").status_code == 400
    assert client.get("/compare?ids=R-001&ids=R-002&ids=R-003&ids=R-004&ids=R-006").status_code == 400
    assert client.get("/compare?ids=R-003&ids=R-021").status_code == 400  # held, not ranked


def test_only_a_comparison_opened_from_the_shortlist_is_logged(client, log_entries):
    assert 'name="from" value="shortlist"' in client.get("/shortlist").text
    assert client.get("/compare?ids=R-003&ids=R-008&from=shortlist").status_code == 200
    e = log_entries()[-1]
    assert e["event"] == "comparison_viewed" and e["applicant_ids"] == ["R-003", "R-008"]
    before = len(log_entries())
    assert client.get("/compare?ids=R-003&ids=R-008").status_code == 200  # e.g. back from an override
    assert len(log_entries()) == before


def test_an_override_that_moves_ranks_needs_a_confirm_after_a_preview(client, log_entries):
    data = {"applicant_id": "R-002", "criterion": "financial_need", "score": "10",
            "reason": "Need statement understates it.", "ids": ["R-002", "R-003"]}
    before = len(log_entries())
    preview = client.post("/override", data=data)
    assert preview.status_code == 200 and len(log_entries()) == before
    assert "R-002 moves from #9 to #5" in preview.text and "R-003 moves from #5 to #6" in preview.text
    assert 'name="confirm" value="yes"' in preview.text
    r = client.post("/override", data={**data, "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"].startswith("/compare")
    e = log_entries()[-1]
    assert (e["event"], e["model_score"], e["score"], e["previous_score"]) == ("score_override", 5, 10, 5)
    assert e["effect"][0] == "R-002 moves from #9 to #5"
    page = client.get("/shortlist").text
    row = page[page.index(">R-002<"):][:600]
    assert "adjusted by reviewer" in row and "model rank 9" in row


def test_an_override_that_moves_nothing_saves_straight_away(client, log_entries):
    r = client.post("/override", data={"applicant_id": "R-004", "criterion": "financial_need", "score": "7",
                                       "reason": "Agree with the model; recording that I checked.", "ids": ["R-004"]})
    assert r.status_code == 303
    assert log_entries()[-1]["event"] == "score_override" and log_entries()[-1]["effect"] == []


def test_the_tie_going_away_is_spelled_out(client):
    r = client.post("/override", data={"applicant_id": "R-004", "criterion": "academic_readiness", "score": "0",
                                       "reason": "r", "ids": ["R-004"]})
    assert r.status_code == 200 and "the tie at #3 goes away" in r.text


def test_evidence_is_labelled(client):
    page = client.get("/compare?ids=R-003&ids=R-008").text
    assert "quote" in page and "field" in page and "gpa: 3.5" in page


def test_held_page_shows_the_application_and_puts_the_same_person_side_by_side(client):
    page = client.get("/held").text
    assert "No essay was submitted." in page
    assert "Looks like the same person applied twice" in page
    assert "word22x0" in page and "word23x0" in page  # both copies' essays
    assert page.count('class="diff"') >= 4  # differing fields are highlighted


def test_overview_shows_applicants_waiting_for_a_rescore(client):
    assert "waiting for the next batch run" not in client.get("/").text
    client.post("/held/R-021", data={"decision": "release", "reason": "Essay arrived by mail."})
    page = client.get("/").text
    assert "waiting for the next batch run" in page and "R-021" in page
    assert "python -m app.batch --yes --resume" in page and "$" in page


@pytest.mark.parametrize("score", ["11", "-1", "7.5", "x"])
def test_override_scores_must_be_whole_numbers_0_to_10(client, score):
    r = client.post("/override", data={"applicant_id": "R-002", "criterion": "financial_need", "score": score,
                                       "reason": "r", "ids": ["R-002"]})
    assert r.status_code == 400


# The final 3


def test_invalid_final_is_refused_and_not_logged(client, log_entries):
    before = len(log_entries())
    r = _final(client, ["R-003", "R-008"])
    assert r.status_code == 400 and "Pick exactly 3" in r.text
    r = _final(client, ["R-004", "R-009", "R-003"])
    assert r.status_code == 400 and "tie at #3" in r.text
    assert len(log_entries()) == before


def test_final_with_the_tie_at_three_records_a_snapshot(client, log_entries):
    client.post("/override", data={"applicant_id": "R-002", "criterion": "financial_need", "score": "10",
                                   "reason": "r", "ids": ["R-002"], "confirm": "yes"})
    assert _final(client, ["R-004", "R-009", "R-002"], ack=True).status_code == 303
    e = log_entries()[-1]
    assert e["event"] == "final_3_recorded" and e["applicant_ids"] == ["R-004", "R-009", "R-002"]
    assert e["acknowledged_tie"] is True and e["reviewer"] == "Rev Iewer" and e["replaces_seq"] is None
    snap = e["snapshot"]
    assert len(snap["shortlist"]) == 16 and snap["overrides"][0]["applicant_id"] == "R-002"
    assert next(r for r in snap["shortlist"] if r["applicant_id"] == "R-002")["adjusted"] is True


def test_after_the_final_overrides_are_closed_and_a_change_is_a_new_record(client, log_entries):
    _final(client, ["R-003", "R-008", "R-013"])
    first = log_entries()[-1]
    r = client.post("/override", data={"applicant_id": "R-002", "criterion": "financial_need", "score": "10",
                                       "reason": "r", "ids": ["R-002"]})
    assert r.status_code == 400 and "final 3 is recorded" in r.text
    assert _final(client, ["R-003", "R-008", "R-018"], reason="Swapped R-013 after a second read.").status_code == 303
    second = log_entries()[-1]
    assert second["replaces_seq"] == first["seq"]
    final = client.get("/final").text
    assert "Zed18 Quill18" in final and "Zed13 Quill13" not in final


# Audit log


def test_audit_page_shows_whether_the_chain_verifies(client, scored_run):
    page = client.get("/audit").text
    assert "Chain intact" in page
    assert client.get("/audit/1").status_code == 200
    lines = scored_run["audit"].read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["model"] = "edited"
    lines[1] = json.dumps(tampered)
    scored_run["audit"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(scored_run["audit"]) is not None
    assert "Chain broken" in client.get("/audit").text
