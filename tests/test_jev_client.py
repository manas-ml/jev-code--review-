import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jev_review.jev_client import (
    JevError,
    RealJevBackend,
    RecordingJevBackend,
    ReplayCacheMiss,
    ReplayJevBackend,
    parse_jev_response,
    request_hash,
)
from jev_review.mock_backend import MockJevBackend
from jev_review.questions import TRIAGE_QUESTIONS

STATE = "FILE: f.py\nDIFF:\n@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n"


def test_parse_jev_response_all_three_types():
    raw = {
        "answers": {
            "risk_category": {"value": "logic", "probabilities": {"logic": 0.8}, "confidence": 0.8},
            "risk_severity": {"value": 0.6, "confidence": 0.7},
            "touches_security_sensitive_input": {"probability": 0.2},
            "is_generated_or_lockfile": {"probability": 0.01},
            "needs_wider_context": {"probability": 0.1},
        },
        "usage": {"input_tokens": 120, "output_tokens": 0, "cost": 0.005},
    }
    resp = parse_jev_response(raw, TRIAGE_QUESTIONS)
    assert resp.choice("risk_category").value == "logic"
    assert resp.score("risk_severity").value == 0.6
    assert resp.noul("touches_security_sensitive_input").probability == 0.2
    assert resp.tokens_in == 120
    assert resp.cost == 0.005


def test_parse_jev_response_missing_answer_raises_jev_error():
    raw = {"answers": {}, "usage": {}}
    with pytest.raises(JevError):
        parse_jev_response(raw, TRIAGE_QUESTIONS)


def test_parse_jev_response_malformed_answer_raises_jev_error():
    raw = {"answers": {qid: {} for qid in TRIAGE_QUESTIONS}, "usage": {}}
    # noul question has no "probability" key -> KeyError inside parse
    with pytest.raises(JevError):
        parse_jev_response(raw, TRIAGE_QUESTIONS)


def test_request_hash_stable_and_sensitive_to_state():
    h1 = request_hash(STATE, TRIAGE_QUESTIONS)
    h2 = request_hash(STATE, TRIAGE_QUESTIONS)
    h3 = request_hash(STATE + "\n", TRIAGE_QUESTIONS)
    assert h1 == h2
    assert h1 != h3


def test_record_then_replay_round_trip(tmp_path):
    cache_path = tmp_path / "cache.jsonl"
    mock = MockJevBackend(script={"h1": {"risk_category": {"value": "logic", "confidence": 0.9}}})
    recorder = RecordingJevBackend(mock, cache_path)
    original = recorder.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")

    replay = ReplayJevBackend(cache_path, TRIAGE_QUESTIONS)
    replayed = replay.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")

    assert replayed.choice("risk_category").value == original.choice("risk_category").value
    assert replayed.score("risk_severity").value == original.score("risk_severity").value


def test_replay_cache_miss_raises_distinct_error(tmp_path):
    cache_path = tmp_path / "empty.jsonl"
    replay = ReplayJevBackend(cache_path, TRIAGE_QUESTIONS)
    with pytest.raises(ReplayCacheMiss):
        replay.ask(STATE, TRIAGE_QUESTIONS, hunk_id="missing")


def test_real_backend_no_api_key_raises_jev_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    backend = RealJevBackend()
    with pytest.raises(JevError):
        backend.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")


def test_real_backend_retries_on_429_then_succeeds(monkeypatch):
    backend = RealJevBackend(api_key="test-key")
    calls = {"n": 0}
    success_raw = {
        "answers": {
            "risk_category": {"value": "logic", "confidence": 0.9},
            "risk_severity": {"value": 0.5, "confidence": 0.9},
            "touches_security_sensitive_input": {"probability": 0.1},
            "is_generated_or_lockfile": {"probability": 0.0},
            "needs_wider_context": {"probability": 0.0},
        },
        "usage": {},
    }

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(success_raw).encode("utf-8"))

    monkeypatch.setattr("jev_review.jev_client.urllib.request.urlopen", lambda req, timeout: fake_urlopen(req, timeout))
    monkeypatch.setattr("jev_review.jev_client.time.sleep", lambda s: None)

    resp = backend.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")
    assert calls["n"] == 2
    assert resp.choice("risk_category").value == "logic"


def test_real_backend_does_not_retry_on_400(monkeypatch):
    backend = RealJevBackend(api_key="test-key")
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b""))

    monkeypatch.setattr("jev_review.jev_client.urllib.request.urlopen", lambda req, timeout: fake_urlopen(req, timeout))

    with pytest.raises(JevError):
        backend.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")
    assert calls["n"] == 1


def test_real_backend_gives_up_after_max_retries(monkeypatch):
    backend = RealJevBackend(api_key="test-key")
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))

    monkeypatch.setattr("jev_review.jev_client.urllib.request.urlopen", lambda req, timeout: fake_urlopen(req, timeout))
    monkeypatch.setattr("jev_review.jev_client.time.sleep", lambda s: None)

    with pytest.raises(JevError):
        backend.ask(STATE, TRIAGE_QUESTIONS, hunk_id="h1")
    assert calls["n"] == 4  # initial + MAX_RETRIES(3)


def test_mock_backend_scripted_error_and_default_confident_harmless():
    mock = MockJevBackend(script={"bad": "error"})
    with pytest.raises(JevError):
        mock.ask(STATE, TRIAGE_QUESTIONS, hunk_id="bad")

    resp = mock.ask(STATE, TRIAGE_QUESTIONS, hunk_id="unscripted")
    assert resp.score("risk_severity").value <= 0.15
    assert resp.choice("risk_category").confidence >= 0.85
