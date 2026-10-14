# Eval run 20261014T045917Z-golden-holdout

- prompt_version: score_v2
- prompt_sha256: 1b5ebb6327fcc60615f747c1b0045a3c4c173d699e327bd4e4f4a17b5010aa58
- model: gemini-3.8-flash
- scoring_config: 3
- eval_config: 2
- seed: 20261013
- applicants: SCH-028, SCH-045, SCH-057
- usage: {'tokens': {'input': 10335, 'output': 4689, 'thinking': 9310}, 'usd': 0.0602}

Moves are shown as `change (threshold/spread)`. A shouldn't-change move fails (✗) only when it's bigger than both its threshold and the measured run-to-run spread.

## Agreement with hand scores

Applicants: SCH-028, SCH-045, SCH-057. Total gap: 0.25. Same order: yes.

| Criterion | Mean gap | Within 1 |
|---|---|---|
| academic_readiness | 0.33 | 3/3 |
| financial_need | 1.0 | 2/3 |
| persistence | 0.33 | 3/3 |
| community_contribution | 0.0 | 3/3 |
| clarity_of_purpose | 1.0 | 3/3 |
