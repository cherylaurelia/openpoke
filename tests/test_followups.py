"""Waiting on: Poke chases replies for you, and only ever nudges through your approval."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.agents.execution_agent.tools import gmail
from server.agents.interaction_agent import tools as poke_tools
from server.services import activity, approvals, followups
from server.services.execution import log_store
from server.services.execution import roster as roster_module
from server.services.execution.log_store import ExecutionAgentLogStore
from server.services.execution.roster import AgentRoster

NOON = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)  # midday in Toronto, outside quiet hours


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = approvals.ApprovalStore(tmp_path / "approvals.json")
    monkeypatch.setattr(approvals, "_store", store)
    monkeypatch.setattr(followups, "_store", followups.FollowUpStore(tmp_path / "followups.json"))
    monkeypatch.setattr(log_store, "_execution_agent_logs", ExecutionAgentLogStore(tmp_path / "logs"))
    roster = AgentRoster(tmp_path / "roster.json")
    monkeypatch.setattr(roster_module, "_agent_roster", roster)
    roster.add_agent("Email to Dana", "Claim #4471 update for Dana")
    sent = []
    for name in approvals.SENDERS:
        monkeypatch.setitem(approvals.SENDERS, name, lambda **kw: sent.append(kw) or {"successful": True, "data": {"threadId": "t-1"}})
    monkeypatch.setattr(gmail, "gmail_create_draft", lambda **kw: {"successful": True, "data": {"id": "r-1"}})
    monkeypatch.setattr(gmail, "_own_email", lambda: "me@example.com")
    monkeypatch.setattr(approvals, "report_to_poke", lambda a: None)
    monkeypatch.setattr(approvals, "not_ready", lambda: None)
    clock = [NOON]  # the time the code sees when an email is sent
    monkeypatch.setattr(followups, "_now", lambda: clock[0])
    told = []

    async def tell(message):
        told.append(message)

    monkeypatch.setattr(followups, "_tell_poke", tell)
    replies = []  # what Gmail returns for "from:<them> after:<sent>"
    monkeypatch.setattr(followups, "_replies_since", lambda to, since: list(replies))
    return SimpleNamespace(store=store, sent=sent, told=told, replies=replies, clock=clock)


def email_dana_and_approve():
    registry = gmail.build_registry("Email to Dana")
    registry["gmail_create_draft"](recipient_email="dana@acme.co", subject="Claim #4471", body="It's approved.")
    approval_id = registry["gmail_execute_draft"](draft_id="r-1")["approval_id"]
    approvals.approve(approval_id)


def check(at):
    return asyncio.run(followups.check_once(now=at))


def open_items():
    return [r for r in followups.get_follow_up_store().all() if r.status in followups.OPEN]


def test_an_approved_email_starts_waiting_for_a_reply(world):
    email_dana_and_approve()

    [item] = open_items()
    assert (item.to, item.subject, item.thread_id, item.status) == ("dana@acme.co", "Claim #4471", "t-1", "waiting")


def test_email_to_yourself_is_not_tracked(world):
    registry = gmail.build_registry("Email to Dana")
    registry["gmail_create_draft"](recipient_email="me@example.com", subject="Note", body="x")
    registry["gmail_execute_draft"](draft_id="r-1")

    assert open_items() == []


def test_no_reply_by_the_deadline_drafts_one_nudge_for_approval_and_never_sends_it(world):
    email_dana_and_approve()

    assert check(NOON + timedelta(days=1)) == []  # not due yet: nothing happens
    told = check(NOON + timedelta(days=2, minutes=1))
    again = check(NOON + timedelta(days=2, minutes=30))

    [nudge] = world.store.pending()
    assert nudge.tool == "gmail_reply_to_thread" and nudge.arguments["thread_id"] == "t-1" and nudge.to == "dana@acme.co"
    assert len(world.sent) == 1  # only the original email: the nudge waits for the user
    assert "hasn't replied" in told[0] and again == []  # one nudge, said once


def test_a_reply_closes_it_and_withdraws_an_unsent_nudge(world):
    email_dana_and_approve()
    check(NOON + timedelta(days=2, minutes=1))
    world.replies.append(SimpleNamespace(clean_text="Thanks, got it!", subject="Re: Claim #4471"))

    told = check(NOON + timedelta(days=2, hours=1))

    assert "replied" in told[0] and "Thanks, got it!" in told[0]
    assert open_items() == [] and world.store.pending() == []  # the nudge can no longer be sent


def test_after_one_nudge_it_tells_you_once_and_stops(world):
    email_dana_and_approve()
    check(NOON + timedelta(days=2, minutes=1))
    world.clock[0] = NOON + timedelta(days=2, minutes=2)
    approvals.approve(world.store.pending()[0].id)  # the user said yes to the nudge

    told = check(NOON + timedelta(days=2, minutes=5))
    assert told == []  # nudged: waiting another FOLLOW_UP_AFTER
    told = check(NOON + timedelta(days=4, minutes=3))

    assert "still hasn't replied" in told[0] and open_items() == []
    assert check(NOON + timedelta(days=6)) == []


def test_declining_the_nudge_or_stopping_ends_it(world):
    email_dana_and_approve()
    check(NOON + timedelta(days=2, minutes=1))

    approvals.decline(world.store.pending()[0].id)

    assert open_items() == []


def test_stop_from_chat_withdraws_a_pending_nudge(world):
    email_dana_and_approve()
    check(NOON + timedelta(days=2, minutes=1))
    [item] = open_items()

    result = poke_tools.handle_tool_call("stop_follow_up", {"follow_up_id": item.id})

    assert result.success and open_items() == [] and world.store.pending() == []


def test_quiet_hours_wait_until_morning(world):
    email_dana_and_approve()
    late = datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc)  # 11pm in Toronto, and overdue

    assert check(late) == [] and world.store.pending() == []


def test_panel_and_prompt_show_what_you_are_waiting_on(world):
    from server.agents.interaction_agent import agent as interaction_agent

    email_dana_and_approve()

    [row] = activity.list_follow_ups(now=NOON)
    assert (row["who"], row["status"]) == ("dana@acme.co", "waiting")
    assert 'who="dana@acme.co"' in interaction_agent._render_waiting_on()
    steps = [e["text"] for e in activity.task_detail("email-to-dana")["events"]]
    assert any(s.startswith("Waiting for a reply from dana@acme.co") for s in steps)
