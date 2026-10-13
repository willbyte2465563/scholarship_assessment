# Data profile

- Source: `data/raw/scholarship_candidates.csv`
- SHA-256: `9ab0fc987839b22ca3e6f7064512483f3c954aeab191a5ed04042102aef39241`
- Rows: 60
- Generated: 2026-10-13T13:58:05+00:00

Counts and applicant IDs only. No raw text or personal information.

## Findings

### Missing

- gpa: 1 blank — SCH-037
- gpa_scale: 1 blank — SCH-037
- class_rank: 1 blank — SCH-037
- test_score: 1 blank — SCH-046
- essay_text: 1 blank — SCH-034
- recommendation_summary: 1 blank — SCH-031

### Text

- essay_text: 1 under half the median length — SCH-030
- essay_text: 1 may be truncated (no closing punctuation, or trailing whitespace) — SCH-030

### Could not check

- rank_within_class: couldn't run for 1 (class_rank blank) — SCH-037
- gpa_within_scale: couldn't run for 1 (gpa blank) — SCH-037
- gpa_within_scale: couldn't run for 1 (gpa_scale blank) — SCH-037
- test_score_plausible: couldn't run for 1 (test_score blank) — SCH-046
- class_size_consistent_per_school: couldn't run for 1 (class_rank blank or unparseable) — SCH-037
- essay_text near-duplicate check couldn't run for 1 (essay_text blank) — SCH-034
- recommendation_summary near-duplicate check couldn't run for 1 (recommendation_summary blank) — SCH-031

### Duplicates

- same name; also matching: city, state, zip — SCH-004, SCH-059
- same name; also matching: city, state, zip — SCH-026, SCH-060
- same values in every numeric/profile field (same name: no, same essay: yes) — SCH-011, SCH-012
- same values in every numeric/profile field (same name: no, same essay: yes) — SCH-013, SCH-014
- same values in every numeric/profile field (same name: no, same essay: yes) — SCH-015, SCH-016
- same values in every numeric/profile field (same name: no, same essay: no) — SCH-022, SCH-023
- same values in every numeric/profile field (same name: no, same essay: no) — SCH-024, SCH-025
- same values in every numeric/profile field (same name: yes, same essay: yes) — SCH-026, SCH-060
- essay_text near-identical (Jaccard 1.0) — SCH-004, SCH-059
- essay_text near-identical (Jaccard 1.0) — SCH-011, SCH-012
- essay_text near-identical (Jaccard 1.0) — SCH-013, SCH-014
- essay_text near-identical (Jaccard 1.0) — SCH-015, SCH-016
- essay_text near-identical (Jaccard 1.0) — SCH-017, SCH-018
- essay_text near-identical (Jaccard 1.0) — SCH-019, SCH-020
- essay_text near-identical (Jaccard 1.0) — SCH-026, SCH-060
- recommendation_summary near-identical (Jaccard 1.0) — SCH-004, SCH-059
- recommendation_summary near-identical (Jaccard 1.0) — SCH-011, SCH-012
- recommendation_summary near-identical (Jaccard 1.0) — SCH-013, SCH-014
- recommendation_summary near-identical (Jaccard 1.0) — SCH-015, SCH-016
- recommendation_summary near-identical (Jaccard 1.0) — SCH-017, SCH-018
- recommendation_summary near-identical (Jaccard 1.0) — SCH-022, SCH-023
- recommendation_summary near-identical (Jaccard 1.0) — SCH-024, SCH-025
- recommendation_summary near-identical (Jaccard 1.0) — SCH-026, SCH-060

### Instruction-like

- recommendation_summary: matches 'override' pattern — SCH-041

## Columns

