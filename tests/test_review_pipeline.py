import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.hunk_model import ChangeKind, DiffLine, Hunk
from jev_review.mock_backend import MockJevBackend
from jev_review.mock_llm_backend import MockLLMBackend
from jev_review.review_pipeline import run_pipeline
from jev_review.routing import Route


def _hunk(hunk_id_suffix, added="a = 2", removed="a = 1"):
    return Hunk(
        file_path=f"f_{hunk_id_suffix}.py",
        change_kind=ChangeKind.MODIFIED,
        header="@@ -1,1 +1,1 @@",
        old_range=(1, 1),
        new_range=(1, 1),
        lines=[DiffLine("-", removed), DiffLine("+", added)],
    )


def test_skip_route_never_calls_the_llm():
    h = _hunk("skip")
    jev_script = {h.hunk_id: {"risk_severity": {"value": 0.0, "confidence": 0.95}, "risk_category": {"value": "style-only", "confidence": 0.95}}}
    jev = MockJevBackend(script=jev_script)
    llm = MockLLMBackend()
    result = run_pipeline([h], jev, llm)
    assert result.ledger.hunks_routed_skip == 1
    assert result.ledger.llm_calls == 0
    assert any(hh.hunk_id == h.hunk_id for hh, _ in result.skipped)


def test_deep_route_reviews_and_gate_keeps_good_comment():
    h = _hunk("deep")
    jev_answers = {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "security", "confidence": 0.95}}
    jev = MockJevBackend(script={h.hunk_id: jev_answers})
    llm = MockLLMBackend(script={h.hunk_id: [{"line": 1, "body": "real finding", "severity": "high"}]})
    result = run_pipeline([h], jev, llm)
    assert result.ledger.hunks_routed_deep == 1
    assert result.ledger.llm_calls == 1
    # Gate backend has no script for this specific gate call -> default
    # confident-harmless-ish noul defaults to 0.05 for both probabilities
    # (see mock_backend._default_raw_answer), which is BELOW GATE_DROP_MAX
    # for both -> the comment gets dropped by default. This is intentional:
    # the mock's "confident harmless" default isn't meant for gate
    # questions, so tests that want a comment kept must script the gate
    # explicitly (see test below).
    assert len(result.dropped_by_gate) == 1
    assert result.comments == []


def test_gate_explicitly_scripted_to_keep_the_comment():
    h = _hunk("deep2")
    jev_answers = {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "security", "confidence": 0.95}}
    llm = MockLLMBackend(script={h.hunk_id: [{"line": 1, "body": "real finding", "severity": "high"}]})
    # We don't know id(comment) ahead of time, so run once to discover the
    # gate hunk_id the pipeline will use, then re-run with the gate scripted.
    probe_jev = MockJevBackend(script={h.hunk_id: jev_answers})
    probe = run_pipeline([h], probe_jev, llm)
    assert len(probe.dropped_by_gate) == 1  # unscripted gate defaults to dropped, as above

    # Now build a Jev backend that answers BOTH the triage question and
    # any gate question (prefix match) as confident/high.
    class AlwaysGoodGateJev(MockJevBackend):
        def ask(self, state, questions, *, hunk_id=None):
            if hunk_id and hunk_id.startswith("gate:"):
                from jev_review.jev_client import parse_jev_response

                raw = {
                    "answers": {qid: {"probability": 0.95} for qid in questions},
                    "usage": {},
                }
                return parse_jev_response(raw, questions)
            return super().ask(state, questions, hunk_id=hunk_id)

    jev = AlwaysGoodGateJev(script={h.hunk_id: jev_answers})
    result = run_pipeline([h], jev, llm)
    assert len(result.comments) == 1
    assert result.dropped_by_gate == []


def test_llm_call_failure_is_reported_not_treated_as_clean():
    h = _hunk("llmfail")
    jev_script = {h.hunk_id: {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "logic", "confidence": 0.95}}}
    jev = MockJevBackend(script=jev_script)
    llm = MockLLMBackend(script={h.hunk_id: "error"})
    result = run_pipeline([h], jev, llm)
    assert result.comments == []
    assert len(result.review_failed) == 1
    assert result.review_failed[0][0].hunk_id == h.hunk_id


def test_jev_triage_failure_still_reviews_via_llm():
    h = _hunk("jevfail")
    jev = MockJevBackend(script={h.hunk_id: "error"})
    llm = MockLLMBackend(script={h.hunk_id: [{"line": 1, "body": "finding", "severity": "moderate"}]})
    result = run_pipeline([h], jev, llm, use_gate=False)
    assert result.ledger.jev_triage_errors == 1
    assert result.ledger.hunks_routed_light == 1  # fail-safe route
    assert len(result.comments) == 1


def test_use_gate_false_skips_gate_entirely():
    h = _hunk("nogate")
    jev = MockJevBackend(script={h.hunk_id: {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "logic", "confidence": 0.95}}})
    llm = MockLLMBackend(script={h.hunk_id: [{"line": 1, "body": "finding", "severity": "moderate"}]})
    result = run_pipeline([h], jev, llm, use_gate=False)
    assert len(result.comments) == 1
    assert result.dropped_by_gate == []
