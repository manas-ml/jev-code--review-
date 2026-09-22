"""Routing decision table (design-doc.md §3): map a hunk + Jev's triage
answers to skip / light-review / deep-review.

Evaluated top to bottom, first match wins, exactly as the decision
table in design-doc.md §3 lists it. Every threshold used here is a
named constant imported from thresholds.py — nothing here should ever
contain a bare number.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jev_review.hunk_model import Hunk
from jev_review.jev_client import JevError, JevResponse
from jev_review.thresholds import (
    CONFIDENCE_SKIP_MIN,
    GENERATED_NOUL_MIN,
    NEEDS_CONTEXT_NOUL_MIN,
    SENSITIVE_INPUT_NOUL_MIN,
    SEVERITY_DEEP_MIN,
    SEVERITY_SKIP_MAX,
)


class Route(str, Enum):
    SKIP = "skip"
    LIGHT_REVIEW = "light-review"
    DEEP_REVIEW = "deep-review"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: str


def route_hunk(hunk: Hunk, jev_result: JevResponse | JevError | None) -> RouteDecision:
    """`jev_result` is the JevResponse on success, or a JevError (or
    None) on failure/timeout. Fail-safe rule (design-doc.md, top-level
    constraint): any failure to get a usable answer routes to review,
    never to skip."""
    if jev_result is None or isinstance(jev_result, JevError):
        return RouteDecision(Route.LIGHT_REVIEW, "jev-call-failed")

    resp = jev_result

    generated = resp.noul("is_generated_or_lockfile").probability
    if generated >= GENERATED_NOUL_MIN:
        return RouteDecision(Route.SKIP, "generated-or-lockfile")

    needs_context = resp.noul("needs_wider_context").probability
    if needs_context >= NEEDS_CONTEXT_NOUL_MIN or hunk.context_truncated:
        return RouteDecision(Route.LIGHT_REVIEW, "needs-wider-context")

    category = resp.choice("risk_category")
    severity = resp.score("risk_severity")
    if category.confidence < CONFIDENCE_SKIP_MIN or severity.confidence < CONFIDENCE_SKIP_MIN:
        return RouteDecision(Route.LIGHT_REVIEW, "low-confidence")

    sensitive = resp.noul("touches_security_sensitive_input").probability
    if sensitive >= SENSITIVE_INPUT_NOUL_MIN:
        if severity.value >= SEVERITY_DEEP_MIN:
            return RouteDecision(Route.DEEP_REVIEW, "sensitive-input-and-high-severity")
        return RouteDecision(Route.LIGHT_REVIEW, "sensitive-input")

    if severity.value <= SEVERITY_SKIP_MAX:
        return RouteDecision(Route.SKIP, "low-severity-confident")

    if severity.value >= SEVERITY_DEEP_MIN:
        return RouteDecision(Route.DEEP_REVIEW, "high-severity")

    return RouteDecision(Route.LIGHT_REVIEW, "moderate-severity")
