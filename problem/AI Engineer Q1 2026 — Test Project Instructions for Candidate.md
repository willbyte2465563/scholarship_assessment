# Senior AI Engineer Test Project

Your goal is to use agentic AI to evaluate college scholarship candidates fairly.

Build an agentic web app that suggests the top X candidates, then lets a human compare them and pick a final 3. Along the way, keep a record of what happened — enough that someone reviewing this in six months can tell how a decision got made. A JSON file is fine. We care about what you record, not where you put it.

We're sending you two things:

- **`scholarship_candidates.csv`** — 60 applicant profiles
- **The Hargrove Family Scholarship rubric** — the committee's five criteria, their weights, and what each one means

Score against the rubric as written.

The profiles are made up. Nobody real is in there, and nothing you build gets used on actual students.

## Time

**Plan for 4–6 hours. Stop at 6.** We mean it; going over won't earn you anything.

We're not going to tell you what to build or what to skip. How you spend those six hours is most of what we're looking at. Scope it yourself, then tell us how you scoped it. Someone who built less and scoped it deliberately beats someone who built more and can't explain the tradeoffs.

## Questions to Answer

1. How do you prevent bias from creating unfair advantages?
2. What should X be to ensure fair results (top X candidates for human review)?
3. Are there any other points at which a human should be in the loop?
4. How would you ensure consistent, fair, quality results over time using a non-deterministic LLM?
5. What plumbing would you add to meet typical financial industry governance standards?
6. Where did you stop?
	1. What did you leave out on purpose, and why was that the right call at six hours or less?
	2. What would you build next, in order?

You don't have to answer these in the app. If you'd rather write the answer than build it, write it — a question answered well in prose beats one answered thinly in code. Keep each answer to about 200 words; question 6 can be shorter. We're reading for your reasoning, not your word count.

## Requirements

**Use Gemini for the LLM calls** so it's easy for us to run locally with our own key. Read the key from an environment variable named `GEMINI_API_KEY`. If you'd rather develop against something else, go ahead — just make sure that's the documented path for us.

**Include a web interface.** If this is your strength, show us what you've got. If it's not, focus elsewhere but keep the UI clean and simple.

**Make it run in Docker.** We want to clone the repo and run one command — `docker compose up` or whatever you document. Don't build us an image or ask us to stand anything up. A Dockerfile and compose file in the repo is all we need. We'll run it on a Mac with Rancher Desktop. No GPU, nothing in the cloud except the LLM call.

**Deliver your code via git repository.**

**In a README or docs section,** tell us how to run it and answer the questions above.

**Show us how you drove the AI.** Commit whatever steered your tooling — `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, plan files, prompts, notes you wrote to the agent. We want to see how you work with these tools, not just what came out.

## What We're Looking For

We want to see agentic development, but we primarily want to see you steer it toward quality code. Mostly we're looking for taste and clear communication.

Specifically:

- Code quality and testability
- Whether your approach to fairness is something you can defend, not just describe
- Whether your human-in-the-loop design shows you know where the model shouldn't be trusted
- Whether you steered the AI or it steered you
- How clearly you explain your reasoning and your tradeoffs

Lean into your strengths. If you're great at documentation, show us. If you're an observability guru, show us how you trace. If you're a testing or evals pro, put that front and center.

## Ask Us Questions

Ask as many as you'd like. How you work with us on this tells us a lot about how you'd work with the team. Reply to the test project email thread, and we'll get back to you within a business day.

## Submitting

Send us the repo link any time before your Round 3 interview. You'll walk us through it for the first 15 minutes of that interview, and we'll review it in detail afterward.