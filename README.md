# Hargrove Scholarship Review

Scores 60 scholarship applications against the Hargrove rubric with Gemini, suggests a shortlist, and lets a person compare applicants and record the final 3. Every model call and every reviewer action is written to a tamper-evident audit log.

It's built as a pipeline component, not a monolith: something upstream triggers a scoring run, the run writes a ranking and an audit trail, and people review the results downstream in the web UI. **The UI never calls the model.** Scoring is a deliberate, costed batch step.

## Run it

You need Docker with **Compose 2.24+** (Docker Desktop or Rancher Desktop). The first build takes a minute or two.

```
docker compose up
```

Open http://localhost:8000. With no key, the UI shows a **recorded demo run** (real Gemini results from 2026-10-13, prompt `score_v2`), labelled on every page. All reviewer actions work on it.

To score for real:

```
cp .env.example .env                                      # set GEMINI_API_KEY
docker compose run --rm app python -m app.batch           # dry run: calls and cost, no calls made
docker compose run --rm app python -m app.batch --yes     # 159 calls, about $1.15, about 8 minutes
```

The UI shows the new run on the next page load. The model is pinned to `gemini-3.8-flash`.

There's no login. The reviewer types a name, which is written into every log entry. In production this would be SSO.

**Reset the demo:** stop the app and delete `data/audit/demo-audit.jsonl`.

## Commands

Prefix each with `docker compose run --rm app`.

| What | Command | Calls Gemini |
|---|---|---|
| Tests | `pytest` | no, the model is faked |
| Profile the data | `python -m app.profiling data/raw/scholarship_candidates.csv` | no |
| Intake (holds, flags, anonymized views) | `python -m app.intake data/raw/scholarship_candidates.csv` | no |
| Score one applicant | `python -m app.scoring SCH-001` | yes, under 1¢ |
| Batch: score, rank, shortlist | `python -m app.batch [--yes] [--resume RUN]` | with `--yes` |
| Eval suite | `python -m evals.run [--yes]` | with `--yes`, about $0.55 |
| Golden-set comparison | `python -m evals.run --golden tuning\|holdout [--yes]` | with `--yes` |

Batch and evals are dry runs unless you add `--yes`. `--resume` continues a crashed run, or scores applicants a reviewer marked "score anyway", reusing every call already made.

**Golden set:** 6 applicants scored by me (3 tuning, 3 holdout), a stand-in for committee calibration. Against `score_v2`, 13 of 15 tuning scores and 14 of 15 holdout scores were within 1 point, and both sets ranked in the same order as mine (results in `evals/results/score_v2/`).

## How it works

```
CSV → intake → score (Gemini, 3 runs) → rank → review UI → final 3
        │            │                     │          │
   hold / flag   validate, retry      noise-band   every action
   redact        never patch          shortlist    to the audit log
```

- **Intake** (`app/intake.py`, `app/redaction.py`):
  - holds applications that can't be judged (no essay, no need statement, no GPA and no rank, a truncated essay, the same person twice);
  - flags the rest (missing fields, near-identical applications, instruction-like text);
  - builds the anonymized view the model sees.
- **Scoring** (`app/scoring.py`, `app/llm.py`):
  - one applicant per call;
  - output validated: five criteria, whole-number scores, and evidence that's a real field or a real quote;
  - a bad reply is retried, never patched; a failure holds the applicant.
- **Ranking** (`app/batch.py`, `app/ranking.py`):
  - medians of 3 runs, with the weighted total computed in code;
  - ties are never broken;
  - the shortlist is sized by measured noise.
- **Review** (`app/web.py`, `app/review.py`): held decisions, compare, override with a reason, record the final 3. Reviewers work blind until the final 3 is recorded.
- **Audit** (`app/audit.py`): append-only JSONL, each entry hashing the one before. Hashes, versions and model output only, no raw applicant data.

Config: `config/scoring.json` (weights, runs, shortlist rules), `config/pricing.json`, `evals/config.json`.

## Where things live

| Path | What | In git |
|---|---|---|
| `data/raw/` | Input CSV, never edited | yes |
| `reports/` | Data profile (counts and IDs only) | yes |
| `demo/` | Recorded demo run and its audit log | yes |
| `evals/results/` | Eval runs per prompt version | yes |
| `evals/cache/`, `evals/golden/` | Cached essay rewrites, hand scores | yes |
| `data/scoring/`, `data/audit/`, `data/derived/` | Real runs, audit log, intake output | no |

## How I drove the agent

- **`CLAUDE.md`:** the rules the coding agent worked under. It started with four and grew as I corrected the agent.
- **`docs/RISKS.md`:** the risk register. Written from the rubric before any code; updated as profiling and evals found things.
- **`prompts/`:** scoring prompt versions (`score_v1` → `score_v2`), never edited in place.
- **`evals/results/`:** the baseline, the recheck that confirmed a failure, and the fix.
- **Git history:** one commit per step.

## Assumptions

- Duplicate applications are held for a person; they're never merged.
- Missing data is held only when a criterion can't be judged at all. A missing test score or recommendation is flagged and scored with lower confidence.
- Test scores are kept, weighed in context, even though the rubric doesn't name them.
- Athletics stays in the text; the prompt is told to ignore it, and an eval checks it does.
- "The human" is one reviewer recording the committee's decision.

---

## Questions

### 1. How do you prevent bias from creating unfair advantages?

I assume the model is biased, remove what it shouldn't see, and test what's left.

- **Remove:**
  - name, location, school and submission time never reach the model;
  - the applicant's own name is stripped from free text;
  - pronouns are neutralized in the recommendation;
  - rank is sent as a percentile, since class size hints at the school.
