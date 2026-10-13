"""The reviewer UI, served by `docker compose up` on port 8000.

With no real scoring run yet (a fresh clone), the UI shows the recorded demo run in demo/,
labelled as such, and writes any reviewer actions on it to data/audit/demo-audit.jsonl.
Paths can be pointed elsewhere with REVIEW_AUDIT_LOG, REVIEW_RUNS_DIR and REVIEW_CSV, for
example to rehearse the flow against a copy of the audit log.
"""

import os
from pathlib import Path

from app.batch import RUNS_DIR
from app.scoring import DEFAULT_AUDIT, DEFAULT_CSV
from app.web import create_app

app = create_app(
    audit_path=Path(os.environ.get("REVIEW_AUDIT_LOG", DEFAULT_AUDIT)),
    runs_dir=Path(os.environ.get("REVIEW_RUNS_DIR", RUNS_DIR)),
    csv_path=Path(os.environ.get("REVIEW_CSV", DEFAULT_CSV)),
    demo_dir=Path("demo"),
    demo_audit_copy=Path("data/audit/demo-audit.jsonl"),
)
