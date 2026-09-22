"""Review-stage LLM client (design-doc.md §5's review pipeline +
§3's light/deep prompt difference). Talks to OpenRouter's standard
chat-completions endpoint — an OpenAI-compatible shape that's
well-documented and much lower-risk than Jev's Decisions API, so
unlike jev_client.py this is NOT marked as an unverified assumption.

The one real assumption here: the review LLM is instructed to return a
JSON array of comment objects in its message content, and that's
parsed defensively (markdown-fence stripping, empty-on-malformed) since
models sometimes wrap JSON in prose or fences despite instructions.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from jev_review.hunk_model import Hunk
from jev_review.jev_client import JevError  # reused: same fail-safe error type
from jev_review.routing import Route
from jev_review.thresholds import (
    CHAT_COMPLETIONS_ENDPOINT,
    DEEP_REVIEW_MAX_COMMENTS,
    LIGHT_REVIEW_MAX_COMMENTS,
    MAX_RETRIES,
    REVIEW_LLM_MODEL_ID,
    RETRY_BASE_DELAYS_S,
    RETRY_JITTER_FRACTION,
    TOTAL_TIMEOUT_S,
)

SYSTEM_PROMPT_TEMPLATE = (
    "You are reviewing one hunk of a code change. Report only problems you "
    "can point to directly in the code shown — do not speculate about code "
    "you cannot see. Return a JSON array (and nothing else) of objects "
    'shaped {"line": <int, a line number from the diff shown>, '
    '"side": "RIGHT" or "LEFT" (RIGHT for added/context lines, LEFT for a '
    'removed line whose absence is itself the problem), "body": <string, '
    'a specific actionable comment>, "severity": "low"|"moderate"|"high"|'
    '"critical"}. Return at most $MAX_COMMENTS objects, most important '
    "first. If you find no real issue, return an empty array []."
)


def max_comments_for(route: Route) -> int:
    return DEEP_REVIEW_MAX_COMMENTS if route == Route.DEEP_REVIEW else LIGHT_REVIEW_MAX_COMMENTS


def render_review_prompt(hunk: Hunk, route: Route, jev_hint_category: str | None = None) -> tuple[str, str]:
    """Returns (system_prompt, user_content). Light review omits the
    full enclosing_context; deep review includes it (design-doc.md §3).
    If `jev_hint_category` is given, it's framed as an unverified hint
    to avoid anchoring the LLM on a possibly-wrong triage label
    (design-doc.md §3's anchoring trade-off)."""
    system = SYSTEM_PROMPT_TEMPLATE.replace("$MAX_COMMENTS", str(max_comments_for(route)))
    user = hunk.render_state(llm=True, include_enclosing_context=(route == Route.DEEP_REVIEW))
    if jev_hint_category:
        user = (
            f"(An automated triage step flagged this hunk as possibly '{jev_hint_category}'. "
            "Verify independently — report only issues you can confirm in the code shown, "
            "regardless of whether they match that category.)\n\n" + user
        )
    return system, user


@dataclass(frozen=True)
class ReviewComment:
    file: str
    line: int
    side: str  # "LEFT" | "RIGHT"
    body: str
    severity: str
    hunk_id: str
    route: str  # Route.value at time of generation, for the report


@dataclass
class LLMResponse:
    comments: list[ReviewComment] = field(default_factory=list)
    raw_content: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_content(content: str, hunk: Hunk, route: Route) -> list[ReviewComment]:
    """Defensive JSON parse: strips markdown fences some models add
    despite instructions. Returns [] (not an error) on anything
    unparseable — an LLM formatting slip should not surface as a
    pipeline failure; see review_pipeline.py for how a *call* failure
    (as opposed to a parse slip) is still treated as a real error."""
    cleaned = _FENCE_RE.sub("", content).strip()
    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                ReviewComment(
                    file=hunk.file_path,
                    line=int(item["line"]),
                    side=str(item.get("side", "RIGHT")).upper(),
                    body=str(item["body"]),
                    severity=str(item.get("severity", "moderate")),
                    hunk_id=hunk.hunk_id,
                    route=route.value,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


class LLMBackend(Protocol):
    def review(self, hunk: Hunk, route: Route, *, jev_hint_category: str | None = None) -> LLMResponse: ...


class RealLLMBackend:
    def __init__(self, api_key: str | None = None, endpoint: str = CHAT_COMPLETIONS_ENDPOINT):
        import os

        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.endpoint = endpoint

    def review(self, hunk: Hunk, route: Route, *, jev_hint_category: str | None = None) -> LLMResponse:
        if not self.api_key:
            raise JevError("OPENROUTER_API_KEY is not set", retryable=False)
        system, user = render_review_prompt(hunk, route, jev_hint_category)
        body = json.dumps(
            {
                "model": REVIEW_LLM_MODEL_ID,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=TOTAL_TIMEOUT_S) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    content = raw["choices"][0]["message"]["content"]
                    comments = parse_llm_content(content, hunk, route)
                    usage = raw.get("usage", {})
                    return LLMResponse(
                        comments=comments,
                        raw_content=content,
                        tokens_in=int(usage.get("prompt_tokens", 0)),
                        tokens_out=int(usage.get("completion_tokens", 0)),
                        cost=float(usage.get("cost", 0.0)),
                        latency_ms=(time.monotonic() - start) * 1000,
                    )
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    last_exc = JevError(f"LLM HTTP {e.code}", retryable=True, status=e.code)
                    if attempt < MAX_RETRIES:
                        self._sleep(attempt)
                        continue
                    raise last_exc
                raise JevError(f"LLM HTTP {e.code}: {e.reason}", retryable=False, status=e.code) from e
            except urllib.error.URLError as e:
                raise JevError(f"LLM connection error: {e.reason}", retryable=False) from e
            except (json.JSONDecodeError, KeyError, IndexError, TimeoutError) as e:
                raise JevError(f"LLM call failed: {e}", retryable=False) from e
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _sleep(attempt: int) -> None:
        import random

        base = RETRY_BASE_DELAYS_S[min(attempt, len(RETRY_BASE_DELAYS_S) - 1)]
        jitter = base * RETRY_JITTER_FRACTION * (2 * random.random() - 1)
        time.sleep(max(0.0, base + jitter))
