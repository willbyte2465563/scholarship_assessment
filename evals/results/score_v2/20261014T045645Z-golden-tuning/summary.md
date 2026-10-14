# Eval run 20261014T045645Z-golden-tuning

- prompt_version: score_v2
- prompt_sha256: 1b5ebb6327fcc60615f747c1b0045a3c4c173d699e327bd4e4f4a17b5010aa58
- model: gemini-3.8-flash
- scoring_config: 3
- eval_config: 2
- seed: 20261013
- applicants: SCH-002, SCH-035, SCH-047
- usage: {'tokens': {'input': 11573, 'output': 5020, 'thinking': 9411}, 'usd': 0.0628}

Moves are shown as `change (threshold/spread)`. A shouldn't-change move fails (✗) only when it's bigger than both its threshold and the measured run-to-run spread.

## Agreement with hand scores

Applicants: SCH-002, SCH-035, SCH-047. Total gap: 0.25. Same order: yes.

| Criterion | Mean gap | Within 1 |
|---|---|---|
| academic_readiness | 0.0 | 3/3 |
| financial_need | 0.33 | 3/3 |
| persistence | 1.67 | 2/3 |
| community_contribution | 0.67 | 3/3 |
| clarity_of_purpose | 1.33 | 2/3 |
