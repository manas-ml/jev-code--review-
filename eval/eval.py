#!/usr/bin/env python3
"""Triage-only eval (design-doc.md §6). Runs the pre-filter + Jev triage
+ routing pipeline over samples/ (or samples_heldout/) and reports
missed bugs, review recall, skip rate on clean hunks, category
accuracy, and cost/latency — with zero calls to a review-writing LLM.

Usage:
  python eval/eval.py --mock                 # demo run, oracle-scripted mock (see NOTE below)
  python eval/eval.py --replay <cache.jsonl>  # re-score from a previously recorded run, no network
  python eval/eval.py --live                  # real Jev calls, records to a new cache
  python eval/eval.py --mock --heldout        # same, against samples_heldout/

NOTE on --mock: the scripted mock answers are built FROM each sample's
own label (an oracle), so this run proves the harness's plumbing is
wired correctly end to end. It is NOT a measurement of Jev's triage
quality — per design-doc.md §5, mock answers are only for testing
structure. Use --live or --replay for a real evaluation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_review.hunk_model import Hunk, attach_enclosing_context, diff_files, split_oversized_hunk
from jev_review.jev_client import (
    JevBackend,
    JevError,
    RealJevBackend,
    RecordingJevBackend,
    ReplayCacheMiss,
    ReplayJevBackend,
)
from jev_review.mock_backend import MockJevBackend
from jev_review.prefilter import prefilter_hunks
from jev_review.questions import TRIAGE_QUESTIONS
from jev_review.routing import Route, route_hunk

DEFAULT_SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"
HELDOUT_SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples_heldout"


# --- Sample loading ---------------------------------------------------------


@dataclass
class Label:
    description: str
    expected_category: str
    expected_min_route: str
    bug_lines: list[int]
    clean: bool
    deletion_only: bool = False


@dataclass
class Sample:
    name: str  # "<category>/<case>"
    before: str
    after: str
    label: Label


def load_samples(root: Path) -> list[Sample]:
    samples = []
    for label_path in sorted(root.glob("*/*/label.json")):
        case_dir = label_path.parent
        name = f"{case_dir.parent.name}/{case_dir.name}"
        before = (case_dir / "before.py").read_text() if (case_dir / "before.py").exists() else ""
        after = (case_dir / "after.py").read_text() if (case_dir / "after.py").exists() else ""
        label_raw = json.loads(label_path.read_text())
        label = Label(
            description=label_raw["description"],
            expected_category=label_raw["expected_category"],
            expected_min_route=label_raw["expected_min_route"],
            bug_lines=label_raw.get("bug_lines", []),
            clean=label_raw["clean"],
            deletion_only=label_raw.get("deletion_only", False),
        )
        samples.append(Sample(name=name, before=before, after=after, label=label))
    return samples


# --- Baseline: review-everything -------------------------------------------


def baseline_review_everything(hunk: Hunk) -> Route:
    return Route.DEEP_REVIEW


# --- Baseline: regex/keyword heuristic --------------------------------------

_SECURITY_RE = re.compile(
    r"password|secret|api_key|token|f[\"']SELECT|execute\(f|os\.path\.join\(",
    re.IGNORECASE,
)
_CONCURRENCY_RE = re.compile(r"\block\b|\bthread\b|\basync\b", re.IGNORECASE)


def baseline_regex_heuristic(hunk: Hunk) -> Route:
    added_text = "\n".join(hunk.added_lines)
    if _SECURITY_RE.search(added_text):
        return Route.DEEP_REVIEW
    if _CONCURRENCY_RE.search(added_text) or _CONCURRENCY_RE.search("\n".join(hunk.removed_lines)):
        return Route.LIGHT_REVIEW
    if hunk.is_whitespace_only:
        return Route.SKIP
    if all(l.lstrip().startswith("#") or l.lstrip().startswith('"""') for l in hunk.added_lines if l.strip()):
        return Route.SKIP  # comment/docstring-only additions
    return Route.LIGHT_REVIEW  # conservative default, per the fail-safe spirit


# --- Oracle mock script (demo only, see module docstring) -------------------

