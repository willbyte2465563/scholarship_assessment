from pathlib import Path

from fastapi.testclient import TestClient

from app.web import create_app


def test_the_ui_starts_before_any_scoring_run(tmp_path):
    app = create_app(audit_path=tmp_path / "audit.jsonl", runs_dir=tmp_path / "runs",
                     csv_path=Path("data/raw/scholarship_candidates.csv"))
    r = TestClient(app).get("/")
    assert r.status_code == 200 and "No scoring run yet" in r.text


def test_the_served_app_is_the_reviewer_ui():
    from app.main import app

    assert app.title == "Hargrove scholarship review"