- **Constrain:**
  - the prompt excludes what the rubric excludes (athletics, prestige, polish);
  - the model scores one applicant at a time and never compares;
  - code does all the math.
- **Test:** counterfactual evals that work on any applicant. Change one thing (name, writing quality, a sports mention, a fancy school, an injected instruction) and check the scores don't move beyond measured noise. Change need, and check that they do.
- **What testing caught:**
  - mentioning a prestigious school dropped one applicant's need score from 4–5 to 2–3, over 4 runs with no overlap;
  - the rationale never mentioned the school, so a reviewer reading it couldn't have seen this;
  - a one-sentence prompt change fixed it, and all evals pass.
- **Lesson:** rationales don't reveal bias; controlled tests do.
- **Still open:**
  - the model sometimes infers gender from activities, so gendered pronouns in rationales are flagged;
  - proxies like intended major remain, and are listed in RISKS.md.

### 2. What should X be to ensure fair results?

X shouldn't be a fixed number. It should come from how well the model can actually tell applicants apart.

- **Rule:** the top 10, plus everyone within the model's run-to-run noise of #10.
  - The 10 is 3 awards × about 3, so the committee has real choice.
  - Noise is the 90th percentile of the spread across 3 runs per applicant, never below 0.25.
- **This run:** noise is 0.45, so **X = 15**.
- **Why:** a cut inside the noise band is decided by randomness, not merit. X gets wider when the model is less certain, which is the right direction.
- **Same logic at the top:** six applicants are within noise of #3, and two are tied at #3. The system marks this for the committee instead of picking.

### 3. Are there any other points at which a human should be in the loop?

Wherever the model shouldn't be trusted, a person decides. Every human action needs a reason and is logged.

- **Bad data, before scoring.** Held applications go to a person who sees the application and chooses "score anyway" or "exclude". Nothing is merged or filled in automatically.
- **Flags during review:** instruction-like text, a missing recommendation, near-identical applications under different names.
- **The cutoff.** Close calls and ties at #3 go to the committee, never to code.
- **Disagreeing with the model.** A reviewer can override any criterion with a reason. The model's score is kept, and rank-changing overrides are previewed first.
- **The final 3.** A person picks, and a snapshot of what they saw is recorded.
- **Blind review** until the final 3 is recorded.
- **Against rubber-stamping:** the shortlist is wider than 3, uncertainty is shown, and reasons are required.
- **I'd add:** committee calibration on a sample before each cycle, and a second reviewer for any override that changes the top 3.

### 4. How would you ensure consistent, fair, quality results over time with a non-deterministic LLM?

Treat it like a noisy instrument: pin it, measure the noise, and gate every change.

- **Pin:** model ID, prompt version and config version, logged on every call. Prompts are never edited in place.
- **Reduce noise:**
  - 3 runs per applicant, with the median;
  - one applicant per call;
  - strict output validation, retry on failure, never patch.
- **Use the noise:** the measured spread sizes the shortlist instead of being hidden.
- **Gate changes:**
  - every prompt or model change reruns the same seeded eval suite against a fixed baseline;
  - one change at a time;
  - confirm a failure is real before fixing it.
- **Over time:**
  - rerun evals on every model update;
  - track pass rate, noise and override rate per version;
  - calibrate against committee scores;
  - turn reviewer overrides into new test cases.
- **It's cheap:** about $1.10 per full batch and $0.55 per eval run, so testing every change is realistic.

### 5. What plumbing would you add to meet typical financial industry governance standards?

**In place:**
- **Hash-chained, append-only audit log,** so edits and deletions are detectable.
- **Every model call** logs the input hash, prompt and config version, model, raw output and token usage.
- **Every human action** logs who, what and why. The final decision is pinned to the ranking it was based on.
- **Mistakes are superseded, not overwritten.**
- **No raw personal data in logs.** Only the redacted view goes to the vendor.

**I'd add:**
- **Identity and access:** SSO, roles, and separation of duties (whoever changes a prompt can't approve it).
- **Change control:** evals as a CI gate, and approval for any prompt, model or config change.
- **Model risk management** (SR 11-7 style): inventory, documentation, independent validation, periodic review.
- **Storage:** write-once audit storage, a retention and deletion policy, encryption.
- **Vendor and data review:** data terms, residency, a data processing agreement.
- **Monitoring:** outcomes by group, drift and override rates, an incident process, and an appeal path for applicants.

### 6. Where did you stop?

**Left out on purpose:**
- **Auth, database, multi-user:** the audit log is the store. Doing these properly costs time and isn't what this exercise tests.
- **Upload and scoring from the UI:** scoring is a costed batch step and the UI is for human judgment. That's the component boundary.
- **A fact-extraction step before scoring:** I planned it to counter writing-polish bias, but the polish evals passed without it on this sample. It would have added cost with no measured benefit.
- **More prompt tuning:** a small remaining school effect on persistence is within threshold. It's logged in RISKS.md instead of chased.
- **Group fairness statistics:** 60 applicants can't support them.

**Next, in order:**
1. A committee-scored golden set (30–50) for calibration, with a holdout.
2. The eval suite as a CI gate on every prompt, model or config change.
3. SSO, roles and an approval workflow.
4. A bigger eval sample, plus a v3 prompt targeting gender inference and the remaining school effect.
5. Production plumbing: write-once audit storage, event-triggered runs, Gemini's batch API, near-duplicate detection at scale.
6. Outcome monitoring by group once volume allows.