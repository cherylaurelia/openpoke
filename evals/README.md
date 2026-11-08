# Routing evals

Live tests of how Poke routes a message among many agents, run against the original OpenPoke and
this fork with the real model. Results and what they mean are in [WRITEUP.md](../WRITEUP.md#how-i-tested-it).

- `scenarios.py`: the 27 scenarios. Each one seeds a roster (12, 50 or 200 agents, the targets
  hidden among similar filler agents), optional chat history, one user message, and the expected
  outcome: route to an agent, route to several, create one, or ask.
- `run_one.py`: runs one scenario on one version in a fresh copy of its `server/` folder. The
  interaction agent and the model are real; messages to workers are recorded instead of run. It
  grades the result in code and prints one JSON line.
- `run.py`: runs every scenario against each version, N times each, and appends to
  `results/latest.jsonl`.
- `summary.py`: pass rate per scenario and version, plus totals.
- `results/`: the runs quoted in the write-up. `final-vs-original.jsonl` is the 52/52 vs 42/52
  run; `after-prompt-change.jsonl` is the later 57/57 run of this fork.

## Run it

Each run calls the real model, about 2 to 3 calls of ~14k prompt tokens per scenario and version.

```bash
# the original code, to compare against
git worktree add ../openpoke-original 5b5f635

# one scenario, once, against both versions
OPENROUTER_API_KEY=... python evals/run.py 1 original=../openpoke-original fork=. --only=ambiguous_alices_small

# everything, twice
OPENROUTER_API_KEY=... python evals/run.py 2 original=../openpoke-original fork=.

python evals/summary.py                                   # your run
python evals/summary.py evals/results/final-vs-original.jsonl  # the run in the write-up
```

Use the project's virtualenv (`server/requirements.txt`). `EVAL_MODEL` sets the model (default
`anthropic/claude-sonnet-5.5`), and `EVAL_CONCURRENCY` sets how many runs go at once (default 2).

To keep the comparison about routing, the original gets two changes from this fork: the same model
(its default was retired), and the fix that ends a turn after a step that only waits (otherwise it
loops until the step limit and errors).