_CATEGORY_TO_JEV_LABEL = {
    "logic": "logic",
    "security": "security",
    "concurrency": "concurrency",
    "resource": "resource",
    "data-model": "data-model",
    "style-only": "style-only",
}

_ROUTE_TO_SEVERITY = {
    "skip": 0.05,
    "light-review": 0.4,
    "deep-review": 0.85,
}


def build_oracle_script(hunks_by_sample: dict[str, list[Hunk]], samples_by_name: dict[str, Sample]) -> dict:
    script = {}
    for name, hunks in hunks_by_sample.items():
        label = samples_by_name[name].label
        bug_hunk_ids = _bug_hunk_ids(hunks, label)
        for h in hunks:
            is_bug_hunk = h.hunk_id in bug_hunk_ids
            category = label.expected_category if is_bug_hunk else "style-only"
            severity = _ROUTE_TO_SEVERITY[label.expected_min_route] if is_bug_hunk else 0.05
            sensitive = 0.6 if (is_bug_hunk and category == "security") else 0.05
            script[h.hunk_id] = {
                "risk_category": {"value": category, "confidence": 0.95},
                "risk_severity": {"value": severity, "confidence": 0.95},
                "touches_security_sensitive_input": sensitive,
                "is_generated_or_lockfile": 0.0,
                "needs_wider_context": 0.0,
            }
    return script


def _bug_hunk_ids(hunks: list[Hunk], label: Label) -> set[str]:
    if label.clean:
        return set()
    if label.deletion_only:
        return {h.hunk_id for h in hunks}  # single-file sample: any hunk carries the deletion
    ids = set()
    for h in hunks:
        start, count = h.new_range
        end = start + max(count, 1) - 1
        if any(start <= ln <= end for ln in label.bug_lines):
            ids.add(h.hunk_id)
    return ids


# --- Per-sample evaluation ---------------------------------------------------


@dataclass
class SampleResult:
    name: str
    clean: bool
    hunks_total: int
    hunks_prefiltered: int
    bug_hunks: int
    bug_hunks_missed: list[str] = field(default_factory=list)  # routes that missed
    bug_hunks_reviewed: int = 0
    category_correct: int = 0
    category_checked: int = 0
    clean_hunks_skipped: int = 0
    clean_hunks_total: int = 0
    baseline_review_everything_missed: int = 0
    baseline_regex_missed: int = 0
    baseline_regex_clean_skipped: int = 0
    jev_calls: int = 0
    jev_errors: int = 0
    tokens_in: int = 0
    latency_ms: float = 0.0


def prepare_hunks(sample: Sample) -> list[Hunk]:
    """Diff + split + attach-context, run exactly once per sample so
    every consumer (oracle-script building, evaluate_sample) sees the
    same Hunk objects with the same hunk_id — hunk_id is assigned at
    construction, so re-diffing would mint new, mismatched ids."""
    hunks = diff_files(sample.name + "/before.py", sample.name + "/after.py", sample.before, sample.after)
    split_hunks: list[Hunk] = []
    for h in hunks:
        split_hunks.extend(split_oversized_hunk(h))
    if sample.after:
        for h in split_hunks:
            attach_enclosing_context(h, sample.after)
    return split_hunks


