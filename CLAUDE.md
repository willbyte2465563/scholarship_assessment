# CLAUDE.md

We're scoring scholarship applications against the Hargrove rubric (docs/rubric.md). The tool suggests a shortlist and a person picks the final 3. Whatever we build, someone should be able to explain a decision months from now just from the logs.

The risks I'm worried about are in docs/RISKS.md. Read it before you start anything big.

## Scope

About 6 hours total. Out of scope: auth, a database, multi-user. Ask before adding anything bigger than what's described here.

## Running stuff

Everything runs in Docker. Don't run python or pip on my machine directly.

- tests: `docker compose run --rm app pytest`
- app: `docker compose up` (port 8000)
- anything that calls Gemini costs money, so only run it when I ask

If you need a new package, ask first.

The model is pinned to `gemini-3.8-flash`, set in .env as `GEMINI_MODEL`. Every scoring call records the model in the log.

## Tests

- Normal tests run offline. They can't hit the network.
- Anything that calls Gemini is an eval. Evals are marked separately, aren't part of the normal test run, and only run when I ask.

## Rules

- Nothing that identifies the applicant goes to the LLM: name (including inside the essay and recommendation text), city/state/zip, high school, school type, submission time. Class rank goes as a percentile, never N/M. Pronouns are neutralized in the recommendation only, not the essay.
- One applicant per LLM call, always. No batching.
- Reviewers work blind (no name, location or school) until the final 3 is recorded.
- The LLM gives criterion scores. The math (totals, ranking, ties) is our code, not the model.
- The source CSV is never edited. Derived copies (like the redacted view) are fine as long as we log how they were made. No filling in blanks, no merging duplicates, no "cleaning". If something looks wrong, flag it.

## How I like to work

- Small steps. If a change touches more than a couple of files, tell me the plan first.
- Write tests with the code, not after.
- If one of these rules gets in your way, ask me. Don't find a way around it.
- If you're not sure what the rubric means, ask. Don't guess.
- I make the commits. When a step is done, suggest a commit message.
