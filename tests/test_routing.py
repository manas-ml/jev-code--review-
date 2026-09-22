import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.hunk_model import ChangeKind, DiffLine, Hunk
from jev_review.jev_client import JevError
from jev_review.mock_backend import MockJevBackend
from jev_review.questions import TRIAGE_QUESTIONS
from jev_review.routing import Route, route_hunk

STATE = "FILE: f.py\nDIFF:\n@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n"


def _hunk(context_truncated=False):
    return Hunk(
        file_path="f.py",
        change_kind=ChangeKind.MODIFIED,
        header="@@ -1,1 +1,1 @@",
        old_range=(1, 1),
        new_range=(1, 1),
        lines=[DiffLine("-", "a = 1"), DiffLine("+", "a = 2")],
        context_truncated=context_truncated,
    )


def _resp(overrides, hunk_id="h1"):
    backend = MockJevBackend(script={hunk_id: overrides})
    return backend.ask(STATE, TRIAGE_QUESTIONS, hunk_id=hunk_id)


def test_jev_call_failure_routes_to_light_review_never_skip():
    decision = route_hunk(_hunk(), JevError("boom"))
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "jev-call-failed"


def test_none_result_also_routes_to_review():
    decision = route_hunk(_hunk(), None)
    assert decision.route == Route.LIGHT_REVIEW


def test_generated_or_lockfile_skips():
    resp = _resp({"is_generated_or_lockfile": 0.9})
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.SKIP
    assert decision.reason == "generated-or-lockfile"


def test_needs_wider_context_forces_review_even_if_harmless():
    resp = _resp({
        "needs_wider_context": 0.8,
        "risk_severity": {"value": 0.0, "confidence": 0.99},
        "risk_category": {"value": "style-only", "confidence": 0.99},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "needs-wider-context"


def test_context_truncated_forces_review_even_without_the_noul_flag():
    resp = _resp({
        "risk_severity": {"value": 0.0, "confidence": 0.99},
        "risk_category": {"value": "style-only", "confidence": 0.99},
    })
    decision = route_hunk(_hunk(context_truncated=True), resp)
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "needs-wider-context"


def test_low_confidence_never_skips_even_if_category_looks_harmless():
    resp = _resp({
        "risk_category": {"value": "style-only", "confidence": 0.5},
        "risk_severity": {"value": 0.0, "confidence": 0.99},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "low-confidence"


def test_sensitive_input_with_high_severity_goes_deep():
    resp = _resp({
        "touches_security_sensitive_input": 0.9,
        "risk_severity": {"value": 0.9, "confidence": 0.95},
        "risk_category": {"value": "security", "confidence": 0.95},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.DEEP_REVIEW
    assert decision.reason == "sensitive-input-and-high-severity"


def test_sensitive_input_with_low_severity_still_gets_light_review_not_skipped():
    resp = _resp({
        "touches_security_sensitive_input": 0.9,
        "risk_severity": {"value": 0.0, "confidence": 0.95},
        "risk_category": {"value": "security", "confidence": 0.95},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "sensitive-input"


def test_confident_low_severity_skips():
    resp = _resp({
        "risk_severity": {"value": 0.0, "confidence": 0.95},
        "risk_category": {"value": "style-only", "confidence": 0.95},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.SKIP
    assert decision.reason == "low-severity-confident"


def test_confident_high_severity_goes_deep_without_sensitive_input():
    resp = _resp({
        "risk_severity": {"value": 0.9, "confidence": 0.95},
        "risk_category": {"value": "logic", "confidence": 0.95},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.DEEP_REVIEW
    assert decision.reason == "high-severity"


def test_confident_moderate_severity_gets_light_review():
    resp = _resp({
        "risk_severity": {"value": 0.4, "confidence": 0.95},
        "risk_category": {"value": "logic", "confidence": 0.95},
    })
    decision = route_hunk(_hunk(), resp)
    assert decision.route == Route.LIGHT_REVIEW
    assert decision.reason == "moderate-severity"
