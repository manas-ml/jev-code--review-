"""Thin client for the OpenRouter System One (Jev) decisions API
(design-doc.md §5).

Every field-name/shape assumption not confirmed against primary docs
(openrouter.ai and docs.typesafe.ai were unreachable from the session
that wrote this — see design-doc.md "Unverified assumptions") is
isolated inside `parse_jev_response`, so a wrong guess is a one-function
fix rather than a hunt through routing.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jev_review.questions import Question, QuestionType
from jev_review.thresholds import (
    CONNECT_TIMEOUT_S,
    JEV_MODEL_ID,
    MAX_RETRIES,
    RETRY_BASE_DELAYS_S,
    RETRY_JITTER_FRACTION,
    SYSTEMONE_ENDPOINT,
    TOTAL_TIMEOUT_S,
)


class JevError(Exception):
    """Raised for any Jev call that could not produce a usable answer.
    Per design-doc.md's fail-safe rule, callers must treat this as
    'route to review,' never as 'no risk found.'"""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ReplayCacheMiss(Exception):
    """Raised by ReplayJevBackend when a request isn't in the cache.
    Distinct from JevError: a replay miss means the eval setup changed
    (different hunk/question hash), not that the model failed."""


# --- Answer types --------------------------------------------------------


@dataclass(frozen=True)
class ChoiceAnswer:
    question_id: str
    value: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class ScoreAnswer:
    question_id: str
    value: float  # normalized 0-1
    raw_level_index: int
    level_probabilities: list[float]
    confidence: float


@dataclass(frozen=True)
class NoulAnswer:
    question_id: str
    probability: float


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass
class JevResponse:
    answers: dict[str, Answer]
    raw: dict
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def choice(self, question_id: str) -> ChoiceAnswer:
        a = self.answers[question_id]
        assert isinstance(a, ChoiceAnswer)
        return a

    def score(self, question_id: str) -> ScoreAnswer:
        a = self.answers[question_id]
        assert isinstance(a, ScoreAnswer)
        return a

    def noul(self, question_id: str) -> NoulAnswer:
        a = self.answers[question_id]
        assert isinstance(a, NoulAnswer)
        return a


# --- Request building -----------------------------------------------------


def build_request(state: str, questions: dict[str, Question]) -> dict:
    q_body = {}
    for qid, q in questions.items():
        entry: dict = {"type": q.type.value, "instructions": q.instructions}
        if q.type == QuestionType.CHOICE:
            entry["criteria"] = dict(q.criteria)
        elif q.type == QuestionType.SCORE:
            entry["levels"] = list(q.levels)
        q_body[qid] = entry
    return {"model": JEV_MODEL_ID, "state": state, "questions": q_body}


def request_hash(state: str, questions: dict[str, Question]) -> str:
    """Cache key: model + question schema + state, so a change to
    either invalidates cached answers (design-doc.md §5 record/replay)."""
    payload = json.dumps(build_request(state, questions), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Response parsing (UNVERIFIED SHAPE — isolated here on purpose) -------


def parse_jev_response(raw: dict, questions: dict[str, Question]) -> JevResponse:
    """Turn the raw System One JSON into typed Answer objects.

    UNVERIFIED against primary docs (see design-doc.md). Assumed shape,
    per question id, under raw["answers"][qid]:
      choice: {"value": str, "probabilities": {opt: float}, "confidence": float}
      score:  {"value": float 0-1 OR "level_index": int, "probabilities": [float,...], "confidence": float}
      noul:   {"probability": float}
    If this doesn't match the real API, this is the only function that
    needs to change.
    """
    answers_raw = raw.get("answers", {})
    answers: dict[str, Answer] = {}
    for qid, q in questions.items():
        a = answers_raw.get(qid)
        if a is None:
            raise JevError(f"missing answer for question '{qid}' in Jev response")
        try:
            if q.type == QuestionType.CHOICE:
                answers[qid] = ChoiceAnswer(
                    question_id=qid,
                    value=a["value"],
                    probabilities=dict(a.get("probabilities", {})),
                    confidence=float(a.get("confidence", 0.0)),
                )
            elif q.type == QuestionType.SCORE:
                n_levels = max(len(q.levels), 1)
                if "value" in a:
                    value = float(a["value"])
                    raw_index = round(value * (n_levels - 1)) if n_levels > 1 else 0
                else:
                    raw_index = int(a.get("level_index", 0))
                    value = raw_index / (n_levels - 1) if n_levels > 1 else 0.0
                answers[qid] = ScoreAnswer(
                    question_id=qid,
                    value=value,
                    raw_level_index=raw_index,
                    level_probabilities=list(a.get("probabilities", [])),
                    confidence=float(a.get("confidence", 0.0)),
                )
            else:  # NOUL
                answers[qid] = NoulAnswer(
                    question_id=qid,
                    probability=float(a["probability"]),
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise JevError(f"unparseable answer for question '{qid}': {exc}") from exc

    usage = raw.get("usage", {})
    return JevResponse(
        answers=answers,
        raw=raw,
        latency_ms=float(raw.get("latency_ms", 0.0)),
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        cost=float(usage.get("cost", 0.0)),
    )


# --- Backend protocol ------------------------------------------------------


class JevBackend(Protocol):
    def ask(self, state: str, questions: dict[str, Question], *, hunk_id: str | None = None) -> JevResponse: ...


# --- Real network backend --------------------------------------------------


class RealJevBackend:
    """Calls the live System One endpoint. Retries only on 429/5xx, up
    to MAX_RETRIES, exponential backoff with jitter (design-doc.md §5).
    A non-retryable failure (bad request, unparseable response, timeout
    after retries) raises JevError — callers must fail safe to review,
    never treat this as 'no risk.'"""

    def __init__(self, api_key: str | None = None, endpoint: str = SYSTEMONE_ENDPOINT):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.endpoint = endpoint

    def ask(self, state: str, questions: dict[str, Question], *, hunk_id: str | None = None) -> JevResponse:
        if not self.api_key:
            raise JevError("OPENROUTER_API_KEY is not set", retryable=False)

        body = json.dumps(build_request(state, questions)).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=TOTAL_TIMEOUT_S) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    parsed = parse_jev_response(raw, questions)
                    parsed.latency_ms = parsed.latency_ms or (time.monotonic() - start) * 1000
                    return parsed
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    last_exc = JevError(f"Jev HTTP {e.code}", retryable=True, status=e.code)
                    if attempt < MAX_RETRIES:
                        self._sleep(attempt)
                        continue
                    raise last_exc
                raise JevError(f"Jev HTTP {e.code}: {e.reason}", retryable=False, status=e.code) from e
            except urllib.error.URLError as e:
                raise JevError(f"Jev connection error: {e.reason}", retryable=False) from e
            except (json.JSONDecodeError, TimeoutError) as e:
                raise JevError(f"Jev call failed: {e}", retryable=False) from e
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _sleep(attempt: int) -> None:
        base = RETRY_BASE_DELAYS_S[min(attempt, len(RETRY_BASE_DELAYS_S) - 1)]
        jitter = base * RETRY_JITTER_FRACTION * (2 * random.random() - 1)
        time.sleep(max(0.0, base + jitter))


# --- Record/replay cache ----------------------------------------------------


class RecordingJevBackend:
    """Wraps another backend and appends every successful raw response
    to a JSONL cache keyed by request_hash, so a later --replay run can
    re-score routing for free (design-doc.md §5)."""

    def __init__(self, inner: JevBackend, cache_path: str | Path):
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def ask(self, state: str, questions: dict[str, Question], *, hunk_id: str | None = None) -> JevResponse:
        result = self.inner.ask(state, questions, hunk_id=hunk_id)
        record = {"request_hash": request_hash(state, questions), "hunk_id": hunk_id, "raw": result.raw}
        with self.cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return result


class ReplayJevBackend:
    """Reads a JSONL cache written by RecordingJevBackend and answers
    from it with zero network calls. Raises ReplayCacheMiss (not
    JevError) on a miss, since a miss means the eval inputs changed
    since the recorded run, not a model failure."""

    def __init__(self, cache_path: str | Path, questions: dict[str, Question]):
        self.cache: dict[str, dict] = {}
        self.questions = questions
        path = Path(cache_path)
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self.cache[rec["request_hash"]] = rec["raw"]

    def ask(self, state: str, questions: dict[str, Question], *, hunk_id: str | None = None) -> JevResponse:
        key = request_hash(state, questions)
        raw = self.cache.get(key)
        if raw is None:
            raise ReplayCacheMiss(f"no cached Jev response for hunk_id={hunk_id!r} (hash={key[:12]}...)")
        return parse_jev_response(raw, questions)
