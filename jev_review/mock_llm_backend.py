"""Scriptable mock review-LLM backend, mirroring mock_backend.py's
design for Jev. Used for offline pipeline tests and for compare.py's
oracle-scripted demo run — never for judging real review quality."""

from __future__ import annotations

from typing import Literal

from jev_review.hunk_model import Hunk
from jev_review.jev_client import JevError
from jev_review.llm_client import LLMResponse, ReviewComment
from jev_review.routing import Route

Failure = Literal["error"]
CommentSpec = dict  # {"line": int, "side": "RIGHT"|"LEFT", "body": str, "severity": str}
ScriptEntry = list[CommentSpec] | Failure


class MockLLMBackend:
    def __init__(self, script: dict[str, ScriptEntry] | None = None):
        """`script` maps hunk_id -> either "error" or a list of comment
        dicts to return. A hunk_id not in `script` gets zero comments
        (the harmless default: 'no issues found')."""
        self.script = script or {}
        self.calls: list[str] = []

    def review(self, hunk: Hunk, route: Route, *, jev_hint_category: str | None = None) -> LLMResponse:
        self.calls.append(hunk.hunk_id)
        entry = self.script.get(hunk.hunk_id)

        if entry == "error":
            raise JevError(f"mock LLM error injected for hunk_id={hunk.hunk_id!r}", retryable=False)

        specs = entry if isinstance(entry, list) else []
        comments = [
            ReviewComment(
                file=hunk.file_path,
                line=int(spec["line"]),
                side=spec.get("side", "RIGHT"),
                body=spec["body"],
                severity=spec.get("severity", "moderate"),
                hunk_id=hunk.hunk_id,
                route=route.value,
            )
            for spec in specs
        ]
        prompt_len = len(hunk.render_state(llm=True))
        return LLMResponse(
            comments=comments,
            raw_content="[]" if not comments else "scripted",
            tokens_in=prompt_len // 4,
            tokens_out=20 * len(comments),
            cost=0.0,
            latency_ms=5.0,
        )