def evaluate_sample(sample: Sample, split_hunks: list[Hunk], backend: JevBackend, budget: "Ledger") -> SampleResult:
    kept, dropped = prefilter_hunks(split_hunks)

    result = SampleResult(
        name=sample.name,
        clean=sample.label.clean,
        hunks_total=len(split_hunks),
        hunks_prefiltered=len(dropped),
        bug_hunks=0,
    )

    bug_ids = _bug_hunk_ids(split_hunks, sample.label)
    result.bug_hunks = len(bug_ids)

    # The pre-filter is shared, free infrastructure (design-doc.md §0) —
    # both Jev triage and the baselines below start from its output, so
    # a hunk it drops is scored as "skipped" for all three.
    for h, reason in dropped:
        if h.hunk_id in bug_ids:
            result.bug_hunks_missed.append(f"{h.hunk_id}:prefiltered:{reason}")
            result.baseline_review_everything_missed += 1
            result.baseline_regex_missed += 1
        elif sample.label.clean:
            result.clean_hunks_total += 1
            result.clean_hunks_skipped += 1
            result.baseline_regex_clean_skipped += 1

    for h in kept:
        state = h.render_state()
        budget.hunks_seen += 1
        try:
            resp = backend.ask(state, TRIAGE_QUESTIONS, hunk_id=h.hunk_id)
            result.jev_calls += 1
            result.tokens_in += resp.tokens_in
            result.latency_ms += resp.latency_ms
            decision = route_hunk(h, resp)
            if h.hunk_id in bug_ids:
                result.category_checked += 1
                if resp.choice("risk_category").value == _CATEGORY_TO_JEV_LABEL.get(
                    sample.label.expected_category, sample.label.expected_category
                ):
                    result.category_correct += 1
        except (JevError, ReplayCacheMiss):
            result.jev_errors += 1
            decision = route_hunk(h, None)  # fail-safe: None -> light-review

        if h.hunk_id in bug_ids:
            if decision.route == Route.SKIP:
                result.bug_hunks_missed.append(f"{h.hunk_id}:{decision.reason}")
            else:
                result.bug_hunks_reviewed += 1
        elif sample.label.clean:
            result.clean_hunks_total += 1
            if decision.route == Route.SKIP:
                result.clean_hunks_skipped += 1

        _score_baselines(h, sample, bug_ids, result)

    return result


def _score_baselines(h: Hunk, sample: Sample, bug_ids: set[str], result: SampleResult) -> None:
    """Score both baselines for a hunk that survived the (shared, free)
    pre-filter. review-everything never skips here by construction — it
    only "misses" a bug hunk the pre-filter itself dropped (handled in
    evaluate_sample's dropped-hunk loop)."""
    reg_route = baseline_regex_heuristic(h)
    if h.hunk_id in bug_ids and reg_route == Route.SKIP:
        result.baseline_regex_missed += 1
    if sample.label.clean and reg_route == Route.SKIP:
        result.baseline_regex_clean_skipped += 1


@dataclass
class Ledger:
    hunks_seen: int = 0


# --- Aggregate report --------------------------------------------------------


def aggregate(results: list[SampleResult]) -> dict:
    total_bug_hunks = sum(r.bug_hunks for r in results)
    total_missed = sum(len(r.bug_hunks_missed) for r in results)
    total_reviewed_bugs = sum(r.bug_hunks_reviewed for r in results)
    total_clean_hunks = sum(r.clean_hunks_total for r in results)
    total_clean_skipped = sum(r.clean_hunks_skipped for r in results)
    total_cat_checked = sum(r.category_checked for r in results)
    total_cat_correct = sum(r.category_correct for r in results)
    baseline_re_missed = sum(r.baseline_review_everything_missed for r in results)
    baseline_regex_missed = sum(r.baseline_regex_missed for r in results)
    baseline_regex_clean_skipped = sum(r.baseline_regex_clean_skipped for r in results)

    return {
        "samples": len(results),
        "bug_hunks_total": total_bug_hunks,
        "bug_hunks_missed": total_missed,
        "missed_detail": [d for r in results for d in r.bug_hunks_missed],
        "review_recall": (total_reviewed_bugs / total_bug_hunks) if total_bug_hunks else None,
        "clean_hunks_total": total_clean_hunks,
        "clean_hunks_skipped": total_clean_skipped,
        "skip_rate_on_clean": (total_clean_skipped / total_clean_hunks) if total_clean_hunks else None,
        "category_accuracy": (total_cat_correct / total_cat_checked) if total_cat_checked else None,
        "jev_calls": sum(r.jev_calls for r in results),
        "jev_errors": sum(r.jev_errors for r in results),
        "tokens_in": sum(r.tokens_in for r in results),
        "latency_ms_total": sum(r.latency_ms for r in results),
        "baseline_review_everything_missed_bugs": baseline_re_missed,
        "baseline_regex_missed_bugs": baseline_regex_missed,
        "baseline_regex_skip_rate_on_clean": (
            baseline_regex_clean_skipped / total_clean_hunks if total_clean_hunks else None
        ),
    }


