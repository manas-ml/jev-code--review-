"""Routing thresholds and pinned model config (design-doc.md §3/§5).
Kept in one place so they're easy to review and tune later — nothing in
routing.py or jev_client.py should hardcode a number that belongs here.
"""

from __future__ import annotations

# --- Model pinning -----------------------------------------------------
# Pinned per design-doc.md §8 risk: "Jev version drift if I use
# jev-latest instead of pinning a version." Do NOT switch this to
# "jev-latest" without re-running the full eval (eval/eval.py) first —
# a version change can silently shift calibration and invalidate every
# threshold below.
JEV_MODEL_ID = "jev-1.13"
SYSTEMONE_ENDPOINT = "https://openrouter.ai/api/v1/systemone"

# --- Routing thresholds (design-doc.md §3) ------------------------------

# Score, normalized 0-1 across risk_severity's 5 levels. A hunk this far
# toward "cosmetic" or below, with every question confident (see
# CONFIDENCE_SKIP_MIN), is skipped.
#
# Raise this (skip more) only after eval/compare.py shows zero missed
# bugs across >=3 comparison runs at the current value AND skip rate is
# below the 40% target — i.e. there's evidence of headroom, not just a
# desire for a higher skip rate.
SEVERITY_SKIP_MAX = 0.15

# Score, normalized 0-1. A hunk at or above this severity goes to
# deep-review (subject to the security-sensitive-input rule below too).
#
# Lower this (send more to deep review) if eval/compare.py's missed-bug
# breakdown shows bugs reaching light-review but getting a shallow LLM
# pass that misses them. Raise it if deep-review cost dominates the
# budget without catching anything light-review wouldn't have.
SEVERITY_DEEP_MIN = 0.60

# Required minimum `confidence` on EVERY choice/score answer before a
# hunk can be skipped at all (design-doc.md §3: "confident across every
# question," not just the top-line severity).
#
# Lower only if eval/compare.py shows Jev is systematically
# under-confident (correct calls sitting well below this threshold)
# rather than actually uncertain about ambiguous hunks. Never lower it
# just to chase a higher skip rate.
CONFIDENCE_SKIP_MIN = 0.85

# Probability (noul, 0-1) above which touches_security_sensitive_input
# counts as true-ish and forces at least light-review.
#
# Lower (more cautious) immediately if any eval sample in the security
# category is ever routed below light-review — that is a hard failure
# of this rule, not a tuning nudge.
SENSITIVE_INPUT_NOUL_MIN = 0.30

# Probability (noul, 0-1) above which needs_wider_context / a truncated
# enclosing-context forces at least light-review, regardless of other
# answers — the model flagging its own uncertainty about context.
NEEDS_CONTEXT_NOUL_MIN = 0.30

# Probability (noul, 0-1) above which is_generated_or_lockfile causes a
# skip, as a safety net behind the free path-based pre-filter.
GENERATED_NOUL_MIN = 0.5

# --- Comment gate (design-doc.md §4) ------------------------------------
# A comment is dropped only if BOTH probabilities are below this bar —
# deliberately biased toward keeping (too loose, visible noise) over
# dropping (too strict, silent missed finding). See design-doc.md §4.
GATE_DROP_MAX = 0.5

# --- Client retry policy (design-doc.md §5) ------------------------------
CONNECT_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 20.0
MAX_RETRIES = 3
RETRY_BASE_DELAYS_S = (0.5, 1.5, 4.0)
RETRY_JITTER_FRACTION = 0.25
