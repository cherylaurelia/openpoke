"""Run every scenario against each version, N trials each, in fresh copies. Appends to results/latest.jsonl.

usage: python evals/run.py <trials> original=<path> fork=<path> [--only=id,id]
Needs OPENROUTER_API_KEY in the environment. See evals/README.md.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from scenarios import SCENARIOS  # noqa: E402

trials = int(sys.argv[1])
args = [a for a in sys.argv[2:] if not a.startswith("--")]
only = next((a.split("=", 1)[1].split(",") for a in sys.argv[2:] if a.startswith("--only=")), None)
impls = dict(a.split("=", 1) for a in args)
PY = os.environ.get("EVAL_PYTHON", sys.executable)
env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"],
       "EVAL_MODEL": os.environ.get("EVAL_MODEL", "anthropic/claude-sonnet-5.5")}
out = HERE / "results" / "latest.jsonl"
sem = asyncio.Semaphore(int(os.environ.get('EVAL_CONCURRENCY', '2')))


async def run(impl, src, sid, trial):
    async with sem:
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(Path(src) / "server", tmp / "server", ignore=shutil.ignore_patterns("data", "__pycache__", "tests"))
        if impl == "original":  # give it the fork's end-the-turn-after-wait fix, so only routing differs
            rt = tmp / "server/agents/interaction_agent/runtime.py"
            anchor = "            if not parsed_tool_calls:\n                break\n"
            text = rt.read_text()
            assert text.count(anchor) == 1
            rt.write_text(text.replace(anchor, anchor + "            if all(c.name == 'wait' for c in parsed_tool_calls):\n                break\n"))
        proc = await asyncio.create_subprocess_exec(
            PY, str(HERE / "run_one.py"), str(tmp), impl, sid, cwd=tmp, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        shutil.rmtree(tmp, ignore_errors=True)
        lines = [l for l in stdout.decode().splitlines() if l.startswith("{")]
        if lines:
            row = json.loads(lines[-1])
        else:
            row = dict(impl=impl, scenario=sid, ok=False, error="harness: " + stderr.decode()[-400:])
        row["trial"] = trial
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"{impl:8} {sid:30} t{trial} {'PASS' if row['ok'] else 'FAIL'}", flush=True)


async def main():
    ids = only or list(SCENARIOS)
    await asyncio.gather(*(run(i, src, sid, t) for t in range(trials) for sid in ids for i, src in impls.items()))


asyncio.run(main())
