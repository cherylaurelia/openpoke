"""Pass rate per scenario and version, plus totals, from a results file.

usage: python evals/summary.py [results/final-vs-original.jsonl]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from scenarios import SCENARIOS  # noqa: E402

path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "results" / "latest.jsonl"
rows = [json.loads(line) for line in path.open()]
impls = [i for i in ("original", "fork") if any(r["impl"] == i for r in rows)]
by = defaultdict(list)
for r in rows:
    by[(r["scenario"], r["impl"])].append(r)

print(f"{'scenario':32}" + "".join(f"{i:>10}" for i in impls))
for sid in SCENARIOS:
    cells = [f"{sum(r['ok'] for r in rs)}/{len(rs)}" if (rs := by.get((sid, i))) else "-" for i in impls]
    if any(c != "-" for c in cells):
        print(f"{sid:32}" + "".join(f"{c:>10}" for c in cells))

print()
for label, fn in [
    ("passed", lambda rs: f"{sum(r['ok'] for r in rs)}/{len(rs)}"),
    ("errors", lambda rs: str(sum(bool(r.get("error")) for r in rs))),
    ("used search", lambda rs: str(sum(bool(r.get("searched")) for r in rs))),
    ("avg prompt tokens", lambda rs: f"{sum(r.get('prompt_tokens', 0) for r in rs) / len(rs):,.0f}"),
]:
    print(f"{label:32}" + "".join(f"{fn([r for r in rows if r['impl'] == i]):>10}" for i in impls))
