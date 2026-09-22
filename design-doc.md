# Design: Jev-triaged agentic code review (PoC)

Response to `planning-prompt.md`. No code yet — this is the design for
review before implementation starts.

## 0. Can this plausibly hit the success criteria?

- **Zero missed bugs on the eval set** — plausible if the "never skip on
  low confidence" rule in §3 is strict and the pre-filter (§0 below) only
  ever touches content that can't hide a bug (lockfiles, vendored code,
  whitespace). Risk: a deletion-only hunk or a bug that needs the
  surrounding function to see (see §1) could still slip past Jev if
  context is too narrow — that's the main design risk, not the routing
  math.
- **≥40% of clean hunks skipped** — plausible only if Jev's confidence is
  genuinely well-calibrated on this kind of input; unknown until the
  eval runs. If Jev is *not* well calibrated, everything reduces to
  "review everything," which is still correct (no bugs missed) but
  doesn't save money. §9 comparison report is what actually answers this.
- **Cheaper than LLM-only at equal/better recall** — plausible: Jev is
  billed input-tokens-only per the search results below (output tokens
  free), so triage is cheap per hunk even if some LLM calls are still
  made. Depends on the review LLM's price relative to Jev's, and on skip
  rate actually landing above 0%.

Bottom line: worth building. The two things that could sink it —
context narrowness and calibration — are both testable before any LLM
review call happens (§1 and §6), so the plan spends effort finding that
out early and cheaply.

## 1. Hunk model

A **hunk** is one contiguous `@@ ... @@` block from a unified diff,
scoped to one file. Splitting happens per-file, per-hunk as git already
delimits them — no re-clustering across files.

Each hunk carries:

```
Hunk:
  file_path: str                 # post-change path (new path on rename)
  old_path: str | None            # set only on rename/copy
  change_kind: added | modified | deleted | renamed | mode_only
  header: str                    # the "@@ -a,b +c,d @@" line, verbatim
  old_range: (start, count)
  new_range: (start, count)
  lines: list[DiffLine]          # tagged '+', '-', ' ' (context)
  enclosing_symbol: str | None   # function/class name, from post-file AST
  enclosing_context: str | None  # full enclosing function body, post-change
  is_binary: bool
  is_generated: bool             # heuristic, see pre-filter
  byte_size: int                 # len(lines) rendered, pre-truncation
```

Points from the prompt, addressed directly:

- **Deletion-only hunks.** A hunk with zero `+` lines is not
  special-cased or down-weighted anywhere in triage — it goes through
  the same pipeline. This matters enough to call out because it's the
  most common place a naive tool "sees no new code, nothing to check."
  The eval set includes one deletion-only bug (a removed `finally`)
  specifically to catch a regression here.
- **New files, renames, mode-only changes.** New files are one hunk per
  `@@` block same as any modified file (a wholly-new small file may be a
  single hunk covering the whole thing). Pure renames with no content
  change and mode-only changes skip Jev and the LLM entirely — they go
  through the pre-filter (§0), logged as `skip: no-content-change`, not
  through triage. A rename *with* content changes is treated as a
  normal modified-file hunk against the new path; `old_path` is kept for
  the report only.
- **Oversized hunks.** Cap: 200 diff lines *or* 6,000 characters per hunk
  for the Jev `state` (well under Jev's 32k context per the pricing page
  found in search, leaving headroom for the enclosing-context addition
  below and future question growth). Over that, split on the nearest
  blank-line or top-level-statement boundary inside the hunk into
  sub-hunks that are triaged independently but reported as one logical
  change (shared `hunk_group_id`). If a single unsplittable line run
  (e.g. one huge generated-looking literal) exceeds the cap alone, it's
  truncated with a `[...truncated N lines...]` marker and routed
  straight to `deep-review` — truncation itself is a reason not to trust
  a "harmless" verdict. LLM prompt cap is separate and larger (~12k
  chars) since a human reviewer benefits from more surrounding code than
  Jev needs for classification.
