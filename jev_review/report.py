"""Report writers for a single pipeline run (design-doc.md §7): a
human-readable report.md and a machine-readable report.json shaped for
a future "post inline PR comment" step (file/line/side/body matches
GitHub's review-comment shape directly)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from jev_review.review_pipeline import PipelineResult


def write_json_report(result: PipelineResult, path: str | Path) -> None:
    data = {
        "comments": [
            {
                "file": c.file,
                "line": c.line,
                "side": c.side,
                "body": c.body,
                "severity": c.severity,
                "route": c.route,
                "hunk_id": c.hunk_id,
                "ungated": c in result.ungated_comments,
            }
            for c in result.comments
        ],
        "skipped": [{"hunk_id": h.hunk_id, "file": h.file_path, "reason": reason} for h, reason in result.skipped],
        "review_failed": [
            {"hunk_id": h.hunk_id, "file": h.file_path, "error": err} for h, err in result.review_failed
        ],
        "dropped_by_gate": [
            {
                "file": gr.comment.file,
                "line": gr.comment.line,
                "body": gr.comment.body,
                "hunk_id": gr.comment.hunk_id,
                "is_actionable": gr.is_actionable,
                "problem_in_shown_code": gr.problem_in_shown_code,
            }
            for gr in result.dropped_by_gate
        ],
        "hunk_routes": [asdict(r) for r in result.hunk_routes],
        "ledger": asdict(result.ledger),
    }
    Path(path).write_text(json.dumps(data, indent=2))


def write_human_report(result: PipelineResult, path: str | Path) -> None:
    l = result.ledger
    lines = [
        "# Review report",
        "",
        f"Hunks seen: {l.hunks_seen} — prefiltered: {l.hunks_prefiltered}, "
        f"skip: {l.hunks_routed_skip}, light-review: {l.hunks_routed_light}, "
        f"deep-review: {l.hunks_routed_deep}",
        f"Jev calls: {l.jev_triage_calls} (errors: {l.jev_triage_errors}), "
        f"gate calls: {l.jev_gate_calls}, LLM calls: {l.llm_calls} (errors: {l.llm_errors})",
        f"Tokens in — Jev: {l.jev_tokens_in}, LLM: {l.llm_tokens_in} — LLM tokens out: {l.llm_tokens_out}",
        f"Total cost: ${l.cost:.4f}",
        "",
        "## Comments",
        "",
    ]
    if not result.comments:
        lines.append("_No comments._")
    for c in sorted(result.comments, key=lambda c: (c.file, c.line)):
        tag = " (ungated — gate call failed)" if c in result.ungated_comments else ""
        lines.append(f"- **{c.file}:{c.line}** [{c.severity}, {c.route}]{tag} — {c.body}")

    lines += ["", "## Skipped hunks", ""]
    if not result.skipped:
        lines.append("_None skipped._")
    for h, reason in sorted(result.skipped, key=lambda p: p[0].file_path):
        lines.append(f"- {h.file_path} (hunk {h.hunk_id}): skipped — {reason}")

    if result.review_failed:
        lines += ["", "## NOT reviewed (LLM call failed — do not treat as clean)", ""]
        for h, err in result.review_failed:
            lines.append(f"- {h.file_path} (hunk {h.hunk_id}): {err}")

    if result.dropped_by_gate:
        lines += ["", "## Comments dropped by the gate (audit)", ""]
        for gr in result.dropped_by_gate:
            lines.append(
                f"- {gr.comment.file}:{gr.comment.line} — \"{gr.comment.body}\" "
                f"(is_actionable={gr.is_actionable:.2f}, problem_in_shown_code={gr.problem_in_shown_code:.2f})"
            )

    Path(path).write_text("\n".join(lines) + "\n")
