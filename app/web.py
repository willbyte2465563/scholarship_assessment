"""Reviewer UI: server-rendered pages over the latest scoring run.

The newest run in data/scoring and the audit log are read on every request. With no real run
yet, the recorded demo run in demo/ is shown instead, clearly labelled. Every reviewer
action is appended to the audit log, and nothing else is stored. Reviewers see only the
applicant ID and the anonymized application until a final 3 is recorded, then identity for
those three only. There's no login: the reviewer types a name, kept in a cookie and written into
every log entry. In production this would be SSO.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.audit import AuditLog, verify
from app.intake import CRITERIA, run_intake
from app.pricing import load_pricing
from app.review import (
    comparison, current_ranking, final_for, final_snapshot, held_groups, hold_decisions, identities, latest_run,
    load_run, overrides_for, pending_rescore, ranking_effect, read_log, submission_labels, validate_final,
)
from app.scoring import load_config
from app.redaction import fingerprint
from app.source import sha256

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
DECISIONS = {"release": "hold_released", "exclude": "hold_excluded"}
NO_NAME = "Enter your name at the top of the page first; it goes into the audit log with everything you do."


def create_app(audit_path: Path, runs_dir: Path, csv_path: Path,
               demo_dir: Path | None = None, demo_audit_copy: Path | None = None) -> FastAPI:
    app = FastAPI(title="Hargrove scholarship review")
    scoring = load_config(model=os.environ.get("GEMINI_MODEL") or "not set")  # the UI never calls the model
    price_cfg = load_pricing()
    logs: dict[Path, AuditLog] = {}  # one per file, so its lock covers every request
    intake_cache: dict[str, dict] = {}

    def source() -> tuple[Path | None, Path, str | None]:
        """(run folder, audit log, demo label). A real run always wins. With none, the recorded demo
        run is shown, and reviewer actions on it go to a gitignored copy of its log, never into the
        committed demo files."""
        real = latest_run(runs_dir)
        demo_run = latest_run(demo_dir / "scoring") if demo_dir and not real else None
        if not demo_run:
            return real, audit_path, None
        if not demo_audit_copy.exists():
            demo_audit_copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(demo_dir / "audit.jsonl", demo_audit_copy)
        recorded = f"{demo_run.name[:4]}-{demo_run.name[4:6]}-{demo_run.name[6:8]}"
        prompt = load_run(demo_run)["meta"]["prompt_version"]
        return demo_run, demo_audit_copy, f"demo run (recorded {recorded}, {prompt})"

    def applicants() -> dict:
        """Intake's output by ID (the anonymized views), recomputed only when the CSV changes."""
        key = sha256(csv_path)
        if key not in intake_cache:
            intake_cache.clear()
            intake_cache[key] = {a["applicant_id"]: a for a in run_intake(csv_path)["applicants"]}
        return intake_cache[key]

    def state(request: Request) -> dict:
        run_dir, log_path, demo = source()
        entries = read_log(log_path)
        run = load_run(run_dir) if run_dir else None
        key = os.environ.get("GEMINI_API_KEY", "")
        overrides = overrides_for(entries, run["id"]) if run else {}
        final = final_for(entries, run["id"]) if run else None
        return {
            "request": request, "reviewer": request.cookies.get("reviewer"), "msg": request.query_params.get("msg"),
            "entries": entries, "run": run, "overrides": overrides, "criteria": CRITERIA, "rules": scoring.ranking,
            "current": current_ranking(run, overrides, scoring) if run else [],
            "final": final,
            # The ranking changed after the final 3 was recorded (e.g. a rescore). The decision stands; say so.
            "final_stale": bool(final) and final["snapshot"]["ranking_sha256"] != fingerprint(run["ranking"]),
            "log": logs.setdefault(log_path, AuditLog(log_path)), "log_path": log_path,
            "log_exists": log_path.exists(),
            "chain_problem": verify(log_path) if log_path.exists() else None,  # None means intact
            "demo": demo,
            "has_key": bool(key) and key != "your-key-here",  # scoring needs it; the UI doesn't
        }

    def render(name: str, s: dict, status: int = 200, **extra):
        return TEMPLATES.TemplateResponse(s["request"], name, {**s, **extra}, status_code=status)

    def refuse(s: dict, message: str, back: str):
        return render("error.html", s, 400, message=message, back=back)

    def go(path: str, msg: str | None = None):
        return RedirectResponse(path + (("&" if "?" in path else "?") + urlencode({"msg": msg}) if msg else ""), 303)

    @app.get("/")
    def index(request: Request):
        s = state(request)
        pending = pending_rescore(s["entries"], s["run"], applicants(), scoring, price_cfg) if s["run"] else None
        if pending and s["demo"]:
            pending["command"] = None  # the demo run can't be resumed; a real run starts fresh
        return render("index.html", s, pending=pending)

    @app.post("/reviewer")
    def set_reviewer(request: Request, name: str = Form(""), next_page: str = Form("", alias="next")):
        # Back to where they were: an explicit local page (e.g. the comparison they were opening), else the referer.
        local = next_page if next_page.startswith("/") and not next_page.startswith("//") and "\\" not in next_page else ""
        response = go(local or urlparse(request.headers.get("referer") or "").path or "/")
        if name.strip():
            response.set_cookie("reviewer", name.strip(), httponly=True, samesite="lax")
        return response

    @app.get("/held")
    def held(request: Request):
        s = state(request)
        decisions = hold_decisions(s["entries"])
        groups = held_groups(s["run"], applicants()) if s["run"] else []
        for g in groups:
            labels = submission_labels(csv_path, [r["applicant_id"] for r in g["rows"]]) if g["pair"] else {}
            for r in g["rows"]:
                r["decision"] = decisions.get(r["applicant_id"])
                r["submission_order"] = labels.get(r["applicant_id"])
        return render("held.html", s, groups=groups)

    @app.post("/held/{applicant_id}")
    def decide_hold(request: Request, applicant_id: str, decision: str = Form(""), reason: str = Form("")):
        s = state(request)
        held_rows = {h["applicant_id"]: h for h in (s["run"] or {}).get("held", [])}
        if not s["reviewer"]:
            return refuse(s, NO_NAME, "/held")
        if applicant_id not in held_rows:
            return refuse(s, f"{applicant_id} isn't held in this run.", "/held")
        if decision not in DECISIONS:
            return refuse(s, "Choose score anyway or exclude.", "/held")
        if not reason.strip():
            return refuse(s, "Give a reason for the decision.", "/held")
        h = held_rows[applicant_id]
        s["log"].append(DECISIONS[decision], {
            "batch_run": s["run"]["id"], "applicant_id": applicant_id, "stage": h["stage"],
            "hold_reasons": h["reasons"], "reason": reason.strip(), "reviewer": s["reviewer"],
        })
        label = "marked to score anyway in the next batch run" if decision == "release" else "excluded"
        return go("/held", f"{applicant_id} {label}.")

    @app.get("/shortlist")
    def shortlist(request: Request):
        return render("shortlist.html", state(request))

    @app.get("/compare")
    def compare(request: Request, ids: list[str] = Query([]), source: str | None = Query(None, alias="from")):
        s = state(request)
        picked = list(dict.fromkeys(ids))
        ranked = {r["applicant_id"] for r in s["current"]}
        if not s["reviewer"]:  # comparisons are logged with who looked, so ask here and then carry on
            here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return render("compare_name.html", s, next_page=here, ids=picked)
        if not 2 <= len(picked) <= 4:
            return refuse(s, f"Compare 2 to 4 applicants (you picked {len(picked)}).", "/shortlist")
        if outside := [i for i in picked if i not in ranked]:
            return refuse(s, f"Not ranked in this run: {', '.join(outside)}.", "/shortlist")
        if source == "shortlist":  # opened from the shortlist; coming back from an override isn't a new look
            s["log"].append("comparison_viewed", {"batch_run": s["run"]["id"], "applicant_ids": picked, "reviewer": s["reviewer"]})
        rows = comparison(s["run"], s["entries"], picked, s["current"], applicants())
        return render("compare.html", s, rows=rows, ids=picked)

    @app.post("/override")
    def override(request: Request, applicant_id: str = Form(""), criterion: str = Form(""), score: str = Form(""),
                 reason: str = Form(""), ids: list[str] = Form([]), confirm: str | None = Form(None)):
        s = state(request)
        back = "/compare?" + urlencode({"ids": ids or [applicant_id]}, doseq=True)
        row = next((r for r in s["current"] if r["applicant_id"] == applicant_id), None)
        if not s["reviewer"]:
            return refuse(s, NO_NAME, back)
        if s["final"]:
            return refuse(s, "The final 3 is recorded, so overrides are closed for this run. "
                             "Record a new final 3 with a reason to change anything.", back)
        if row is None or criterion not in CRITERIA:
            return refuse(s, "Unknown applicant or criterion.", back)
        if not re.fullmatch(r"\d{1,2}", score.strip()) or int(score) > 10:
            return refuse(s, "The new score must be a whole number from 0 to 10.", back)
        if not reason.strip():
            return refuse(s, "Give a reason for the override.", back)
        # An override that moves anyone's rank is shown first and needs a confirm; one that moves nothing saves.
        proposed = current_ranking(s["run"], {**s["overrides"], (applicant_id, criterion): {"score": int(score)}}, scoring)
        effect = ranking_effect(s["current"], proposed, scoring.ranking["close_call_position"], subject=applicant_id)
        if effect and confirm != "yes":
            return render("override_preview.html", s, effect=effect, back=back, ids=ids, applicant_id=applicant_id,
                          criterion=criterion, score=int(score), reason=reason.strip(),
                          current_score=row["scores"][criterion], model_score=row["model_scores"][criterion])
        s["log"].append("score_override", {
            "batch_run": s["run"]["id"], "applicant_id": applicant_id, "criterion": criterion,
            "model_score": row["model_scores"][criterion], "previous_score": row["scores"][criterion],
            "score": int(score), "reason": reason.strip(), "reviewer": s["reviewer"], "effect": effect,
        })
        return go(back, f"{applicant_id} {criterion.replace('_', ' ')} set to {int(score)}.")

    @app.get("/final")
    def final(request: Request):
        s = state(request)
        revealed = identities(csv_path, s["final"]["applicant_ids"]) if s["final"] else {}
        return render("final.html", s, revealed=revealed, errors=[], picked=[])

    @app.post("/final")
    def record_final(request: Request, ids: list[str] = Form([]), reason: str = Form(""),
                     acknowledge_tie: str | None = Form(None)):
        s = state(request)
        if not s["reviewer"]:
            return refuse(s, NO_NAME, "/final")
        picked = list(dict.fromkeys(ids))
        errors = validate_final(ids, reason, bool(acknowledge_tie), s["current"])
        if errors:
            revealed = identities(csv_path, s["final"]["applicant_ids"]) if s["final"] else {}
            return render("final.html", s, 400, revealed=revealed, errors=errors, picked=picked, reason=reason)
        s["log"].append("final_3_recorded", {
            "batch_run": s["run"]["id"], "applicant_ids": picked, "reason": reason.strip(),
            "acknowledged_tie": bool(acknowledge_tie), "reviewer": s["reviewer"],
            "replaces_seq": s["final"]["seq"] if s["final"] else None,
            "snapshot": final_snapshot(s["run"], s["current"], s["overrides"]),
        })
        return go("/final", "Final 3 recorded.")

    @app.get("/audit")
    def audit(request: Request, event: str | None = None):
        s = state(request)
        shown = [e for e in reversed(s["entries"]) if not event or e["event"] == event][:200]
        events = sorted({e["event"] for e in s["entries"]})
        return render("audit.html", s, shown=shown, events=events, event=event)

    @app.get("/audit/{seq}")
    def audit_entry(request: Request, seq: int):
        s = state(request)
        entry = next((e for e in s["entries"] if e["seq"] == seq), None)
        if entry is None:
            return refuse(s, f"No audit entry {seq}.", "/audit")
        return render("audit_entry.html", s, entry=entry)

    return app
