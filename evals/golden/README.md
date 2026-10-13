# Golden set

Hand scores for comparing the model against a person. Six applicants: three for tuning the prompt, three held out for the final check only.

- `tuning.jsonl`: used while changing the prompt.
- `holdout.jsonl`: run only with `--golden holdout`, once a prompt is settled. Looking at holdout results while tuning defeats the point.

One applicant per line:

```json
{"applicant_id": "SCH-000", "scores": {"academic_readiness": 7, "financial_need": 5, "persistence": 6, "community_contribution": 4, "clarity_of_purpose": 8}, "notes": "optional: why"}
```

Scores are whole numbers from 0 to 10, scored against `docs/rubric.md` without looking at model output. Each applicant must be ready at intake (not held). The model scores each one 3 times and the median is compared.
