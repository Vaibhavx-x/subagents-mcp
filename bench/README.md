# Model config benchmark — 4 configs, 3 tasks

Decides which model/thinking-level to use for which kind of sub-agent work.

| config | model | thinking_level |
|---|---|---|
| A | gemini-3.8-flash | low |
| B | gemini-3.8-flash | medium |
| C | gemini-3.8-flash | high |
| D | gemini-3.7-flash | medium |

## Setup (5 min, once)

```bash
pip install pytest
./setup.sh
```

Then open `run.sh` and fix the two marked lines so the flags match `agy --help`
on your machine. That is the only edit you need to make.

## Sanity check before spending quota

```bash
cd fixture_repo && python -m pytest -q; cd ..
```
Expect: `test_smoke.py` passes (2), `test_parser.py` fails (4, import error),
`test_cache.py::test_repeated_lookup` fails (1). If that is not what you see,
stop — the fixtures are wrong and every result will be garbage.

## Run

Everything, randomised order, 2 repeats (24 runs, ~1 hr unattended):
```bash
./run_all.sh 2
```

Or one cell at a time:
```bash
./run.sh B t2 1
```

## Results

```bash
python3 report.py > RESULTS.md
```

## The three tasks

- **t1** — rename `fetch_cfg` to `load_config` across 4 files. Mechanical.
- **t2** — implement `parse_duration` so 4 existing tests pass. Normal work.
- **t3** — debug `test_repeated_lookup`. The symptom is in lookup, the cause is
  in the key function. Genuinely requires reasoning.

Each `tasks/<id>/check.sh` exits with PASS or FAIL deterministically. All three
also assert no test file was modified — a model editing the test to make it pass
scores as a failure, and this does happen.

## Notes

- Runs are serial on purpose. You are measuring per-run cost; parallel runs
  contend for quota and distort both timing and throttling.
- `status` is separate from `passed`. Timeouts and rate-limits are excluded from
  aggregates rather than counted as failures.
- Output size in bytes is the cost proxy. If `agy` reports token counts in its
  output, grep them out of `logs/*.log` and add a column — that is strictly
  better, but the byte proxy is consistent across configs and good enough to
  rank them.
- Full transcripts land in `logs/`. Read the failures; that is where the
  interesting findings are.

## The A/B comparison is a different experiment

This bench measures **one worker doing one task** across model configurations.
It ranks cost and says nothing about whether the orchestrator helps.

That question lives in [`ab/`](ab/): three arms (no server, server registered
but unused, server delegating), the same task, parent tokens measured on each.
Results in [`ab/RESULTS.md`](ab/RESULTS.md).

```bash
python bench/ab/preflight.py            # can a headless parent call MCP tools?
python bench/ab/run_ab.py --repeats 3   # spends tokens
python bench/ab/run_ab.py --repeats 1 --dry   # fake parent, spends nothing
python bench/ab/report.py               # writes ab/RESULTS.md
python bench/ab/demo_taint.py           # the out-of-scope demo, 2 workers
```

## And task reuse is a third experiment

[`cache/`](cache/) asks how often finished work gets asked for twice, and what
it costs when it does. Results in [`cache/RESULTS.md`](cache/RESULTS.md).

```bash
python bench/cache/replay.py     # the historical bound; free, no agy
python bench/cache/measure.py    # cold against warm; ONE worker
```

