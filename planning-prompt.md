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

**Success criteria for the PoC** (the plan should say how each is measured):
- Zero seeded bugs routed to `skip` on the eval set.
- A meaningful share of clean/cosmetic hunks skipped (target: ≥40%).
- Total cost (Jev + LLM) per PR lower than "LLM reviews every hunk", at equal
  or better bug recall. This is measured head-to-head in the final
  comparison report (item 9).
If the design can't plausibly meet these, say so up front.

**Non-goals for now:** posting to GitHub, languages other than Python,
cross-file/whole-repo reasoning, a UI. Design so these can be added later,
but don't build them.

**Architecture constraint — three-stage pipeline, two models:**
0. **Pre-filter (no model)** — drop hunks that never need review by path or
   content rules alone: lockfiles, generated files, vendored code, binary
   files, whitespace-only changes. These are free; don't pay Jev for them.
1. **Triage stage** — TypeSafe's Jev (System One model), called via
   OpenRouter's System One API (`POST https://openrouter.ai/api/v1/systemone`,
   model `jev-1.13`, billed on my `OPENROUTER_API_KEY`). Jev takes code as
   `state` and returns typed answers to typed questions — `choice`, `score`,
   or `noul` (yes/no probability) — never prose. Use it to classify each
   hunk's risk and route it.
2. **Review stage** — a real LLM (Claude, via OpenRouter chat completions,
   with the model ID pinned in config) writes the actual review comments,
   but only for hunks Jev routes to it.
3. **Gate stage** — Jev checks each LLM-written comment before it's shown to
   me (e.g. "is this actionable?", "is the problem actually in the code
   shown?") and drops the ones that fail.

Jev decides, the LLM writes. Never ask Jev to generate text, explanations, or
code, and never ask the LLM to do triage that Jev could do more cheaply.

**API facts — verify, don't invent.** Everything I've said above about the
System One endpoint (URL, model ID, request fields, response fields,
`confidence` on choice/score, raw probability on noul, the score scale) is
from my notes and may be wrong or incomplete. Before designing the client,
check the TypeSafe skill's reference material and/or OpenRouter's docs for
the actual request/response schema, and cite what you found. Anything you
could not confirm goes in a clearly labelled **"Unverified assumptions"**
list, and the client design must isolate it behind one parsing function so a
wrong guess is a one-place fix.

**Fail-safe direction.** Every failure must push toward *more* review, never
less:
- Jev call fails, times out, returns an unparseable or missing answer →
  route the hunk to review (not skip).
- Gate call fails → keep the comment, marked "ungated", rather than drop it.
- Budget exhausted → stop, and report which hunks were not reviewed; never
  silently mark them as clean.

**What I need from you, in order:**

1. **Hunk model.** How should we split a diff into independently-reviewable
   units? What metadata does each unit need (file, header/line range,
   surrounding context, added vs. removed lines)? Address explicitly:
   - Deletion-only hunks (a removed lock or `finally` is often *the* bug).
   - New files, renames, and mode-only changes.
   - Oversized hunks (what's the size cap for Jev's `state` and for the LLM
     prompt, and how do we split or truncate beyond it?).
   - How much surrounding context to include, and whether the enclosing
     function should be pulled in from the post-change file, since a hunk
     alone often doesn't contain the cause of the bug.
   - Whether the same file can also be fed as a whole (for the eval set and
     for "review this directory" use), and how that maps onto hunks.

2. **Jev questions.** Design the triage question set. For each question,
   decide the type (`choice` / `score` / `noul`) and justify it against
   TypeSafe's own guidance: a `score` needs an ordered spectrum with
   describable levels; a `choice` needs unordered discrete categories; a
   `noul` is for one yes/no fact. Keep each question to one dimension —
   split anything that's really measuring two things into two questions.
   Send them in one request per hunk since questions in a request run in
   parallel, so more questions cost little extra time. Show the exact
   `state` string you'd send for one example hunk (how are diff markers,
   file path, and context formatted?).

