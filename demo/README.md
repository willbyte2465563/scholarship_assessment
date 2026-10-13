# Demo run

A real scoring run, recorded on 2026-10-13 with `score_v2` and `gemini-3.8-flash`, kept in the repo so the reviewer UI works on a fresh clone with no API key.

- `scoring/20261013T185017Z/`: the run as `python -m app.batch --yes` wrote it: every call (`calls.jsonl`), the ranking (`ranking.json`, `summary.md`), and the first ranking it replaced after a cache bug was fixed (`*.superseded-1.*`).
- `audit.jsonl`: the hash-chained audit log for that run, as written. It holds IDs, hashes, versions and model output that quotes only the anonymized applications. No reviewer actions and no final 3.

The UI shows this run only when `data/scoring/` has no run of its own, and labels it "demo run (recorded 2026-10-13, score_v2)". Reviewer actions on the demo go to `data/audit/demo-audit.jsonl` (gitignored), never into these files. Real runs stay in `data/scoring/` and `data/audit/`, which are gitignored.