def print_report(agg: dict, results: list[SampleResult], mock: bool) -> None:
    if mock:
        print(
            "*** MOCK RUN: oracle-scripted answers, structural demo only — "
            "NOT a measurement of Jev's real triage quality. ***\n"
        )
    print(f"Samples evaluated: {agg['samples']}")
    print(f"Bug hunks total:   {agg['bug_hunks_total']}")
    print(f"Bug hunks MISSED (routed to skip): {agg['bug_hunks_missed']}  <- target 0")
    for d in agg["missed_detail"]:
        print(f"    MISSED: {d}")
    recall = agg["review_recall"]
    print(f"Review recall: {recall:.0%}" if recall is not None else "Review recall: n/a")
    skip_rate = agg["skip_rate_on_clean"]
    print(f"Skip rate on clean hunks: {skip_rate:.0%}" if skip_rate is not None else "Skip rate on clean hunks: n/a")
    cat_acc = agg["category_accuracy"]
    print(f"Category accuracy (on bug hunks): {cat_acc:.0%}" if cat_acc is not None else "Category accuracy: n/a")
    print(f"Jev calls: {agg['jev_calls']}  errors: {agg['jev_errors']}  tokens_in: {agg['tokens_in']}  latency_ms: {agg['latency_ms_total']:.0f}")
    print()
    print("Baselines:")
    print(f"  review-everything: missed bugs = {agg['baseline_review_everything_missed_bugs']} (expected 0, zero cost)")
    print(f"  regex heuristic:   missed bugs = {agg['baseline_regex_missed_bugs']}, skip-rate-on-clean = "
          f"{agg['baseline_regex_skip_rate_on_clean']:.0%}" if agg['baseline_regex_skip_rate_on_clean'] is not None
          else f"  regex heuristic:   missed bugs = {agg['baseline_regex_missed_bugs']}")
    print()
    if agg["bug_hunks_missed"] == 0 and (skip_rate or 0) >= 0.40:
        print("SUCCESS CRITERIA MET: zero missed bugs, skip rate >= 40%.")
    elif agg["bug_hunks_missed"] == 0:
        print("Zero missed bugs, but skip rate below the 40% target.")
    else:
        print("FAILING success criteria: missed bugs > 0.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="oracle-scripted mock demo run (see module docstring)")
    mode.add_argument("--replay", metavar="CACHE_JSONL", help="re-score from a recorded Jev response cache")
    mode.add_argument("--live", action="store_true", help="real Jev API calls (needs OPENROUTER_API_KEY), records a new cache")
    p.add_argument("--heldout", action="store_true", help="evaluate samples_heldout/ instead of samples/")
    p.add_argument("--json-out", metavar="PATH", help="also write the aggregate report as JSON")
    p.add_argument("--record-to", metavar="PATH", default="eval/.jev_cache/run.jsonl", help="cache path for --live")
    args = p.parse_args()

    root = HELDOUT_SAMPLES_DIR if args.heldout else DEFAULT_SAMPLES_DIR
    samples = load_samples(root)
    if not samples:
        print(f"No samples found under {root}", file=sys.stderr)
        return 2
    samples_by_name = {s.name: s for s in samples}

    # Prepare hunks exactly once per sample (see prepare_hunks docstring)
    # so the oracle script (mock mode) and evaluate_sample agree on
    # hunk_id for every hunk.
    hunks_by_sample: dict[str, list[Hunk]] = {s.name: prepare_hunks(s) for s in samples}

    if args.mock:
        script = build_oracle_script(hunks_by_sample, samples_by_name)
        backend: JevBackend = MockJevBackend(script=script)
    elif args.replay:
        backend = ReplayJevBackend(args.replay, TRIAGE_QUESTIONS)
    else:
        backend = RecordingJevBackend(RealJevBackend(), args.record_to)

    ledger = Ledger()
    results = [evaluate_sample(s, hunks_by_sample[s.name], backend, ledger) for s in samples]
    agg = aggregate(results)
    print_report(agg, results, mock=args.mock)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"aggregate": agg, "per_sample": [asdict(r) for r in results]}, indent=2)
        )
        print(f"\nWrote {args.json_out}")

    return 0 if agg["bug_hunks_missed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
