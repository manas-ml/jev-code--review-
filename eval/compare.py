#!/usr/bin/env python3
"""Jev-triaged vs. LLM-only comparison (design-doc.md §9). Runs the
same samples through both arms and writes a full report plus a short
summary.

  Arm A (Jev-triaged): pre-filter -> Jev triage -> route -> LLM review
                        of routed hunks only -> Jev comment gate.
  Arm B (LLM-only):    pre-filter -> LLM reviews EVERY kept hunk with
                        the deep-review prompt (same model, same
                        prompt strength as Arm A's deep tier — see
                        design-doc.md §9 "keep it a fair fight") ->
                        no Jev, no gate.

Matching rule (fixed before results are looked at, design-doc.md §9):
a comment matches a seeded bug if it cites a line within
MATCH_LINE_WINDOW of a labelled bug_line, or — for a deletion-only bug,
which has no post-change line to anchor to — if it was generated from
the hunk containing the deletion.

Usage:
  python eval/compare.py --mock             # oracle-scripted demo (see NOTE)
  python eval/compare.py --mock --heldout
  python eval/compare.py --live             # real Jev + LLM calls

NOTE on --mock: as in eval.py, the mock LLM/Jev answers are built from
each sample's own label. This proves the harness's plumbing end to end
(including that Arm A and Arm B are being scored identically, and that
Arm A's cost savings show up when it should) — it is NOT a real
measurement of either Jev's or the LLM's quality. Only --live answers
design-doc.md's actual comparison questions.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.eval import DEFAULT_SAMPLES_DIR, HELDOUT_SAMPLES_DIR, Label, Sample, _bug_hunk_ids, load_samples
from jev_review.hunk_model import Hunk, attach_enclosing_context, diff_files, split_oversized_hunk
from jev_review.jev_client import RealJevBackend, RecordingJevBackend
from jev_review.llm_client import RealLLMBackend, ReviewComment
from jev_review.mock_backend import MockJevBackend
from jev_review.mock_llm_backend import MockLLMBackend
from jev_review.prefilter import prefilter_hunks
from jev_review.review_pipeline import run_pipeline
from jev_review.routing import Route
from jev_review.thresholds import (
    COMPARISON_RUNS_PER_ARM,
    JEV_MODEL_ID,
    MATCH_LINE_WINDOW,
    REVIEW_LLM_MODEL_ID,
)

# --- Matching ----------------------------------------------------------


def comment_matches_bug(comment: ReviewComment, label: Label, bug_hunk_ids: set[str]) -> bool:
    if label.clean:
        return False
    if label.deletion_only:
        return comment.hunk_id in bug_hunk_ids
    return any(abs(comment.line - bl) <= MATCH_LINE_WINDOW for bl in label.bug_lines)


# --- Arm B: LLM-only (no Jev anywhere) ----------------------------------


def run_llm_only(hunks: list[Hunk], llm_backend) -> tuple[list[ReviewComment], dict]:
    """Fair-fight requirement (design-doc.md §9): every kept hunk gets
    the DEEP_REVIEW prompt/context/max_comments, since that's the
    strongest tier Arm A ever uses — giving Arm B anything weaker would
    set it up to lose on a dimension that has nothing to do with Jev."""
    kept, dropped = prefilter_hunks(hunks)
    comments: list[ReviewComment] = []
    tokens_in = tokens_out = 0
    latency_ms = 0.0
    errors = 0
    for h in kept:
        try:
            resp = llm_backend.review(h, Route.DEEP_REVIEW, jev_hint_category=None)
            comments.extend(resp.comments)
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            latency_ms += resp.latency_ms
        except Exception:
            errors += 1
    stats = {
        "hunks_seen": len(hunks),
        "hunks_prefiltered": len(dropped),
        "hunks_reviewed": len(kept),
        "llm_calls": len(kept) - errors,
        "llm_errors": errors,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
    }
    return comments, stats


# --- Oracle scripting for --mock (mirrors eval.py's approach) -----------


def build_oracle_jev_script(hunks_by_sample: dict[str, list[Hunk]], samples_by_name: dict[str, Sample]) -> dict:
    script = {}
    for name, hunks in hunks_by_sample.items():
        label = samples_by_name[name].label
        bug_ids = _bug_hunk_ids(hunks, label)
        for h in hunks:
            is_bug = h.hunk_id in bug_ids
            category = label.expected_category if is_bug else "style-only"
            severity = 0.85 if (is_bug and label.expected_min_route == "deep-review") else (0.4 if is_bug else 0.05)
            sensitive = 0.6 if (is_bug and category == "security") else 0.05
            script[h.hunk_id] = {
                "risk_category": {"value": category, "confidence": 0.95},
                "risk_severity": {"value": severity, "confidence": 0.95},
                "touches_security_sensitive_input": sensitive,
                "is_generated_or_lockfile": 0.0,
                "needs_wider_context": 0.0,
            }
    return script


def build_oracle_gate_answers() -> None:
    """The gate is scripted permissively (both probabilities high) for
    every gate call in mock mode, via AlwaysPassGateJev below — so the
    mock demo measures triage+LLM plumbing, not gate calibration
    (which has no real oracle signal to script against)."""


class AlwaysPassGateJev(MockJevBackend):
    def ask(self, state, questions, *, hunk_id=None):
        if hunk_id and hunk_id.startswith("gate:"):
            from jev_review.jev_client import parse_jev_response

            raw = {"answers": {qid: {"probability": 0.95} for qid in questions}, "usage": {}}
            return parse_jev_response(raw, questions)
        return super().ask(state, questions, hunk_id=hunk_id)


def build_oracle_llm_script(hunks_by_sample: dict[str, list[Hunk]], samples_by_name: dict[str, Sample]) -> dict:
    script = {}
    for name, hunks in hunks_by_sample.items():
        label = samples_by_name[name].label
        bug_ids = _bug_hunk_ids(hunks, label)
        for h in hunks:
            if h.hunk_id in bug_ids:
                line = label.bug_lines[0] if label.bug_lines else h.new_range[0]
                script[h.hunk_id] = [{"line": line, "side": "RIGHT", "body": f"seeded: {label.description}", "severity": "high"}]
            else:
                script[h.hunk_id] = []  # correct: no issue
    return script


# --- Per-run, per-arm scoring --------------------------------------------


@dataclass
class ArmSampleScore:
    sample: str
    clean: bool
    bugs_total: int
    bugs_found: int
    bugs_missed: list[str] = field(default_factory=list)
    noise_comments: int = 0
    comments_on_clean: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    hunks_reviewed: int = 0
    hunks_total: int = 0


def score_arm_a(sample: Sample, hunks: list[Hunk], jev_backend, llm_backend) -> ArmSampleScore:
    bug_ids = _bug_hunk_ids(hunks, sample.label)
    result = run_pipeline(hunks, jev_backend, llm_backend, use_gate=True, hint_llm_with_jev_category=True)
    reviewed_hunk_ids = {r.hunk_id for r in result.hunk_routes if r.route != "skip"}

    bugs_found_ids = {h for h in bug_ids if any(c.hunk_id == h and comment_matches_bug(c, sample.label, bug_ids) for c in result.comments)}
    bugs_missed = []
    for h_id in bug_ids - bugs_found_ids:
        cause = "skipped-by-triage" if h_id not in reviewed_hunk_ids else (
            "dropped-by-gate" if any(gr.comment.hunk_id == h_id for gr in result.dropped_by_gate) else "reviewed-but-missed"
        )
        bugs_missed.append(f"{h_id}:{cause}")

    noise = sum(1 for c in result.comments if not comment_matches_bug(c, sample.label, bug_ids))
    comments_on_clean = len(result.comments) if sample.label.clean else 0

    return ArmSampleScore(
        sample=sample.name,
        clean=sample.label.clean,
        bugs_total=len(bug_ids),
        bugs_found=len(bugs_found_ids),
        bugs_missed=bugs_missed,
        noise_comments=noise,
        comments_on_clean=comments_on_clean,
        tokens_in=result.ledger.jev_tokens_in + result.ledger.llm_tokens_in,
        tokens_out=result.ledger.llm_tokens_out,
        latency_ms=result.ledger.jev_latency_ms + result.ledger.llm_latency_ms,
        hunks_reviewed=len(reviewed_hunk_ids),
        hunks_total=len(hunks),
    )


def score_arm_b(sample: Sample, hunks: list[Hunk], llm_backend) -> ArmSampleScore:
    bug_ids = _bug_hunk_ids(hunks, sample.label)
    comments, stats = run_llm_only(hunks, llm_backend)

    bugs_found_ids = {h for h in bug_ids if any(c.hunk_id == h and comment_matches_bug(c, sample.label, bug_ids) for c in comments)}
    bugs_missed = [f"{h}:reviewed-but-missed" for h in bug_ids - bugs_found_ids]

    noise = sum(1 for c in comments if not comment_matches_bug(c, sample.label, bug_ids))
    comments_on_clean = len(comments) if sample.label.clean else 0

    return ArmSampleScore(
        sample=sample.name,
        clean=sample.label.clean,
        bugs_total=len(bug_ids),
        bugs_found=len(bugs_found_ids),
        bugs_missed=bugs_missed,
        noise_comments=noise,
        comments_on_clean=comments_on_clean,
        tokens_in=stats["tokens_in"],
        tokens_out=stats["tokens_out"],
        latency_ms=stats["latency_ms"],
        hunks_reviewed=stats["hunks_reviewed"],
        hunks_total=stats["hunks_seen"],
    )


def aggregate_arm(scores: list[ArmSampleScore]) -> dict:
    bugs_total = sum(s.bugs_total for s in scores)
    bugs_found = sum(s.bugs_found for s in scores)
    return {
        "bugs_total": bugs_total,
        "bugs_found": bugs_found,
        "bugs_missed": bugs_total - bugs_found,
        "missed_detail": [d for s in scores for d in s.bugs_missed],
        "noise_comments": sum(s.noise_comments for s in scores),
        "comments_on_clean": sum(s.comments_on_clean for s in scores),
        "tokens_in": sum(s.tokens_in for s in scores),
        "tokens_out": sum(s.tokens_out for s in scores),
        "latency_ms": sum(s.latency_ms for s in scores),
        "hunks_reviewed": sum(s.hunks_reviewed for s in scores),
        "hunks_total": sum(s.hunks_total for s in scores),
    }


# --- Full run across COMPARISON_RUNS_PER_ARM repetitions -----------------


def run_comparison(samples: list[Sample], mock: bool, record_dir: str | None) -> dict:
    samples_by_name = {s.name: s for s in samples}
    hunks_by_sample = {s.name: _prepare(s) for s in samples}

    run_summaries_a = []
    run_summaries_b = []
    per_sample_a: list[list[ArmSampleScore]] = []
    per_sample_b: list[list[ArmSampleScore]] = []

    for run_idx in range(COMPARISON_RUNS_PER_ARM):
        if mock:
            jev_script = build_oracle_jev_script(hunks_by_sample, samples_by_name)
            llm_script = build_oracle_llm_script(hunks_by_sample, samples_by_name)
            jev_backend = AlwaysPassGateJev(script=jev_script)
            llm_backend_a = MockLLMBackend(script=llm_script)
            llm_backend_b = MockLLMBackend(script=llm_script)
        else:
            jev_cache = f"{record_dir}/jev_run{run_idx}.jsonl" if record_dir else f"eval/.jev_cache/compare_run{run_idx}.jsonl"
            jev_backend = RecordingJevBackend(RealJevBackend(), jev_cache)
            llm_backend_a = RealLLMBackend()
            llm_backend_b = RealLLMBackend()

        scores_a = [score_arm_a(s, hunks_by_sample[s.name], jev_backend, llm_backend_a) for s in samples]
        scores_b = [score_arm_b(s, hunks_by_sample[s.name], llm_backend_b) for s in samples]
        per_sample_a.append(scores_a)
        per_sample_b.append(scores_b)
        run_summaries_a.append(aggregate_arm(scores_a))
        run_summaries_b.append(aggregate_arm(scores_b))

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jev_model": JEV_MODEL_ID,
        "llm_model": REVIEW_LLM_MODEL_ID,
        "mock": mock,
        "runs_per_arm": COMPARISON_RUNS_PER_ARM,
        "arm_a_runs": run_summaries_a,
        "arm_b_runs": run_summaries_b,
        "per_sample_a": [[asdict(s) for s in run] for run in per_sample_a],
        "per_sample_b": [[asdict(s) for s in run] for run in per_sample_b],
    }


def _prepare(sample: Sample) -> list[Hunk]:
    hunks = diff_files(sample.name + "/before.py", sample.name + "/after.py", sample.before, sample.after)
    split: list[Hunk] = []
    for h in hunks:
        split.extend(split_oversized_hunk(h))
    if sample.after:
        for h in split:
            attach_enclosing_context(h, sample.after)
    return split


# --- Reporting -------------------------------------------------------------


def _mean_spread(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.mean(values)
    spread = statistics.pstdev(values) if len(values) > 1 else 0.0
    return mean, spread


def write_full_report(data: dict, path: Path) -> None:
    lines = [
        "# Jev-triaged vs. LLM-only comparison",
        "",
        f"Run: {data['run_id']}  |  Jev: `{data['jev_model']}`  |  LLM: `{data['llm_model']}`  |  "
        f"runs/arm: {data['runs_per_arm']}" + ("  |  **MOCK (oracle demo, not a real measurement)**" if data["mock"] else ""),
        "",
        "## Summary (mean across runs, ± population stdev)",
        "",
        "| metric | Arm A (Jev-triaged) | Arm B (LLM-only) | diff |",
        "|---|---|---|---|",
    ]

    def row(label: str, key: str, fmt="{:.2f}"):
        a_vals = [r[key] for r in data["arm_a_runs"]]
        b_vals = [r[key] for r in data["arm_b_runs"]]
        a_mean, a_sd = _mean_spread(a_vals)
        b_mean, b_sd = _mean_spread(b_vals)
        diff = a_mean - b_mean
        lines.append(
            f"| {label} | {fmt.format(a_mean)} ± {fmt.format(a_sd)} | {fmt.format(b_mean)} ± {fmt.format(b_sd)} | {fmt.format(diff)} |"
        )

    row("bugs found", "bugs_found")
    row("bugs missed", "bugs_missed")
    row("noise comments", "noise_comments")
    row("comments on clean samples", "comments_on_clean")
    row("hunks reviewed by LLM", "hunks_reviewed")
    row("tokens in", "tokens_in")
    row("tokens out", "tokens_out")
    row("latency (ms)", "latency_ms", "{:.0f}")

    total_hunks = data["arm_a_runs"][0]["hunks_total"] if data["arm_a_runs"] else 0
    a_reviewed = statistics.mean(r["hunks_reviewed"] for r in data["arm_a_runs"]) if data["arm_a_runs"] else 0
    b_reviewed = statistics.mean(r["hunks_reviewed"] for r in data["arm_b_runs"]) if data["arm_b_runs"] else 0
    lines += [
        "",
        f"Hunks seen (both arms, after the shared pre-filter): {total_hunks}. "
        f"Arm A sent {a_reviewed:.1f} to the LLM ({a_reviewed / total_hunks:.0%} of hunks seen); "
        f"Arm B sent {b_reviewed:.1f} ({b_reviewed / total_hunks:.0%}), by definition all of them.",
        "",
        "## Per-sample (run 0)",
        "",
        "| sample | clean | A: found/total | A: noise | A: missed | B: found/total | B: noise | B: missed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    a0 = {s["sample"]: s for s in data["per_sample_a"][0]} if data["per_sample_a"] else {}
    b0 = {s["sample"]: s for s in data["per_sample_b"][0]} if data["per_sample_b"] else {}
    disagreements = []
    for name in sorted(set(a0) | set(b0)):
        a = a0.get(name, {})
        b = b0.get(name, {})
        lines.append(
            f"| {name} | {a.get('clean')} | {a.get('bugs_found')}/{a.get('bugs_total')} | "
            f"{a.get('noise_comments')} | {len(a.get('bugs_missed', []))} | "
            f"{b.get('bugs_found')}/{b.get('bugs_total')} | {b.get('noise_comments')} | "
            f"{len(b.get('bugs_missed', []))} |"
        )
        if a.get("bugs_missed") != b.get("bugs_missed"):
            disagreements.append((name, a.get("bugs_missed", []), b.get("bugs_missed", [])))

    lines += ["", "## Disagreements between arms (run 0)", ""]
    if not disagreements:
        lines.append("_None — both arms missed exactly the same bugs (including none)._")
    for name, a_missed, b_missed in disagreements:
        lines.append(f"- **{name}**: Arm A missed {a_missed or '[]'}, Arm B missed {b_missed or '[]'}")

    a_missed_total = statistics.mean(r["bugs_missed"] for r in data["arm_a_runs"])
    b_missed_total = statistics.mean(r["bugs_missed"] for r in data["arm_b_runs"])
    lines += ["", "## Verdict", ""]
    if data["mock"]:
        lines.append("_(Mock/oracle run — this verdict reflects the harness's plumbing, not real model quality. Run `--live` for a real answer.)_")
        lines.append("")
    recall_ok = a_missed_total <= b_missed_total
    cost_savings = 1 - (a_reviewed / b_reviewed if b_reviewed else 1)
    lines.append(
        f"- Recall: Arm A missed {a_missed_total:.1f} bugs/run vs. Arm B's {b_missed_total:.1f}/run — "
        f"{'within margin' if recall_ok else 'WORSE than LLM-only'}."
    )
    lines.append(f"- Cost: Arm A sent {cost_savings:.0%} fewer hunks to the LLM than Arm B.")
    gate_dropped_total = 0  # populated by caller if desired; left 0 here since full gate audit lives in dropped.jsonl
    lines.append(
        "- Worth continuing: "
        + ("yes — recall held and cost dropped." if recall_ok and cost_savings > 0 else "re-examine thresholds before continuing.")
    )

    path.write_text("\n".join(lines) + "\n")


def write_summary(data: dict, full_report_path: Path, path: Path) -> None:
    a_missed = statistics.mean(r["bugs_missed"] for r in data["arm_a_runs"])
    b_missed = statistics.mean(r["bugs_missed"] for r in data["arm_b_runs"])
    a_found = statistics.mean(r["bugs_found"] for r in data["arm_a_runs"])
    b_found = statistics.mean(r["bugs_found"] for r in data["arm_b_runs"])
    a_reviewed = statistics.mean(r["hunks_reviewed"] for r in data["arm_a_runs"])
    b_reviewed = statistics.mean(r["hunks_reviewed"] for r in data["arm_b_runs"])
    a_noise = statistics.mean(r["noise_comments"] for r in data["arm_a_runs"])
    b_noise = statistics.mean(r["noise_comments"] for r in data["arm_b_runs"])
    a_tok = statistics.mean(r["tokens_in"] + r["tokens_out"] for r in data["arm_a_runs"])
    b_tok = statistics.mean(r["tokens_in"] + r["tokens_out"] for r in data["arm_b_runs"])
    cost_savings = 1 - (a_reviewed / b_reviewed if b_reviewed else 1)

    verdict = (
        f"Jev triage: {'same' if a_missed <= b_missed else 'worse'} recall "
        f"({a_found:.0f}/{a_found + a_missed:.0f} vs {b_found:.0f}/{b_found + b_missed:.0f}), "
        f"{cost_savings:.0%} fewer hunks sent to the LLM, "
        f"{'fewer' if a_noise <= b_noise else 'more'} noise comments "
        f"({a_noise:.0f} vs {b_noise:.0f}). "
        f"{'Worth continuing.' if a_missed <= b_missed and cost_savings > 0 else 'Re-examine before continuing.'}"
    )
    if data["mock"]:
        verdict = "[MOCK DEMO, not a real measurement] " + verdict

    lines = [
        "# Comparison summary",
        "",
        f"**{verdict}**",
        "",
        "| metric | Arm A (Jev) | Arm B (LLM-only) | diff |",
        "|---|---|---|---|",
        f"| bugs found | {a_found:.1f} | {b_found:.1f} | {a_found - b_found:+.1f} |",
        f"| bugs missed | {a_missed:.1f} | {b_missed:.1f} | {a_missed - b_missed:+.1f} |",
        f"| noise comments | {a_noise:.1f} | {b_noise:.1f} | {a_noise - b_noise:+.1f} |",
        f"| tokens (in+out) | {a_tok:.0f} | {b_tok:.0f} | {a_tok - b_tok:+.0f} |",
        f"| hunks sent to LLM | {a_reviewed:.1f} | {b_reviewed:.1f} | {a_reviewed - b_reviewed:+.1f} |",
        "",
        "**Top findings:**",
        f"1. Cost: Arm A used {cost_savings:.0%} fewer LLM calls than Arm B.",
        f"2. Recall gap: {abs(a_missed - b_missed):.1f} bugs/run difference between arms.",
        f"3. See the full report for per-sample disagreements and missed-bug causes.",
        "",
        f"**Next step:** {'proceed — run --live to confirm with real Jev/LLM calls.' if data['mock'] else ('tune thresholds for the missed bugs above.' if a_missed > 0 else 'ship the current thresholds; monitor noise rate in production.')}",
        "",
        f"Run {data['run_id']} · Jev `{data['jev_model']}` · LLM `{data['llm_model']}` · full report: {full_report_path.name}",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--live", action="store_true")
    p.add_argument("--heldout", action="store_true")
    p.add_argument("--out-dir", default="eval/compare_out")
    args = p.parse_args()

    root = HELDOUT_SAMPLES_DIR if args.heldout else DEFAULT_SAMPLES_DIR
    samples = load_samples(root)
    if not samples:
        print(f"No samples found under {root}", file=sys.stderr)
        return 2

    data = run_comparison(samples, mock=args.mock, record_dir=None)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"compare_{data['run_id']}.json"
    full_path = out_dir / f"report_{data['run_id']}.md"
    summary_path = out_dir / f"summary_{data['run_id']}.md"

    json_path.write_text(json.dumps(data, indent=2))
    write_full_report(data, full_path)
    write_summary(data, full_path, summary_path)

    print(summary_path.read_text())
    print(f"\nFull report: {full_path}\nJSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
