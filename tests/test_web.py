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


def test_comparing_is_logged_and_needs_two_to_four(client, log_entries):
    assert client.get("/compare?ids=R-003").status_code == 400
    assert client.get("/compare?ids=R-001&ids=R-002&ids=R-003&ids=R-004&ids=R-006").status_code == 400
    assert client.get("/compare?ids=R-003&ids=R-021").status_code == 400  # held, not ranked
    assert client.get("/compare?ids=R-003&ids=R-008").status_code == 200
    e = log_entries()[-1]
    assert e["event"] == "comparison_viewed" and e["applicant_ids"] == ["R-003", "R-008"]


def test_an_override_keeps_the_model_score_and_marks_the_adjusted_rank(client, log_entries):
    r = client.post("/override", data={"applicant_id": "R-002", "criterion": "financial_need", "score": "10",
                                       "reason": "Need statement understates it.", "ids": ["R-002", "R-003"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/compare")
    e = log_entries()[-1]
    assert (e["event"], e["model_score"], e["score"], e["previous_score"]) == ("score_override", 5, 10, 5)
    page = client.get("/shortlist").text
    row = page[page.index(">R-002<"):][:600]
    assert "adjusted by reviewer" in row and "model rank 9" in row


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
                                   "reason": "r", "ids": ["R-002"]})
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
