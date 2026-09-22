# Planning prompt: Jev-triaged agentic code review (PoC)

Paste this into Claude Code (with the TypeSafe skill installed via
`claude plugin install typesafe@typesafe-ai`) or any coding agent. It asks for
a plan first, not code, per TypeSafe's own "review the plan before
implementing" guidance.

---

## Prompt

I want to build a proof-of-concept agentic code reviewer in Python. Before
writing any code, propose a design. Do not implement anything until I approve
the plan.

**Goal:** review a PR/diff automatically, but only spend LLM tokens on hunks
that actually need a careful look. Cheap heuristics should decide *where to
look*; an LLM should decide *what to say*.

**Architecture constraint — two-model pipeline:**
1. **Triage stage** — TypeSafe's Jev (System One model), called via
   OpenRouter's System One API (`POST https://openrouter.ai/api/v1/systemone`,
   model `jev-1.13`, billed on my `OPENROUTER_API_KEY`). Jev takes code as
   `state` and returns typed answers to typed questions — `choice`, `score`,
   or `noul` (yes/no probability) — never prose. Use it to classify each
   hunk's risk and route it.
2. **Review stage** — a real LLM (Claude, via OpenRouter chat completions)
   writes the actual review comments, but only for hunks Jev routes to it.
3. **Gate stage** — Jev checks each LLM-written comment before it's shown to
   me (e.g. "is this actionable?", "is the problem actually in the code
   shown?") and drops the ones that fail.

Jev decides, the LLM writes. Never ask Jev to generate text, explanations, or
code, and never ask the LLM to do triage that Jev could do more cheaply.

**What I need from you, in order:**

1. **Hunk model.** How should we split a diff or a set of files into
   independently-reviewable units? What metadata does each unit need
   (file, header/line range, surrounding context)?

2. **Jev questions.** Design the triage question set. For each question,
   decide the type (`choice` / `score` / `noul`) and justify it against
   TypeSafe's own guidance: a `score` needs an ordered spectrum with
   describable levels; a `choice` needs unordered discrete categories; a
   `noul` is for one yes/no fact. Keep each question to one dimension —
   split anything that's really measuring two things into two questions.
   Send them in one request per hunk since questions in a request run in
   parallel, so more questions cost little extra time.

3. **Routing logic.** Given Jev's answers (with `confidence` on choice/score
   and raw probability on noul), define the rule that maps a hunk to
   `skip` / `light-review` / `deep-review`. Requirements:
   - Never skip on low confidence — route low-confidence hunks to review
     even if the top category looks harmless.
   - State the thresholds as named constants in one place, not scattered
     through the code, so they're easy to review and tune later.
   - Explain, in a comment, what evidence should make me *change* each
     threshold.

4. **Comment gate.** Design the Jev question(s) used to filter LLM output
   before it reaches me. What's the failure mode if this gate is too strict
   vs. too loose?

5. **Client design.** A thin wrapper around the OpenRouter System One
   endpoint: request/response shapes, retry policy for 429/5xx, and helper
   functions to read a `choice`, `score` (normalized 0–1), and `noul`
   answer. Also design a `--mock` mode that runs the whole pipeline with
   fake but deterministic answers so the plumbing can be tested without an
   API key. Note that mock answers are only for testing structure, never for
   evaluating question quality.

6. **Evaluation plan.** Before wiring up the real LLM stage, how do we check
   Jev's triage alone is any good? Propose:
   - A small set of hand-written Python files with *known, reproducible*
     bugs, spanning a few different risk categories (e.g. a logic error,
     a security-sensitive input, a concurrency bug, a resource leak, a
     data-modeling bug), plus at least one clean/cosmetic-only file as a
     negative control.
   - Metrics: category accuracy against a hand-labeled expected value,
     review recall (buggy files that get sent to an LLM), skip precision
     (clean files that get skipped), and Jev's own reported latency/cost.
   - How this script should be runnable on its own, cheaply, without ever
     calling the review-writing LLM.

7. **Output format.** What does a finished review report look like — for a
   human reading it, and for something that could later post inline PR
   comments?

8. **Risks and open questions.** What's likely to go wrong (model ID typos,
   threshold miscalibration, Jev version drift if I use `jev-latest`
   instead of pinning a version, the comment gate over-filtering real
   issues)? What would you want me to decide before you start building?

**Format of your answer:** a short design doc (headings for each numbered
item above), followed by a proposed file layout (one file per concern:
Jev client, questions/thresholds, hunk splitting, review pipeline, mock
backends, eval script, samples). No code yet — I'll review this plan and
tell you to proceed.

---

*Tip: after the agent responds, ask it to walk through one hunk by hand —
"trace `sql_injection.py` through the whole pipeline with hypothetical Jev
answers" — before it writes any code. That surfaces routing-logic bugs for
free.*
