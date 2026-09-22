"""Orchestrates the full Jev-triaged pipeline: pre-filter -> Jev triage
-> route -> LLM review (only routed hunks) -> comment gate.

Used both for a standalone run (report.py's report.md/report.json) and
as "Arm A" inside eval/compare.py's Jev-vs-LLM-only comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jev_review.gate import GateResult, gate_comments
from jev_review.hunk_model import Hunk
from jev_review.jev_client import JevBackend, JevError, ReplayCacheMiss
from jev_review.llm_client import LLMBackend, ReviewComment
from jev_review.prefilter import prefilter_hunks
from jev_review.questions import TRIAGE_QUESTIONS
from jev_review.routing import Route, route_hunk


@dataclass
class PipelineLedger:
    hunks_seen: int = 0
    hunks_prefiltered: int = 0
    hunks_routed_skip: int = 0
    hunks_routed_light: int = 0
    hunks_routed_deep: int = 0
    jev_triage_calls: int = 0
    jev_triage_errors: int = 0
    jev_gate_calls: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    jev_tokens_in: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    jev_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    cost: float = 0.0


@dataclass
class HunkRouteRecord:
    """One row of per-hunk triage output, for report.py's per-hunk
    table (design-doc.md §7)."""

    hunk_id: str
    file: str
    route: str
    reason: str
    category: str | None
    severity: float | None


@dataclass
class PipelineResult:
    comments: list[ReviewComment] = field(default_factory=list)
    ungated_comments: list[ReviewComment] = field(default_factory=list)
    dropped_by_gate: list[GateResult] = field(default_factory=list)
    skipped: list[tuple[Hunk, str]] = field(default_factory=list)  # prefiltered or routed-skip
    review_failed: list[tuple[Hunk, str]] = field(default_factory=list)  # LLM call errored, never silently "clean"
    hunk_routes: list[HunkRouteRecord] = field(default_factory=list)
    ledger: PipelineLedger = field(default_factory=PipelineLedger)


def run_pipeline(
    hunks: list[Hunk],
    jev_backend: JevBackend,
    llm_backend: LLMBackend,
    *,
    use_gate: bool = True,
    hint_llm_with_jev_category: bool = True,
    gate_log_path: str | Path | None = None,
) -> PipelineResult:
    result = PipelineResult()
    ledger = result.ledger
    ledger.hunks_seen = len(hunks)

    kept, dropped = prefilter_hunks(hunks)
    ledger.hunks_prefiltered = len(dropped)
    for h, reason in dropped:
        result.skipped.append((h, reason))

    for h in kept:
        state = h.render_state()
        try:
            jev_resp = jev_backend.ask(state, TRIAGE_QUESTIONS, hunk_id=h.hunk_id)
            ledger.jev_triage_calls += 1
            ledger.jev_tokens_in += jev_resp.tokens_in
            ledger.jev_latency_ms += jev_resp.latency_ms
            ledger.cost += jev_resp.cost
        except (JevError, ReplayCacheMiss):
            ledger.jev_triage_errors += 1
            jev_resp = None  # fail-safe: None -> route_hunk sends this to light-review

        decision = route_hunk(h, jev_resp)
        category = jev_resp.choice("risk_category").value if jev_resp else None
        severity = jev_resp.score("risk_severity").value if jev_resp else None
        result.hunk_routes.append(
            HunkRouteRecord(h.hunk_id, h.file_path, decision.route.value, decision.reason, category, severity)
        )

        if decision.route == Route.SKIP:
            ledger.hunks_routed_skip += 1
            result.skipped.append((h, decision.reason))
            continue
        elif decision.route == Route.LIGHT_REVIEW:
            ledger.hunks_routed_light += 1
        else:
            ledger.hunks_routed_deep += 1

        hint = jev_resp.choice("risk_category").value if (jev_resp and hint_llm_with_jev_category) else None
        try:
            llm_resp = llm_backend.review(h, decision.route, jev_hint_category=hint)
            ledger.llm_calls += 1
            ledger.llm_tokens_in += llm_resp.tokens_in
            ledger.llm_tokens_out += llm_resp.tokens_out
            ledger.llm_latency_ms += llm_resp.latency_ms
            ledger.cost += llm_resp.cost
        except JevError as e:
            # A failed LLM call means this hunk was never actually
            # reviewed — report that explicitly, never silently as
            # "clean" (top-level fail-safe rule).
            ledger.llm_errors += 1
            result.review_failed.append((h, str(e)))
            continue

        if use_gate:
            gate_results = gate_comments(llm_resp.comments, h, jev_backend, dropped_log_path=gate_log_path)
            ledger.jev_gate_calls += len(gate_results)
            for gr in gate_results:
                if gr.kept:
                    result.comments.append(gr.comment)
                    if gr.ungated:
                        result.ungated_comments.append(gr.comment)
                else:
                    result.dropped_by_gate.append(gr)
        else:
            result.comments.extend(llm_resp.comments)

    return result