- **Context.** Default context is git's normal 3 lines. Additionally, if
  the hunk falls inside a function/method (found via a language-aware
  AST pass — `ast` module for Python, since scope is Python-only per
  non-goals), the *whole enclosing function from the post-change file*
  is pulled in as `enclosing_context`, separate from the raw diff lines,
  and included in both the Jev `state` and the LLM prompt. This directly
  answers the prompt's concern that a hunk alone often doesn't contain
  the cause — e.g. a changed line that violates an invariant set up
  earlier in the same function. If the enclosing function itself is
  larger than the size cap, only the function signature + docstring +
  the diff lines + immediate ±10 lines are included, and a flag
  `context_truncated: true` is set (this also forces routing away from
  `skip`, see §3).
- **Whole-file input.** The same `Hunk` shape is reused for the eval
  harness's before/after pairs: a sample is two files, diffed with the
  same splitter used in production, so eval exercises the identical
  code path (this was the point raised in §6 of the prompt — no
  separate "buggy file" format). A future "review this directory" mode
  would synthesize one hunk per file (full content as both old/new) but
  is out of scope now.

## 2. Jev questions

One request per hunk, all questions in parallel. Every question's exact
wording lives in `questions.py` next to its type, not here, but the
*shape* is fixed below.

| id | type | why this type |
|---|---|---|
| `risk_category` | `choice` | Unordered discrete buckets (logic, security, concurrency, resource, data-model, style-only, unclear) — there's no natural ordering between "security" and "concurrency," so `choice` is correct, not `score`. |
| `risk_severity` | `score` | Ordered spectrum with describable levels (`cosmetic` → `low` → `moderate` → `high` → `critical`), each level given a one-line description in the question so Jev has a rubric, not just adjectives. |
| `touches_security_sensitive_input` | `noul` | One yes/no fact ("does this hunk handle user input, auth, or a trust boundary") — not a spectrum, not a category, a fact. Kept separate from `risk_category=security` because a hunk can touch a sensitive input *and* be a different kind of bug (e.g. a concurrency bug in auth code); conflating them into one `choice` would lose that. |
| `is_generated_or_lockfile` | `noul` | A second, cheap fact-check behind the pre-filter's heuristic — the pre-filter uses path/content rules for free; this question exists as a safety net for the cases those rules miss (e.g. a lockfile with an unusual extension), so it's asked even on hunks the pre-filter let through, at negligible cost since Jev output tokens are free. |
| `needs_wider_context` | `noul` | Was the enclosing function/context clearly insufficient to judge this hunk? If yes, routing forces at least `light-review` regardless of the other answers (§3) — this is the model's own signal that it's guessing. |

Each question is single-dimension by construction: `risk_category` never
asks "and how bad," `risk_severity` never asks "of what kind." Two
questions that were tempting to merge — "is this risky" and "how risky"
— are kept as one `score` question with a 5-level rubric, not split
into a `noul` + `score` pair, because "is this risky" without a severity
is not independently actionable.

Example `state` string sent for one hunk (SQL string built via
f-string with request data — the routing-logic worked example in §9
of the prompt):

```
FILE: app/db/queries.py
CHANGE: modified
ENCLOSING FUNCTION (post-change):
def get_user_orders(request):
    user_id = request.args.get("user_id")
    query = f"SELECT * FROM orders WHERE user_id = {user_id}"
    return db.execute(query)

DIFF:
@@ -12,3 +12,3 @@ def get_user_orders(request):
     user_id = request.args.get("user_id")
-    query = "SELECT * FROM orders WHERE user_id = %s"
-    return db.execute(query, (user_id,))
+    query = f"SELECT * FROM orders WHERE user_id = {user_id}"
+    return db.execute(query)
```

Diff markers are kept as literal `+`/`-`/` ` prefixes (not converted to
prose) since that's the format Jev's training data almost certainly
matches best for a code-review use case, and it's what a human reviewer
would see too — no translation layer to get wrong.

## 3. Routing logic

Constants, one place (`thresholds.py`):

```python
SEVERITY_SKIP_MAX = 0.15       # score, normalized 0-1, "cosmetic" end
SEVERITY_DEEP_MIN = 0.60       # score, normalized 0-1, "high" end
CONFIDENCE_SKIP_MIN = 0.85     # required on EVERY choice/score answer to skip
SENSITIVE_INPUT_NOUL_MIN = 0.30  # probability above which "sensitive input" counts as true-ish
```

