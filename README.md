# Jev-triaged agentic code review (PoC)

Status: **Checkpoint A built** — the offline half of the pipeline
(hunk splitting, pre-filter, Jev client with mock/record/replay,
routing, and the triage-only eval harness). No review-LLM stage yet;
see `design-doc.md` §build-order for what's next (Checkpoint B).

See `planning-prompt.md` for the original design brief and
`design-doc.md` for the full design this code implements, including
the "Unverified assumptions" list — the OpenRouter System One API
schema in `jev_review/jev_client.py` is a best-effort guess isolated
behind `parse_jev_response`, pending a real schema check.

## Layout

```
jev_review/       # hunk_model, prefilter, questions, thresholds,
                   # jev_client (real/mock/record/replay), routing
eval/eval.py       # triage-only eval: missed bugs, recall, skip rate,
                   # category accuracy, cost/latency, two baselines
samples/           # 13 tuning samples: before.py/after.py/label.json
samples_heldout/    # 3 held-out samples, not used while tuning
tests/               # 47 tests, all offline (mock backend + fixtures)
```

## Running it

```bash
pip install pytest        # only dev dependency
python -m pytest tests/ -q

python eval/eval.py --mock             # oracle-scripted demo run (see below)
python eval/eval.py --mock --heldout   # same, on the held-out slice
python eval/eval.py --live             # real Jev calls, needs OPENROUTER_API_KEY
python eval/eval.py --replay <cache.jsonl>  # re-score a --live run for free
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

## Not built yet (Checkpoint B)

The review-writing LLM stage, the comment gate, `report.md`/`report.json`
output, and `eval/compare.py` (Jev-triaged vs. LLM-only, with the full
and quick-summary comparison reports). See `design-doc.md`'s build
order for the plan.
