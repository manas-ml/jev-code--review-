import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.gate import gate_comment, gate_comments
from jev_review.hunk_model import ChangeKind, DiffLine, Hunk
from jev_review.jev_client import JevError
from jev_review.llm_client import ReviewComment
from jev_review.mock_backend import MockJevBackend
from jev_review.routing import Route


def _hunk():
    return Hunk(
        file_path="f.py",
        change_kind=ChangeKind.MODIFIED,
        header="@@ -1,1 +1,1 @@",
        old_range=(1, 1),
        new_range=(1, 1),
        lines=[DiffLine("-", "a = 1"), DiffLine("+", "a = 2")],
    )


def _comment(hunk):
    return ReviewComment(file=hunk.file_path, line=1, side="RIGHT", body="looks off", severity="moderate", hunk_id=hunk.hunk_id, route=Route.LIGHT_REVIEW.value)


def test_comment_kept_when_both_probabilities_high():
    hunk = _hunk()
    comment = _comment(hunk)
    gate_id = f"gate:{hunk.hunk_id}:{id(comment)}"
    backend = MockJevBackend(script={gate_id: {"is_actionable": 0.9, "problem_in_shown_code": 0.9}})
    result = gate_comment(comment, hunk, backend)
    assert result.kept
    assert not result.ungated


def test_comment_dropped_only_when_both_probabilities_low():
    hunk = _hunk()
    comment = _comment(hunk)
    gate_id = f"gate:{hunk.hunk_id}:{id(comment)}"
    backend = MockJevBackend(script={gate_id: {"is_actionable": 0.2, "problem_in_shown_code": 0.2}})
    result = gate_comment(comment, hunk, backend)
    assert not result.kept
    assert result.reason == "both-below-threshold"


def test_comment_kept_when_only_one_probability_low():
    hunk = _hunk()
    comment = _comment(hunk)
    gate_id = f"gate:{hunk.hunk_id}:{id(comment)}"
    # actionable low, but the problem IS in the shown code -> keep (biased loose)
    backend = MockJevBackend(script={gate_id: {"is_actionable": 0.1, "problem_in_shown_code": 0.9}})
    result = gate_comment(comment, hunk, backend)
    assert result.kept


class _FailingJevBackend:
    def ask(self, state, questions, *, hunk_id=None):
        raise JevError("boom")


def test_gate_call_failure_keeps_comment_marked_ungated():
    hunk = _hunk()
    comment = _comment(hunk)
    result = gate_comment(comment, hunk, _FailingJevBackend())
    assert result.kept
    assert result.ungated


def test_gate_comments_logs_only_dropped(tmp_path):
    hunk = _hunk()
    kept_comment = _comment(hunk)
    dropped_comment = ReviewComment(file=hunk.file_path, line=1, side="RIGHT", body="noise", severity="low", hunk_id=hunk.hunk_id, route="light-review")
    script = {
        f"gate:{hunk.hunk_id}:{id(kept_comment)}": {"is_actionable": 0.9, "problem_in_shown_code": 0.9},
        f"gate:{hunk.hunk_id}:{id(dropped_comment)}": {"is_actionable": 0.1, "problem_in_shown_code": 0.1},
    }
    backend = MockJevBackend(script=script)
    log_path = tmp_path / "dropped.jsonl"
    results = gate_comments([kept_comment, dropped_comment], hunk, backend, dropped_log_path=log_path)
    assert sum(r.kept for r in results) == 1
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert "noise" in lines[0]
