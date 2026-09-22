import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.hunk_model import ChangeKind, DiffLine, Hunk
from jev_review.mock_backend import MockJevBackend
from jev_review.mock_llm_backend import MockLLMBackend
from jev_review.report import write_human_report, write_json_report
from jev_review.review_pipeline import run_pipeline


def _hunks():
    skip = Hunk(
        file_path="clean.py", change_kind=ChangeKind.MODIFIED, header="@@ -1,1 +1,1 @@",
        old_range=(1, 1), new_range=(1, 1), lines=[DiffLine("-", "x=1"), DiffLine("+", "x = 1")],
    )
    bug = Hunk(
        file_path="buggy.py", change_kind=ChangeKind.MODIFIED, header="@@ -1,1 +1,1 @@",
        old_range=(1, 1), new_range=(1, 1), lines=[DiffLine("-", "a = 1"), DiffLine("+", "a = 2")],
    )
    return skip, bug


def test_report_json_and_md_written_with_expected_sections(tmp_path):
    skip_hunk, bug_hunk = _hunks()
    jev = MockJevBackend(script={
        bug_hunk.hunk_id: {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "logic", "confidence": 0.95}},
    })

    class AlwaysPassGate(MockJevBackend):
        def ask(self, state, questions, *, hunk_id=None):
            if hunk_id and hunk_id.startswith("gate:"):
                from jev_review.jev_client import parse_jev_response
                raw = {"answers": {qid: {"probability": 0.95} for qid in questions}, "usage": {}}
                return parse_jev_response(raw, questions)
            return super().ask(state, questions, hunk_id=hunk_id)

    jev = AlwaysPassGate(script={
        bug_hunk.hunk_id: {"risk_severity": {"value": 0.9, "confidence": 0.95}, "risk_category": {"value": "logic", "confidence": 0.95}},
    })
    llm = MockLLMBackend(script={bug_hunk.hunk_id: [{"line": 1, "body": "real bug", "severity": "high"}]})

    result = run_pipeline([skip_hunk, bug_hunk], jev, llm)

    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    write_json_report(result, json_path)
    write_human_report(result, md_path)

    data = json.loads(json_path.read_text())
    assert len(data["comments"]) == 1
    assert data["comments"][0]["file"] == "buggy.py"
    assert any(s["file"] == "clean.py" for s in data["skipped"])

    md = md_path.read_text()
    assert "## Comments" in md
    assert "## Skipped hunks" in md
    assert "buggy.py:1" in md
    assert "clean.py" in md
