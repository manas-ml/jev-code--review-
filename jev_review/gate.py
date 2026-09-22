"""Comment gate (design-doc.md §4): Jev checks each LLM-written comment
before it reaches the human. Drop only if BOTH `is_actionable` and
`problem_in_shown_code` are below GATE_DROP_MAX — deliberately biased
toward the "too loose" failure (visible noise, self-correcting) over
"too strict" (a real finding silently disappears after the review LLM
call was already paid for). Every drop is logged with both
probabilities so over-filtering can be audited later.

Per the top-level fail-safe rule: a gate call that errors keeps the
comment, marked `ungated=True`, rather than dropping it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jev_review.hunk_model import Hunk
from jev_review.jev_client import JevBackend, JevError, ReplayCacheMiss
from jev_review.llm_client import ReviewComment
from jev_review.questions import GATE_QUESTIONS
from jev_review.thresholds import GATE_DROP_MAX


@dataclass
class GateResult:
    comment: ReviewComment
    kept: bool
    ungated: bool
    is_actionable: float | None
    problem_in_shown_code: float | None
    reason: str


def _render_gate_state(comment: ReviewComment, hunk: Hunk) -> str:
    """state is comment + hunk, not the comment alone — design-doc.md
    §4: 'problem_in_shown_code is meaningless without the code to check
    it against.'"""
    return (
        f"REVIEW COMMENT: {comment.body}\n"
        f"CITED LINE: {comment.line} ({comment.side})\n\n"
        + hunk.render_state()
    )


def gate_comment(comment: ReviewComment, hunk: Hunk, jev_backend: JevBackend) -> GateResult:
    state = _render_gate_state(comment, hunk)
    try:
        resp = jev_backend.ask(state, GATE_QUESTIONS, hunk_id=f"gate:{hunk.hunk_id}:{id(comment)}")
    except (JevError, ReplayCacheMiss) as e:
        # Fail-safe: keep, don't drop, on a gate failure.
        return GateResult(comment, kept=True, ungated=True, is_actionable=None, problem_in_shown_code=None, reason=f"gate-call-failed: {e}")

    actionable = resp.noul("is_actionable").probability
    in_scope = resp.noul("problem_in_shown_code").probability
    if actionable < GATE_DROP_MAX and in_scope < GATE_DROP_MAX:
        return GateResult(comment, kept=False, ungated=False, is_actionable=actionable, problem_in_shown_code=in_scope, reason="both-below-threshold")
    return GateResult(comment, kept=True, ungated=False, is_actionable=actionable, problem_in_shown_code=in_scope, reason="kept")


def gate_comments(
    comments: list[ReviewComment],
    hunk: Hunk,
    jev_backend: JevBackend,
    *,
    dropped_log_path: str | Path | None = None,
) -> list[GateResult]:
    results = [gate_comment(c, hunk, jev_backend) for c in comments]
    if dropped_log_path:
        path = Path(dropped_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in results:
                if not r.kept:
                    f.write(
                        json.dumps(
                            {
                                "file": r.comment.file,
                                "line": r.comment.line,
                                "body": r.comment.body,
                                "hunk_id": r.comment.hunk_id,
                                "is_actionable": r.is_actionable,
                                "problem_in_shown_code": r.problem_in_shown_code,
                                "reason": r.reason,
                            }
                        )
                        + "\n"
                    )
    return results