Decision table (evaluated top to bottom, first match wins):

| condition | route |
|---|---|
| `is_generated_or_lockfile.probability >= 0.5` (safety net beyond pre-filter) | `skip` |
| `needs_wider_context.probability >= 0.3` OR `context_truncated` | `light-review` (never skip when the model flags its own context as thin) |
| any of `risk_category.confidence`, `risk_severity.confidence` `< CONFIDENCE_SKIP_MIN` | `light-review` |
| `touches_security_sensitive_input.probability >= SENSITIVE_INPUT_NOUL_MIN` | `light-review` minimum, `deep-review` if severity also `>= SEVERITY_DEEP_MIN` |
| `risk_severity.value <= SEVERITY_SKIP_MAX` (as fraction of levels) | `skip` |
| `risk_severity.value >= SEVERITY_DEEP_MIN` | `deep-review` |
| otherwise | `light-review` |

"Confident across every question" is required for `skip`, not just the
top-line severity — a hunk where Jev is 95% confident it's cosmetic but
only 40% confident on category still isn't skipped, because a
low-confidence category answer means the model isn't sure what it's
even looking at.

**What would make me change a threshold**, as inline comments next to
each constant:

- `SEVERITY_SKIP_MAX` — raise it (skip more) only after §9's comparison
  report shows zero missed bugs across ≥3 runs at the current value
  *and* skip rate is below the 40% target, i.e. there's headroom to
  push further without evidence of a recall cost yet paid.
- `SEVERITY_DEEP_MIN` — lower it (send more to deep review) if the
  missed-bug breakdown in §9 shows bugs reaching `light-review` but
  getting a shallow LLM pass that misses them; raise it if deep-review
  cost dominates the budget without catching anything light-review
  wouldn't have.
- `CONFIDENCE_SKIP_MIN` — lower only if the eval shows Jev is
  systematically under-confident (e.g. correct category calls sitting
  at 0.7-0.8 confidence) rather than actually uncertain; never lower
  it to chase skip rate alone.
- `SENSITIVE_INPUT_NOUL_MIN` — lower (more cautious) if any eval sample
  in the security category is ever routed below `light-review`; that's
  a hard failure of this rule, not a tuning nudge.

