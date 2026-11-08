"""Run one scenario, one turn, against one version, with the real model. Prints one JSON line.

usage: python run_one.py <fresh_repo_copy> <original|fork> <scenario_id>
Workers never actually run: messages to execution agents are recorded, not executed.
Called by run.py, which makes the fresh copy.
"""
import asyncio
import json
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

root, IMPL, sid = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(root))
sys.path.insert(0, str(Path(__file__).parent))
from scenarios import SCENARIOS, filler  # noqa: E402

sc = SCENARIOS[sid]

# ---- seed the roster before the server code loads it -------------------------------------
targets = sc["targets"]
recency = sc["recency"] if isinstance(sc["recency"], list) else [sc["recency"]] * len(targets)
others = filler(sc["size"] - len(targets), seed=zlib.crc32(sid.encode()))
old = [t for t, r in zip(targets, recency) if r == "old"]
recent = [t for t, r in zip(targets, recency) if r == "recent"]
ordered = old + others + recent  # least recently used first
base = datetime(2026, 10, 1, 9, 0, 0)
stamp = {name: (base + timedelta(minutes=i)).isoformat() for i, (name, _) in enumerate(ordered)}
now = (base + timedelta(minutes=len(ordered) + 5)).isoformat()
for name in sc["ran_recently"]:
    if IMPL == "fork":  # this fork counts any run as use; the original doesn't
        stamp[name] = now

data = root / "server" / "data" / "execution_agents"
data.mkdir(parents=True, exist_ok=True)
if IMPL == "original":  # the original roster is a plain list of names
    body = [n for n, _ in ordered]
else:
    body = [dict(name=n, description=d, created_at=stamp[n], last_used=stamp[n]) for n, d in ordered]
(data / "roster.json").write_text(json.dumps(body))

import os  # noqa: E402
from server.config import get_settings  # noqa: E402
_settings = get_settings()
for field in ("interaction_agent_model", "execution_agent_model"):
    if hasattr(_settings, field):
        setattr(_settings, field, os.environ["EVAL_MODEL"])
from server.agents.interaction_agent import runtime as rt  # noqa: E402
from server.agents.interaction_agent import tools  # noqa: E402
from server.services.conversation import get_conversation_log  # noqa: E402
from server.services.execution import get_agent_roster, get_execution_agent_logs  # noqa: E402

logs = get_execution_agent_logs()
for n, d in ordered:
    logs.record_request(n, d)


conv = get_conversation_log()
for tag, payload in sc["history"]:
    if tag == "user_message":
        conv.record_user_message(payload)
    elif tag == "poke_reply":
        conv.record_reply(payload)
    else:
        name, text = payload
        conv.record_agent_message(f"[SUCCESS] {name}: {text}")

roster = get_agent_roster()
roster.load()
before = set(roster.get_agents())

# ---- instrument -------------------------------------------------------------------------------
dispatched = []


async def fake_execute(agent_name, instructions, request_id=None):
    dispatched.append(agent_name)


tools._EXECUTION_BATCH_MANAGER.execute_agent = fake_execute

llm = []
real_completion = rt.request_chat_completion


async def counted(**kwargs):
    for attempt in range(8):  # new accounts get a per-minute cap; back off and retry
        t = time.monotonic()
        try:
            resp = await real_completion(**kwargs)
            break
        except Exception as exc:
            if "(429)" not in str(exc) or attempt == 7:
                raise
            await asyncio.sleep(min(60, 8 * 2 ** attempt))
    usage = resp.get("usage") or {}
    llm.append(dict(seconds=round(time.monotonic() - t, 2), tools=bool(kwargs.get("tools")),
                    prompt=usage.get("prompt_tokens", 0), completion=usage.get("completion_tokens", 0)))
    return resp


rt.request_chat_completion = counted

tool_calls = []
real_exec = rt.InteractionAgentRuntime._execute_tool


def traced(self, call, *a, **k):
    res = real_exec(self, call, *a, **k)
    tool_calls.append(dict(tool=call.name, args={k: v for k, v in call.arguments.items() if k != "instructions"},
                           ok=res.success))
    return res


rt.InteractionAgentRuntime._execute_tool = traced


async def main():
    result = await rt.InteractionAgentRuntime().execute(sc["message"])
    await asyncio.sleep(0.05)  # let dispatch tasks record
    return result


t0 = time.monotonic()
result = asyncio.run(main())
elapsed = round(time.monotonic() - t0, 2)

roster.load()
after = set(roster.get_agents())
created = sorted(after - before)
existing_sent = sorted({d for d in dispatched if d in before})

# ---- grade ------------------------------------------------------------------------------------
kind = sc["expect"][0]
reply = result.response or ""
if kind == "route":
    ok = existing_sent == [sc["expect"][1]] and not created
elif kind == "route_all":
    ok = existing_sent == sorted(sc["expect"][1]) and not created
elif kind == "new":
    ok = len(created) == 1 and not existing_sent
elif kind == "ask":
    ok = not dispatched and not created and "?" in reply  # must actually ask, not just stop
elif kind == "ask_plus_new":  # ask about the ambiguous part; the separate request may go ahead
    ok = not existing_sent and len(created) <= 1 and not any("alice" in c.lower() for c in created) and "?" in reply
elif kind == "ask_or_all":
    ok = not created and (not dispatched and "?" in reply or existing_sent == sorted(sc["expect"][1]))
elif kind == "new_or_route":
    ok = (len(created) == 1 and not existing_sent) or (existing_sent == [sc["expect"][1]] and not created)
else:
    raise ValueError(kind)

print(json.dumps(dict(
    impl=IMPL, scenario=sid, ok=ok, error=result.error, existing_sent=existing_sent, created=created,
    searched=any(c["tool"] == "find_agents" for c in tool_calls),
    refused=[c["tool"] for c in tool_calls if not c["ok"]],
    llm_calls=len(llm), prompt_tokens=sum(c["prompt"] for c in llm),
    completion_tokens=sum(c["completion"] for c in llm), seconds=elapsed,
    says_agent=" agent" in reply.lower(), reply=reply[:300], tool_calls=tool_calls,
)))
