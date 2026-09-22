"""Jev triage question set (design-doc.md §2) and the comment-gate
question set (design-doc.md §4). Wording lives here, next to the type
it's paired with; routing thresholds live in thresholds.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class QuestionType(str, Enum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


@dataclass(frozen=True)
class Question:
    id: str
    type: QuestionType
    instructions: str
    # choice only:
    criteria: dict[str, str] = field(default_factory=dict)
    # score only, ordered low -> high:
    levels: list[str] = field(default_factory=list)


RISK_CATEGORY = Question(
    id="risk_category",
    type=QuestionType.CHOICE,
    instructions=(
        "What kind of change is this hunk, if any risk is present at all? "
        "Pick the single best-fitting category."
    ),
    criteria={
        "logic": "A conditional, loop, or calculation that could produce a wrong result.",
        "security": "Handles user input, auth, secrets, or a trust boundary in a way that could be exploited.",
        "concurrency": "Shared state, locking, or async/thread ordering that could race or deadlock.",
        "resource": "Opens, allocates, or acquires something (file, socket, lock, connection) that might not be released.",
        "data-model": "Changes how data is shaped, validated, or persisted in a way that could corrupt or misinterpret it.",
        "style-only": "Formatting, naming, comments, or other changes with no behavior difference.",
        "unclear": "Not enough information in the hunk to tell.",
    },
)

RISK_SEVERITY = Question(
    id="risk_severity",
    type=QuestionType.SCORE,
    instructions=(
        "How severe would this hunk's issue be if the risk category above is real? "
        "Judge the worst plausible consequence, not the likelihood."
    ),
    levels=[
        "cosmetic: no behavior change, or a harmless formatting/naming difference",
        "low: a minor inconvenience or an edge case with limited impact",
        "moderate: a real bug affecting some users or some inputs",
        "high: a bug likely to affect most users/inputs, or a real security weakness",
        "critical: data loss, security breach, or a crash/outage in normal operation",
    ],
)

TOUCHES_SECURITY_SENSITIVE_INPUT = Question(
    id="touches_security_sensitive_input",
    type=QuestionType.NOUL,
    instructions=(
        "Does this hunk handle user-controlled input, authentication, authorization, "
        "secrets, or any other trust boundary?"
    ),
)

IS_GENERATED_OR_LOCKFILE = Question(
    id="is_generated_or_lockfile",
    type=QuestionType.NOUL,
    instructions=(
        "Is this hunk part of an auto-generated file, a dependency lockfile, or "
        "vendored third-party code that a human wouldn't hand-review?"
    ),
)

NEEDS_WIDER_CONTEXT = Question(
    id="needs_wider_context",
    type=QuestionType.NOUL,
    instructions=(
        "Is the code shown insufficient to judge this hunk with confidence — would "
        "you need to see more of the surrounding file, caller, or callee to be sure?"
    ),
)

TRIAGE_QUESTIONS: dict[str, Question] = {
    q.id: q
    for q in (
        RISK_CATEGORY,
        RISK_SEVERITY,
        TOUCHES_SECURITY_SENSITIVE_INPUT,
        IS_GENERATED_OR_LOCKFILE,
        NEEDS_WIDER_CONTEXT,
    )
}

IS_ACTIONABLE = Question(
    id="is_actionable",
    type=QuestionType.NOUL,
    instructions=(
        "Does this review comment point to a specific, concrete change the author "
        "could make, rather than a vague observation?"
    ),
)

PROBLEM_IN_SHOWN_CODE = Question(
    id="problem_in_shown_code",
    type=QuestionType.NOUL,
    instructions=(
        "Is the problem this comment describes actually visible in the code/diff "
        "shown, rather than a claim about code outside that context?"
    ),
)

GATE_QUESTIONS: dict[str, Question] = {
    q.id: q for q in (IS_ACTIONABLE, PROBLEM_IN_SHOWN_CODE)
}