**Light vs. deep, concretely different** (if these turned out identical
I'd merge the tiers per the prompt's instruction — they don't):
- `light-review`: review LLM prompt includes the hunk + immediate diff
  context only (not the full enclosing function), asks for at most 2
  comments, cheaper/faster model config.
- `deep-review`: includes the full `enclosing_context`, asks for up to 5
  comments, allowed a second LLM call for a "did I miss anything"
  self-check pass gated by budget.

**Does the review LLM see Jev's labels?** Yes, but framed as a hint, not
a verdict: the prompt says "an automated triage step flagged this hunk
as possibly `<category>`; verify independently and report only issues
you can confirm in the code shown" — never "this is a `<category>` bug,
explain why." This is deliberately hedged against anchoring per the
prompt's own trade-off callout. The eval in §9 checks this doesn't cause
the LLM arm to just echo the category by comparing missed-bug categories
between the labeled hint and what the LLM actually reports.

## 4. Comment gate

Two Jev `noul` questions per comment, run after the LLM writes it:

- `is_actionable` — "does this comment point to a specific, concrete
  change the author could make, rather than a vague observation?"
- `problem_in_shown_code` — "is the problem this comment describes
  actually visible in the code/diff shown to it, not a claim about code
  outside that context?"

`state` for the gate is **comment + the same hunk `state` the LLM saw**,
not the comment alone — `problem_in_shown_code` is meaningless without
the code to check it against.

Gate rule: drop the comment only if *both* probabilities are `< 0.5`.
Requiring both to fail (not either) is the deliberate lean toward the
"too loose" failure mode over "too strict" — see below.

- **Too strict** (drops good comments): the failure is silent — you
  never see a real bug because the report says nothing about that hunk.
  This is worse than noise, because the deep-review LLM call was already
  paid for and the finding is then invisible. This is why every dropped
  comment is logged with its two Jev probabilities to `dropped.jsonl`
  regardless of route, so an audit later can catch over-filtering by
  scanning that log against the eval labels — a comment that matched a
  seeded bug but got dropped is a hard bug in the gate.
- **Too loose** (keeps bad comments): the failure is visible noise in
  the report — annoying, costs your reading time, but self-correcting
  since you see it and can ignore or retune. Strictly preferable to the
  silent failure above, which is why the AND-of-both-probabilities rule
  is biased loose.

## 5. Client design

`jev_client.py`:

```python
def ask(state: str, questions: dict[str, Question]) -> JevResponse | JevError: ...
```

- **Timeout**: 10s connect / 20s total per call (triage and gate calls
  are small; no reason to wait longer — a slow Jev call is routed to
  review anyway per the fail-safe rule).
- **Retry**: on 429/5xx only, up to 3 attempts, exponential backoff with
  jitter (0.5s, 1.5s, 4s ± up to 25%). Never retried: 4xx other than
  429 (a schema bug in our request, retrying won't fix it — log and
  fail safe to review).
- **Parsing isolation**: exactly one function, `parse_jev_response`,
  turns the raw HTTP JSON into the internal `ChoiceAnswer` /
  `ScoreAnswer` / `NoulAnswer` types. Every field name assumption listed
  in "Unverified assumptions" below is read only inside this function,
  so a wrong guess about, say, whether `confidence` is nested under each
  option or top-level is a one-function fix, not a hunt through routing
  logic.
- **Model pinning**: `jev-1.13` is a config constant in `thresholds.py`
  next to the routing thresholds (not hardcoded in the client), with a
  comment that switching to `jev-latest` is explicitly against the
  prompt's own stated risk (§8) and requires re-running the full eval
  before use.
- **Record/replay**: every real response (request hash → raw JSON)
  appended to `.jev_cache/<run_id>.jsonl`, keyed on
  `sha256(model_version + questions_schema + state)`. `--replay <run_id>`
  reads the cache only, calls no API, and re-runs routing — this is what
  makes threshold retuning free after one paid pass.
- **Mock mode**: `--mock` uses a `MockJevBackend` that takes a
  per-hunk-id script (`{hunk_id: {question_id: value}}`) with a default
  fallback answer, plus scriptable failure injection
  (`{hunk_id: "error"}` / `{hunk_id: "timeout"}`) so tests can force
  every branch in §3's decision table, including the fail-safe paths.
  Mock is structural only — never used to draw conclusions about
  question quality, enforced by keeping `--mock` and `--replay` mutually
  exclusive with the eval script's "real" mode flag.
- **Cost/latency accounting**: every call (Jev triage, Jev gate, LLM
  review) appends `{stage, tokens_in, tokens_out, cost, latency_ms}` to
  a per-run ledger, summed into the run's final report.

## 6. Evaluation plan (triage only, no review LLM)

- **Samples**: `samples/<category>/<name>/{before.py, after.py,
  label.json}`. `label.json`: `{bug_hunks: [...], expected_category,
  expected_min_route, bug_lines: [...]}`. Categories: logic, security,
  concurrency, resource-leak, data-modeling — 3 variants each, plus one
  deletion-only bug, plus negative controls including at least 2
  "tricky" negatives (rename inside auth code, comment-only change
  inside a lock block). ~20 samples total for the tuning set.
- **Held-out slice**: a separate `samples_heldout/` directory, same
  structure, written once and never opened again while tuning
  thresholds in §3 — only run at the end, reported separately.
- **Metrics** (`eval.py`, computed from replay or live Jev):
  - Missed bugs: buggy hunks routed to `skip` — list each one by sample
    name, target 0.
  - Review recall: buggy hunks routed to light/deep.
  - Skip rate on clean hunks.
  - Category accuracy vs. `expected_category`.
  - Jev latency/cost per hunk, from the ledger.
- **Baselines**, run by the same script: (a) review-everything (every
  hunk → `deep-review`, no Jev call, zero cost), (b) a plain
  regex/keyword heuristic (`re.search` for `eval(`, string-formatted
  SQL, `except:`, bare `thread`/`lock` tokens, etc.) mapped to the same
  three routes. If Jev doesn't beat (b) on missed-bugs-at-equal-skip-rate,
  that's flagged as the top-line finding, per the prompt's instruction.
- **Runs standalone**: `python eval.py` needs `OPENROUTER_API_KEY` only
  the first time; `python eval.py --replay <run_id>` needs nothing.
  Never imports or calls the review pipeline's LLM stage.

## 7. Output format

**Human report** (`report.md`): per-hunk table — file, route, category,
severity, comments (or "skipped: <reason>" for every skipped hunk, not
just reviewed ones) — plus a summary line (hunks seen / skipped /
light / deep, total cost).

**Machine format** (`report.json`), one entry per comment:
```json
{"file": "app/db/queries.py", "line": 14, "side": "RIGHT",
 "body": "...", "severity": "high", "route": "deep-review",
 "hunk_id": "...", "gate": {"passed": true, "is_actionable": 0.91,
 "problem_in_shown_code": 0.88}}
```
plus a parallel `skipped: [{hunk_id, file, reason}]` array — shaped
directly for a future "post inline PR comment" step (file/line/side/body
is already the GitHub review-comment shape) without designing that
integration now.

## 8. Risks and open questions

- Model ID typo / silent drift to `jev-latest` — mitigated by pinning in
  one config constant (§5) with a comment against auto-upgrading.
- Threshold miscalibration — mitigated by §6's held-out slice and §9's
  variance-aware comparison; not fully solvable before real usage data.
- Comment gate over-filtering — mitigated by requiring both `noul`
  answers to fail before a drop (§4), plus the `dropped.jsonl` audit log.
- Context too narrow to see a bug — the main open technical risk (§1);
  the `needs_wider_context` question and forced-review-on-truncation
  rule are the mitigation, but neither has been tested against a real
  case yet.
- Cost blowup on large PRs — no hunk-count budget is designed yet beyond
  the per-hunk size cap; if a PR eval later shows this mattering, a
  per-run hunk budget with "report what wasn't reviewed" (per the
  fail-safe rule) is the natural extension.
- **API schema gaps** — see Unverified assumptions below; this is the
  biggest near-term risk to getting the client right on the first try.

**Decisions I'd like answered before building, with my recommended
default:**

1. Proceed with the `state`/`questions`/answer shape in §5's "Unverified
   assumptions" as a best-effort implementation, to be corrected once
   `docs.typesafe.ai/api` is reachable or you can paste its schema? —
   **recommend yes**, isolated behind `parse_jev_response` so it's cheap
   to fix.
2. Is Python's stdlib `ast` module sufficient for "enclosing function"
   extraction (§1), or is a more robust parser (e.g. `libcst`) worth the
   extra dependency? — **recommend stdlib `ast`** first; the PoC's
   constraint is minimal dependencies, and `ast` handles the common case.
3. Hunk size cap of 200 lines / 6,000 chars for Jev's `state` (§1) — any
   reason to go tighter given real Jev pricing/latency at that size? —
   **recommend keeping as proposed**, revisit after §6's latency numbers
   come back.
4. 20 tuning samples + held-out slice (§6) — enough, or want more
   categories/variants before the triage checkpoint? — **recommend
   starting at this size**; it's small enough to hand-label carefully
   and can grow before §9 if the triage checkpoint result is ambiguous.

## Unverified assumptions

Verification note: `openrouter.ai`, `docs.typesafe.ai`, and `dev.to` are
all blocked by this session's network egress proxy, so I could not read
primary documentation directly. What follows is from a web search whose
results cite OpenRouter's System One pricing/docs page and a third-party
"awesome-typesafe-jev" write-up (see below); it's second-hand and should
be checked against `docs.typesafe.ai/api` (referenced by that write-up
as the authoritative contract) before the client ships.

**Reasonably well supported by search results** (still worth a spot
check, but multiple independent sources agree):
- Endpoint `POST https://openrouter.ai/api/v1/systemone`, model id
  `jev-1.13`.
- It's a distinct "Decisions API," not OpenAI-chat-compatible — existing
  chat SDKs won't work against it.
- Billing is input-tokens-only; output tokens are free.
- Three question types: `choice`, `score`, `noul`; `noul` answers are a
  single probability 0–1.
- Rough request shape: `{"state": ..., "questions": {"<id>": {"type":
  ..., "instructions": ..., "criteria": {...} | "levels": [...]}}}`.

**Not confirmed by anything found — pure guesses, isolated in
`parse_jev_response`, treat as wrong until checked:**
- Exact field name and range for `confidence` on `choice`/`score`
  answers (assumed: `confidence`, 0–1 float).
- Whether `score` is returned pre-normalized 0–1 or as a raw level index
  needing division by level count (assumed: raw index, client
  normalizes).
- Exact 429/5xx error body shape (assumed: standard HTTP status only,
  no structured retry-after field — client retry logic doesn't depend
  on one).
- Any explicit model-version-pinning request field vs. just baking the
  version into the `model` string (assumed: the latter, `"model":
  "jev-1.13"` in the request body).
- Real per-call latency at ~6,000-char state size (Jev's advertised p50
  is ~100-230ms per search results, but that's likely for smaller
  states — the 200-line hunk cap in §1 is a guess pending real
  measurement in §6).

Sources: search results reference
[Jev 1.13 - OpenRouter](https://openrouter.ai/typesafe/jev-1.13),
[SystemOne - OpenRouter docs](https://openrouter.ai/docs/client-sdks/go/sdks/systemone/README),
[awesome-typesafe-jev](https://github.com/AbdelStark/awesome-typesafe-jev),
[dev.to: How to Use Jev](https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e) —
none fetchable directly from this session; content above is from search
snippets, not the pages themselves.

## File layout

```
jev_review/
  jev_client.py        # §5 — HTTP wrapper, retry, parse_jev_response, record/replay
  mock_backend.py       # §5 — MockJevBackend, scriptable answers/failures
  questions.py           # §2 — question definitions (id, type, instructions, criteria/levels)
  thresholds.py           # §3 — routing constants, model id pin, inline "when to change" comments
  hunk_model.py           # §1 — Hunk dataclass, diff parser, AST enclosing-context extraction, size cap/split
  prefilter.py              # §0 — free path/content rules (lockfiles, generated, binary, whitespace-only)
  routing.py                  # §3 — decision table as a function over Jev answers -> route
  review_pipeline.py            # orchestrates prefilter -> triage -> route -> LLM review -> gate
  gate.py                        # §4 — comment gate questions + drop rule + dropped.jsonl logging
  report.py                       # §7 — report.md / report.json writers
  ledger.py                        # cost/latency accounting shared across stages
eval/
  eval.py                # §6 — triage-only eval, baselines, metrics, standalone-runnable
  compare.py              # §9 — Jev-triaged vs LLM-only, variance runs, full + quick summary reports
samples/
  logic/... security/... concurrency/... resource-leak/... data-modeling/...
  negative/...   # includes tricky negatives
samples_heldout/
  (same structure, untouched during tuning)
tests/
  test_hunk_model.py
  test_routing.py
  test_gate.py
  test_jev_client.py     # against MockJevBackend, forces every branch incl. fail-safe paths
```

## Build order

1. `hunk_model.py` + `prefilter.py` + tests — diff → hunks, no network
   calls. Verify against a few real diffs by hand.
2. `jev_client.py` (against `MockJevBackend` only) + `questions.py` +
   `thresholds.py` + `routing.py` + tests — full triage decision logic,
   provably correct offline.
3. `samples/` + `eval.py`, run in `--mock` first to prove the harness
   works, then with a real `OPENROUTER_API_KEY` once available.
   **Checkpoint A: triage-only results reviewed before touching the LLM
   stage** — decide whether Jev's calibration looks good enough to
   proceed, per §0's plausibility read.
4. `review_pipeline.py` LLM review stage + `gate.py` + `report.py`.
5. `compare.py` (§9) — Jev-triaged vs. LLM-only, full + quick summary
   reports. **Checkpoint B, final deliverable**: verdict on whether the
   PoC is worth continuing past a proof of concept.

Not implementing anything yet — this is the plan for review.