3. **Routing logic.** Given Jev's answers, define the rule that maps a hunk
   to `skip` / `light-review` / `deep-review`. Requirements:
   - Never skip on low confidence — route low-confidence hunks to review
     even if the top category looks harmless. Define how confidence is
     combined across questions (e.g. "skip only if *every* question is
     confident and harmless").
   - State the thresholds as named constants in one place, not scattered
     through the code, so they're easy to review and tune later.
   - Explain, in a comment, what evidence should make me *change* each
     threshold.
   - Present the rule as a small decision table I can check by eye.
   - Say what "light" vs "deep" review actually changes (model, context
     size, prompt, max comments) — if nothing concrete differs, merge them.
   - Decide whether the review LLM sees Jev's triage labels. Consider the
     trade-off: focus vs. anchoring on a wrong category.

4. **Comment gate.** Design the Jev question(s) used to filter LLM output
   before it reaches me, including what goes into `state` (the comment
   alone, or comment + hunk). What's the failure mode if this gate is too
   strict vs. too loose? Dropped comments must be logged with the Jev
   answers that dropped them, so I can audit over-filtering.

5. **Client design.** A thin wrapper around the OpenRouter System One
   endpoint: request/response shapes (confirmed per "API facts" above),
   timeouts, retry policy for 429/5xx (with backoff and a max), and helper
   functions to read a `choice`, `score` (normalized 0–1), and `noul`
   answer. Also:
   - **Record/replay.** Every real Jev response is written to a JSONL cache
     keyed by (model version, question set hash, state hash). A `--replay`
     mode re-runs routing from the cache with zero API calls, so thresholds
     can be retuned for free after one paid run.
   - **Mock mode.** `--mock` runs the whole pipeline with fake but
     deterministic answers so the plumbing can be tested without an API
     key. Mock answers must be scriptable per hunk so tests can force every
     routing branch (low confidence, API error, gate drop, etc.). Mock
     answers are only for testing structure, never for evaluating question
     quality.
   - Per-run cost and latency accounting for both Jev and the LLM.

6. **Evaluation plan.** Before wiring up the real LLM stage, how do we check
   Jev's triage alone is any good? Propose:
   - Eval samples as **before/after pairs that produce a real diff**, not
     just buggy files — so the eval exercises the same hunk splitting and
     `state` format as production. Each sample carries a hand-written label
     file: the hunk(s) containing the bug, the expected category, the
     expected minimum route, and the line(s) where the bug is.
   - Coverage: at least 3 variants for each of a few risk categories (a
     logic error, a security-sensitive input, a concurrency bug, a resource
     leak, a data-modeling bug), a deletion-only bug, and several negative
     controls — including *tricky* negatives that look risky but are safe
     (e.g. a rename inside auth code, a comment-only change in a lock
     block). Every bug must be reproducible by a small test or a one-line
     explanation.
   - A held-out slice that isn't looked at while tuning thresholds, since
     the set is small enough to overfit by hand.
   - Metrics, precisely defined:
     - **Missed bugs** — buggy hunks routed to `skip` (target: 0; list each
       one, don't just count).
     - **Review recall** — buggy hunks routed to light or deep review.
     - **Skip rate on clean hunks** — share of clean hunks skipped (this is
       where the savings come from).
     - Category accuracy against the hand label.
     - Jev latency and cost per hunk.
   - **Baselines:** compare against (a) review everything, and (b) the
     pre-filter plus a plain regex/keyword heuristic. If Jev doesn't beat
     (b), that's the most important finding of the PoC.
   - How this script runs on its own, cheaply, without ever calling the
     review-writing LLM — and how `--replay` lets it re-score without
     calling Jev either.
   - A separate, later check for the gate: a handful of hand-written good
     and bad review comments per sample, to measure what the gate drops.

7. **Output format.** What does a finished review report look like — for a
   human reading it, and as machine-readable JSON that could later post
   inline PR comments (file, line, side, body, severity)? The report should
   also list every skipped hunk with the reason, so I can spot-check skips.

8. **Risks and open questions.** What's likely to go wrong (model ID typos,
   threshold miscalibration, Jev version drift if I use `jev-latest`
   instead of pinning a version, the comment gate over-filtering real
   issues, context too narrow to see the bug, cost blowup on huge PRs)?
   What would you want me to decide before you start building? End with a
   numbered list of decisions, each with your recommended default, so I
   can answer them by number.

9. **Comparison report: Jev-triaged vs. LLM-only.** This is the final step.
   Once the full pipeline runs, run the same eval samples through two
   arms and write a side-by-side report:
   - **Arm A: Jev-triaged.** Pre-filter → Jev triage → LLM review of routed
     hunks → Jev gate.
   - **Arm B: LLM-only.** Pre-filter → LLM reviews *every* hunk, with no
     Jev anywhere.
   - Keep it a fair fight: both arms use the same LLM model ID, review
     prompt, context size and temperature. The only difference is whether
     Jev is in the loop. If "deep review" uses a stronger prompt than
     "light", Arm B uses the deep prompt on every hunk. Otherwise LLM-only
     is set up to lose.
   - **Matching comments to bugs.** Define how a comment counts as finding
     a seeded bug before looking at results. For example, the comment
     touches the labelled bug line(s) ± N lines and names the right problem.
     Settle "names the right problem" with a fixed rule or an LLM judge
     using a pinned rubric, applied identically to both arms. Show a few
     matched and unmatched examples so I can check the matcher.
   - **Metrics per arm:**
     - Bugs found / bugs missed. List each missed bug by name, and flag any
       bug that one arm caught and the other didn't.
     - Missed-bug cause for Arm A: skipped by triage, reviewed but the LLM
       missed it, or found but dropped by the gate. This says which stage
       to fix.
     - Noise: comments that don't match any seeded bug, per arm. Hand-label
       a sample of them as useful/not useful, since some may be real.
     - Comments on clean/negative-control samples (should be ~0).
     - Tokens, cost, and wall-clock latency, split by stage (Jev, LLM,
       gate), plus the share of hunks the LLM actually saw in Arm A.
   - **Variance:** LLM output varies between runs, so run each arm at least
     3 times (or explain why fewer is enough) and report mean and spread. A
     one-bug difference on this small set is not a result.
   - **Held-out slice** results reported separately from the tuning set.
   - **Format:** a Markdown report for me (a summary table on top,
     per-sample table below, then the list of disagreements between arms)
     plus the raw JSON it was built from. Include a closing **verdict**:
     does Jev triage keep recall within an agreed margin of LLM-only while
     cutting cost? Is the gate removing more noise than real findings? Is
     the PoC worth continuing? Give the numbers behind each answer.
   - **Quick summary report.** Also write a separate `summary.md`, under
     about 20 lines, that I can read in a minute or paste into a chat
     without editing:
     - A one-line verdict, e.g. "Jev triage: same recall (9/10 vs 9/10),
       58% cheaper, 2 fewer noise comments. Worth continuing."
     - A small table with one row per metric and one column per arm, plus
       a difference column: bugs found, bugs missed, noise comments, cost,
       latency, and share of hunks the LLM saw.
     - The top 3 findings, e.g. bugs only one arm caught, or the stage
       losing the most bugs.
     - One recommended next step.
     - A link to the full report and the run ID/date/model versions, so the
       numbers can be traced back.
     Generate it from the same JSON as the full report, never retyped by
     hand, so the two can't disagree.
   - The comparison runs from one command (e.g. `compare.py`). It reuses the
     record/replay cache for Jev, and also caches LLM responses so the report
     can be regenerated without paying again. Say roughly what one full
     comparison run costs.

**Constraints:** Python 3.11+, minimal dependencies (an HTTP client and a
test runner; no agent framework), `OPENROUTER_API_KEY` read from the
environment only and never logged, and the test suite must pass offline
using mock/replay.

**Format of your answer:** a short design doc (headings for each numbered
item above), then the "Unverified assumptions" list, then a proposed file
layout (one file per concern: Jev client, questions/thresholds, hunk
splitting, pre-filter, review pipeline, mock/replay backends, eval script,
comparison script and report, samples, tests). Then a build order in small,
individually testable steps. Include two explicit checkpoints where I decide
whether to continue: the eval-only milestone (triage measured, no LLM
stage), and the Jev-vs-LLM-only comparison report as the final deliverable. No code yet — I'll review
this plan and tell you to proceed.

---

*Tip: after the agent responds, ask it to walk through one sample by hand —
"trace the SQL-injection sample through the whole pipeline with
hypothetical Jev answers, including one where Jev's call fails and one
where confidence is low" — before it writes any code. That surfaces
routing-logic and fail-safe bugs for free.*
