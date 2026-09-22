import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jev_review.hunk_model import ChangeKind, DiffLine, Hunk, attach_enclosing_context
from jev_review.jev_client import JevError
from jev_review.llm_client import parse_llm_content, render_review_prompt
from jev_review.mock_llm_backend import MockLLMBackend
from jev_review.routing import Route


def _hunk_in_function():
    post_source = (
        "def f(x):\n"
        "    y = x + 1\n"
        "    return y\n"
    )
    old = "def f(x):\n    y = x\n    return y\n"
    h = Hunk(
        file_path="f.py",
        change_kind=ChangeKind.MODIFIED,
        header="@@ -1,3 +1,3 @@",
        old_range=(1, 3),
        new_range=(1, 3),
        lines=[DiffLine(" ", "def f(x):"), DiffLine("-", "    y = x"), DiffLine("+", "    y = x + 1"), DiffLine(" ", "    return y")],
    )
    attach_enclosing_context(h, post_source)
    return h


def test_deep_review_prompt_includes_enclosing_context_light_does_not():
    h = _hunk_in_function()
    _, deep_user = render_review_prompt(h, Route.DEEP_REVIEW)
    _, light_user = render_review_prompt(h, Route.LIGHT_REVIEW)
    assert "ENCLOSING FUNCTION" in deep_user
    assert "ENCLOSING FUNCTION" not in light_user


def test_hint_is_framed_as_unverified_not_a_verdict():
    h = _hunk_in_function()
    _, user = render_review_prompt(h, Route.LIGHT_REVIEW, jev_hint_category="logic")
    assert "possibly 'logic'" in user
    assert "verify independently" in user.lower()


def test_parse_llm_content_valid_json_array():
    h = _hunk_in_function()
    content = '[{"line": 2, "side": "RIGHT", "body": "off by one", "severity": "high"}]'
    comments = parse_llm_content(content, h, Route.LIGHT_REVIEW)
    assert len(comments) == 1
    assert comments[0].line == 2
    assert comments[0].severity == "high"


def test_parse_llm_content_strips_markdown_fences():
    h = _hunk_in_function()
    content = '```json\n[{"line": 2, "side": "RIGHT", "body": "x", "severity": "low"}]\n```'
    comments = parse_llm_content(content, h, Route.LIGHT_REVIEW)
    assert len(comments) == 1


def test_parse_llm_content_malformed_returns_empty_not_raises():
    h = _hunk_in_function()
    assert parse_llm_content("not json at all", h, Route.LIGHT_REVIEW) == []
    assert parse_llm_content('{"not": "a list"}', h, Route.LIGHT_REVIEW) == []
    assert parse_llm_content('[{"missing": "required fields"}]', h, Route.LIGHT_REVIEW) == []


def test_parse_llm_content_empty_array_means_no_issues():
    h = _hunk_in_function()
    assert parse_llm_content("[]", h, Route.LIGHT_REVIEW) == []


def test_mock_llm_backend_scripted_comments_and_error():
    h = _hunk_in_function()
    mock = MockLLMBackend(script={h.hunk_id: [{"line": 2, "body": "seeded finding", "severity": "high"}]})
    resp = mock.review(h, Route.DEEP_REVIEW)
    assert len(resp.comments) == 1
    assert resp.comments[0].body == "seeded finding"

    mock_err = MockLLMBackend(script={h.hunk_id: "error"})
    with pytest.raises(JevError):
        mock_err.review(h, Route.DEEP_REVIEW)


def test_mock_llm_backend_default_is_no_comments():
    h = _hunk_in_function()
    mock = MockLLMBackend()
    resp = mock.review(h, Route.LIGHT_REVIEW)
    assert resp.comments == []
