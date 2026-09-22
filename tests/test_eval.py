import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval.eval import (
    Ledger,
    _bug_hunk_ids,
    aggregate,
    build_oracle_script,
    evaluate_sample,
    load_samples,
    prepare_hunks,
)
from jev_review.mock_backend import MockJevBackend
from jev_review.routing import Route


def test_samples_load_and_have_required_label_fields():
    samples = load_samples(REPO_ROOT / "samples")
    assert len(samples) >= 10
    for s in samples:
        assert s.label.expected_min_route in ("skip", "light-review", "deep-review")
        assert isinstance(s.label.bug_lines, list)
        if not s.label.clean and not s.label.deletion_only:
            assert s.label.bug_lines, f"{s.name}: non-clean, non-deletion sample must have bug_lines"


def test_heldout_samples_load():
    samples = load_samples(REPO_ROOT / "samples_heldout")
    assert len(samples) >= 2


def test_oracle_mock_gets_zero_missed_bugs_on_tuning_set():
    samples = load_samples(REPO_ROOT / "samples")
    samples_by_name = {s.name: s for s in samples}
    hunks_by_sample = {s.name: prepare_hunks(s) for s in samples}
    script = build_oracle_script(hunks_by_sample, samples_by_name)
    backend = MockJevBackend(script=script)
    ledger = Ledger()
    results = [evaluate_sample(s, hunks_by_sample[s.name], backend, ledger) for s in samples]
    agg = aggregate(results)
    assert agg["bug_hunks_missed"] == 0
    assert agg["bug_hunks_total"] >= 8


def test_jev_call_failure_never_produces_a_missed_bug():
    """Fail-safe check at the eval-harness level: if every Jev call
    errors, review recall must stay at 100% (fail open to review), not
    drop bugs to 'skip'."""
    samples = load_samples(REPO_ROOT / "samples")
    hunks_by_sample = {s.name: prepare_hunks(s) for s in samples}
    always_error = MockJevBackend(script={h.hunk_id: "error" for hs in hunks_by_sample.values() for h in hs})
    ledger = Ledger()
    results = [evaluate_sample(s, hunks_by_sample[s.name], always_error, ledger) for s in samples]
    agg = aggregate(results)
    assert agg["bug_hunks_missed"] == 0
    assert agg["jev_errors"] > 0


def test_deletion_only_bug_is_matched_to_its_hunk():
    samples = load_samples(REPO_ROOT / "samples")
    sample = next(s for s in samples if s.label.deletion_only)
    hunks = prepare_hunks(sample)
    bug_ids = _bug_hunk_ids(hunks, sample.label)
    assert len(bug_ids) >= 1
    assert bug_ids <= {h.hunk_id for h in hunks}


def test_cli_mock_run_exits_zero_and_reports_success(tmp_path):
    out_json = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "eval/eval.py", "--mock", "--json-out", str(out_json)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS CRITERIA MET" in result.stdout
    data = json.loads(out_json.read_text())
    assert data["aggregate"]["bug_hunks_missed"] == 0


def test_cli_live_without_api_key_fails_safe_not_silently(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = subprocess.run(
        [sys.executable, "eval/eval.py", "--live"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    # No API key -> every Jev call errors -> fail-safe routes everything
    # to review -> zero missed bugs, but skip rate stays at 0%, and the
    # report says so instead of pretending the run succeeded cleanly.
    assert "Bug hunks MISSED (routed to skip): 0" in result.stdout
    assert "below the 40% target" in result.stdout
