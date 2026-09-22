"""Scriptable mock Jev backend (design-doc.md §5). Used for offline
testing of the pipeline's structure — never for evaluating question
quality, which is what eval/eval.py against the real API (or a replay
cache) is for.

Mock answers are built as raw JSON dicts and passed through the real
`parse_jev_response`, so a test exercises the same parsing path a real
response would.
"""

from __future__ import annotations

from typing import Literal

from jev_review.jev_client import JevError, JevResponse, parse_jev_response
from jev_review.questions import Question, QuestionType

Failure = Literal["error", "timeout"]
AnswerOverride = float | dict  # noul shorthand (float) or full override dict
ScriptEntry = dict[str, AnswerOverride] | Failure


def _default_raw_answer(q: Question) -> dict:
    """Confident-and-harmless default for any question not explicitly
    scripted, so unscripted hunks behave predictably rather than
    erroring."""
    if q.type == QuestionType.CHOICE:
        options = list(q.criteria) or ["style-only"]
        top = "style-only" if "style-only" in options else options[0]
        probs = {opt: (0.9 if opt == top else 0.1 / max(len(options) - 1, 1)) for opt in options}
        return {"value": top, "probabilities": probs, "confidence": 0.95}
    if q.type == QuestionType.SCORE:
        return {"value": 0.05, "confidence": 0.95}
    return {"probability": 0.05}  # noul


def _apply_override(q: Question, override: AnswerOverride) -> dict:
    base = _default_raw_answer(q)
    if q.type == QuestionType.NOUL and isinstance(override, (int, float)):
        return {"probability": float(override)}
    if isinstance(override, dict):
        base.update(override)
        return base
    raise ValueError(f"invalid override for question '{q.id}' ({q.type.value}): {override!r}")


class MockJevBackend:
    def __init__(
        self,
        script: dict[str, ScriptEntry] | None = None,
        default_only: bool = False,
    ):
        """`script` maps hunk_id -> either a failure string ("error" /
        "timeout") or a dict of {question_id: override}. Any question
        not present in that dict, or any hunk_id not in `script` at
        all, gets the confident-harmless default. `default_only=True`
        ignores `script` entirely (useful for a "review everything
        looks fine" baseline run)."""
        self.script = {} if default_only else (script or {})
        self.calls: list[tuple[str | None, list[str]]] = []

    def ask(self, state: str, questions: dict[str, Question], *, hunk_id: str | None = None) -> JevResponse:
        self.calls.append((hunk_id, list(questions.keys())))
        entry = self.script.get(hunk_id or "")

        if entry == "error":
            raise JevError(f"mock error injected for hunk_id={hunk_id!r}", retryable=False)
        if entry == "timeout":
            raise JevError(f"mock timeout injected for hunk_id={hunk_id!r}", retryable=True)

        overrides: dict = entry if isinstance(entry, dict) else {}
        answers_raw = {}
        for qid, q in questions.items():
            answers_raw[qid] = _apply_override(q, overrides.get(qid, {}))

        raw = {"answers": answers_raw, "usage": {"input_tokens": len(state) // 4, "output_tokens": 0, "cost": 0.0}}
        return parse_jev_response(raw, questions)