| Column | Expected | DuckDB inferred | Blank | Unparseable | Distinct |
|---|---|---|---|---|---|
| applicant_id | id | VARCHAR | 0 | 0 | 60 |
| name | label | VARCHAR | 0 | 0 | 58 |
| city | label | VARCHAR | 0 | 0 | 15 |
| state | label | VARCHAR | 0 | 0 | 7 |
| zip | zip | BIGINT | 0 | 0 | 43 |
| high_school | label | VARCHAR | 0 | 0 | 43 |
| school_type | label | VARCHAR | 0 | 0 | 3 |
| gpa | number | DOUBLE | 1 | 0 | 48 |
| gpa_scale | number | DOUBLE | 1 | 0 | 2 |
| class_rank | rank | VARCHAR | 1 | 0 | 52 |
| test_score | number | BIGINT | 1 | 0 | 45 |
| intended_major | label | VARCHAR | 0 | 0 | 42 |
| household_size | number | BIGINT | 0 | 0 | 5 |
| first_generation | yes_no | VARCHAR | 0 | 0 | 2 |
| work_hours_per_week | number | BIGINT | 0 | 0 | 17 |
| extracurriculars | text | VARCHAR | 0 | 0 | 55 |
| community_service_hours | number | BIGINT | 0 | 0 | 41 |
| essay_prompt | label | VARCHAR | 0 | 0 | 1 |
| essay_text | text | VARCHAR | 1 | 0 | 52 |
| recommendation_summary | text | VARCHAR | 1 | 0 | 51 |
| financial_need_statement | text | VARCHAR | 0 | 0 | 44 |
| submitted_at | timestamp | TIMESTAMP | 0 | 0 | 60 |

## Numeric columns

| Column | Min | Median | Mean | Max |
|---|---|---|---|---|
| gpa | 2.65 | 3.59 | 3.49 | 3.97 |
| gpa_scale | 4.0 | 4.0 | 4.0 | 4.0 |
| test_score | 980.0 | 1295.0 | 1274.407 | 1520.0 |
| household_size | 3.0 | 4.0 | 4.45 | 7.0 |
| work_hours_per_week | 0.0 | 14.0 | 13.433 | 25.0 |
| community_service_hours | 0.0 | 97.5 | 98.25 | 225.0 |

## Value counts

- school_type: magnet: 2, private: 1, public: 57
- first_generation: N: 28, Y: 32
- gpa_scale: (blank): 1, 4.00: 59

## Text lengths (words)

| Column | Min | P5 | Median | P95 | Max | Median chars | Short | Possibly truncated |
|---|---|---|---|---|---|---|---|---|
| extracurriculars | 4 | 4.0 | 6.0 | 9.0 | 10 | 43.5 | 0 | - |
| essay_text | 20 | 55.0 | 85.0 | 107.3 | 114 | 446.0 | 1 | 1 |
| recommendation_summary | 6 | 6.0 | 11.0 | 19.1 | 47 | 86.0 | 0 | 0 |
| financial_need_statement | 4 | 5.0 | 7.0 | 10.05 | 14 | 61.0 | 0 | 0 |

## Sanity checks

| Check | Rule | Failed | Couldn't run |
|---|---|---|---|
| rank_within_class | class rank is between 1 and class size | 0 | 1 |
| gpa_within_scale | GPA is between 0 and its scale, and the scale is positive | 0 | 2 |
| test_score_plausible | test score is a whole number in the ACT (1-36) or SAT (400-1600) range | 0 | 1 |
| household_size_valid | household size is a whole number of at least 1 | 0 | 0 |
| work_hours_valid | work hours per week are between 0 and 168 | 0 | 0 |
| service_hours_valid | community service hours are not negative | 0 | 0 |
| class_size_consistent_per_school | applicants from the same high school report the same class size | 0 | 1 |

## Duplicates

- Repeated applicant_id: 0
- Groups identical except applicant_id: 0
- Pairs with the same name: 2
- Groups with identical numbers: 6
- Near-identical essay_text pairs (Jaccard >= 0.5): 7
- Near-identical recommendation_summary pairs (Jaccard >= 0.5): 8

## Instruction-like text

Pattern matches in text that reaches the model. Categories: override, score_demand, addressed_to_model, prompt_markup, hidden_characters.
Matches: 1. A match is a flag for a person, not proof of anything.

