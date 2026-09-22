# Jev-triaged agentic code review (PoC)

Status: **Checkpoint B built** — the full pipeline (hunk splitting,
pre-filter, Jev client, routing, the review-LLM stage, the comment
gate) plus both eval scripts: `eval/eval.py` (triage-only) and
`eval/compare.py` (Jev-triaged vs. LLM-only, the final deliverable per
`design-doc.md`'s build order).

See `planning-prompt.md` for the original design brief and
`design-doc.md` for the full design this code implements, including
the "Unverified assumptions" list — the OpenRouter System One API
schema in `jev_review/jev_client.py` is a best-effort guess isolated
behind `parse_jev_response`, pending a real schema check.

## Layout

```
jev_review/       # hunk_model, prefilter, questions, thresholds,
                   # jev_client (real/mock/record/replay), routing,
                   # llm_client + mock_llm_backend (review stage),
                   # gate (comment gate), review_pipeline (orchestrates
                   # prefilter -> triage -> route -> LLM -> gate),
                   # report (report.md / report.json)
eval/eval.py       # triage-only eval: missed bugs, recall, skip rate,
                   # category accuracy, cost/latency, two baselines
eval/compare.py    # Jev-triaged (Arm A) vs LLM-only (Arm B): full
                   # report + a short summary.md, >=3 runs/arm
samples/           # 13 tuning samples: before.py/after.py/label.json
samples_heldout/    # 3 held-out samples, not used while tuning
tests/               # 72 tests, all offline (mock backends + fixtures)
```

## Running it

```bash
pip install pytest        # only dev dependency
python -m pytest tests/ -q

python eval/eval.py --mock             # oracle-scripted demo run (see below)
python eval/eval.py --mock --heldout   # same, on the held-out slice
python eval/eval.py --live             # real Jev calls, needs OPENROUTER_API_KEY
python eval/eval.py --replay <cache.jsonl>  # re-score a --live run for free

python eval/compare.py --mock          # Jev-triaged vs LLM-only, oracle demo
python eval/compare.py --live          # real Jev + LLM calls, needs OPENROUTER_API_KEY
```

**`--mock` is a structural demo, not a real evaluation.** Its answers
are generated from each sample's own label (an oracle), to prove the
harness is wired correctly end to end — diffing, splitting, the
pre-filter, routing, and the fail-safe paths. It cannot tell you
whether Jev's *real* triage is any good; only `--live` (or replaying
its cache) can. Current mock run: 0 missed bugs, 100% skip rate on
clean hunks, both baselines beaten.

Every failure mode (a Jev call erroring, an empty replay cache, no API
key) is exercised in `tests/test_eval.py` and always resolves to
*more* review, never a silent skip — confirmed by
`test_jev_call_failure_never_produces_a_missed_bug`.

## `eval/compare.py`: Jev-triaged vs. LLM-only

Runs the same samples through both arms and writes `report_<run>.md`
(full comparison, per-sample table, disagreements, verdict),
`summary_<run>.md` (a paste-into-chat-sized summary), and the raw
`compare_<run>.json`, to `eval/compare_out/` by default. Mock demo
result on the tuning set: equal recall (10/10 both arms), 23% fewer
hunks sent to the LLM by Arm A, zero noise either way. As with `eval.py`,
`--mock` proves the harness end to end; it is not a real measurement —
`--live` is what actually answers design-doc.md §9's questions.

## Not built yet

Everything through Checkpoint B (design-doc.md's final deliverable) is
implemented. What's left is running it for real: `openrouter.ai` is
blocked from this session's network, so no `--live` run — of either
`eval.py` or `compare.py` — has actually happened yet. That, plus
checking `jev_client.py`'s parsed response shape against the real API
(see design-doc.md's "Unverified assumptions"), is the next step
outside this session.
