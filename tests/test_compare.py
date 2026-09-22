import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval.compare import comment_matches_bug, run_comparison
from eval.eval import Label, load_samples
from jev_review.llm_client import ReviewComment


def _comment(line, hunk_id="h1"):
    return ReviewComment(file="f.py", line=line, side="RIGHT", body="x", severity="moderate", hunk_id=hunk_id, route="light-review")


def test_matches_within_window():
    label = Label("d", "logic", "light-review", bug_lines=[10], clean=False)
    assert comment_matches_bug(_comment(12), label, set())  # within default window (5)
    assert comment_matches_bug(_comment(15), label, set())  # exactly at the edge
    assert not comment_matches_bug(_comment(16), label, set())


def test_clean_sample_never_matches():
    label = Label("d", "style-only", "skip", bug_lines=[], clean=True)
    assert not comment_matches_bug(_comment(1), label, set())


def test_deletion_only_matches_by_hunk_id_not_line():
    label = Label("d", "resource", "light-review", bug_lines=[], clean=False, deletion_only=True)
    bug_hunk_ids = {"the-hunk"}
    assert comment_matches_bug(_comment(9999, hunk_id="the-hunk"), label, bug_hunk_ids)
    assert not comment_matches_bug(_comment(1, hunk_id="other-hunk"), label, bug_hunk_ids)


def test_run_comparison_mock_zero_missed_bugs_both_arms():
    samples = load_samples(REPO_ROOT / "samples")
    data = run_comparison(samples, mock=True, record_dir=None)
    assert data["runs_per_arm"] == len(data["arm_a_runs"]) == len(data["arm_b_runs"])
    for run in data["arm_a_runs"] + data["arm_b_runs"]:
        assert run["bugs_missed"] == 0
    # Arm A must never review MORE hunks than Arm B (Arm B reviews everything).
    for a, b in zip(data["arm_a_runs"], data["arm_b_runs"]):
        assert a["hunks_reviewed"] <= b["hunks_reviewed"]


def test_cli_mock_produces_reports(tmp_path):
    out_dir = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, "eval/compare.py", "--mock", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    jsons = list(out_dir.glob("compare_*.json"))
    reports = list(out_dir.glob("report_*.md"))
    summaries = list(out_dir.glob("summary_*.md"))
    assert len(jsons) == len(reports) == len(summaries) == 1
    data = json.loads(jsons[0].read_text())
    assert data["mock"] is True
    summary_text = summaries[0].read_text()
    assert "MOCK DEMO" in summary_text
    assert summary_text.count("\n") < 25  # quick summary stays short (design-doc.md §9)
