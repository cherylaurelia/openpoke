"""Sending email needs the user's approval, enforced in code, and happens at most once."""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from server.agents.execution_agent.tools import gmail
from server.agents.interaction_agent import tools as poke_tools
from server.services import activity, approvals
from server.services.execution import log_store
from server.services.execution import roster as roster_module
from server.services.execution.log_store import ExecutionAgentLogStore
from server.services.execution.roster import AgentRoster


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = approvals.ApprovalStore(tmp_path / "approvals.json")
    monkeypatch.setattr(approvals, "_store", store)
    monkeypatch.setattr(log_store, "_execution_agent_logs", ExecutionAgentLogStore(tmp_path / "logs"))
    roster = AgentRoster(tmp_path / "roster.json")
    monkeypatch.setattr(roster_module, "_agent_roster", roster)
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    sent = []
    fake = lambda **kw: sent.append(kw) or {"successful": True}  # noqa: E731
    for name in approvals.SENDERS:
        monkeypatch.setitem(approvals.SENDERS, name, fake)
    ids = iter(f"r-{n}" for n in range(1, 100))
    monkeypatch.setattr(gmail, "gmail_create_draft",
                        lambda **kw: {"successful": True, "data": {"response_data": {"id": next(ids)}}})
    monkeypatch.setattr(gmail, "_own_email", lambda: "me@example.com")
    monkeypatch.setattr(approvals, "report_to_poke", lambda a: None)
    monkeypatch.setattr(approvals, "not_ready", lambda: None)  # Gmail connected
    poke_tools.set_user_turn(None)
    return store, sent


def agent_tools():
    return gmail.build_registry("Email to Alice")


def draft_id(created):
    return created["data"]["response_data"]["id"]


def draft_then_send(registry):
    created = registry["gmail_create_draft"](recipient_email="alice@acme.co", subject="Lunch?", body="Free Friday?")
    return registry["gmail_execute_draft"](draft_id=draft_id(created))


def test_send_waits_for_approval_and_nothing_goes_out(world):
    store, sent = world

    result = draft_then_send(agent_tools())

    assert result["status"] == "awaiting_user_approval" and sent == []
    [pending] = store.pending()
    assert (pending.to, pending.subject, pending.preview) == ("alice@acme.co", "Lunch?", "Free Friday?")


def test_approving_sends_exactly_once_even_when_two_approvals_race(world):
    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()

    threads = [threading.Thread(target=approvals.approve, args=(pending.id,)) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(sent) == 1 and store.get(pending.id).status == "sent"
    assert approvals.approve(pending.id) is None  # a later retry sends nothing


def test_declined_email_can_never_be_sent(world):
    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()

    approvals.decline(pending.id)

    assert approvals.approve(pending.id) is None and sent == []


def test_agent_asking_again_reuses_the_waiting_request(world):
    store, _ = world
    registry = agent_tools()
    draft_then_send(registry)
    registry["gmail_execute_draft"](draft_id="r-1")

    assert len(store.pending()) == 1


def test_email_only_to_the_user_skips_approval(world):
    store, sent = world
    registry = agent_tools()
    created = registry["gmail_create_draft"](recipient_email="ME@example.com", subject="Inbox summary", body="...")

    result = registry["gmail_execute_draft"](draft_id=draft_id(created))

    assert result == {"successful": True} and len(sent) == 1 and store.pending() == []


def test_cc_to_someone_else_still_needs_approval(world):
    store, sent = world

    agent_tools()["gmail_reply_to_thread"](thread_id="t1", recipient_email="me@example.com",
                                            message_body="hi", cc=["boss@acme.co"])

    assert sent == [] and len(store.pending()) == 1


def test_only_a_user_turn_can_approve_from_chat(world):
    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()

    async def agent_turn_then_user_turn():
        poke_tools.set_user_turn(None)  # an agent's report, not the user
        refused = poke_tools.handle_tool_call("approve_pending", {"approval_id": pending.id})
        poke_tools.set_user_turn("yes send it")
        accepted = poke_tools.handle_tool_call("approve_pending", {"approval_id": pending.id})
        await asyncio.sleep(0.2)  # the send runs in the background
        return refused, accepted

    refused, accepted = asyncio.run(agent_turn_then_user_turn())

    assert not refused.success and accepted.success and len(sent) == 1


def test_activity_says_waiting_until_it_is_really_sent(world):
    import json

    store, _ = world
    registry = agent_tools()
    logs = log_store.get_execution_agent_logs()
    logs.record_request("Email to Alice", "Invite Alice to lunch")
    created = registry["gmail_create_draft"](recipient_email="alice@acme.co", subject="Lunch?", body="Free Friday?")
    logs.record_action("Email to Alice", 'Calling gmail_create_draft with: {"recipient_email": "alice@acme.co"}')
    logs.record_tool_response("Email to Alice", "gmail_create_draft", json.dumps(created))
    waiting = registry["gmail_execute_draft"](draft_id=draft_id(created))
    logs.record_action("Email to Alice", f'Calling gmail_execute_draft with: {{"draft_id": "{draft_id(created)}"}}')
    logs.record_tool_response("Email to Alice", "gmail_execute_draft", json.dumps(waiting))
    logs.record_agent_response("Email to Alice", "Draft ready, waiting for approval.")

    texts = [e["text"] for e in activity.task_detail("email-to-alice")["events"]]
    assert "Waiting for your approval to send to alice@acme.co" in texts
    assert not any(t.startswith("Sent") for t in texts)

    approvals.approve(store.pending()[0].id)
    detail = activity.task_detail("email-to-alice")
    texts = [e["text"] for e in detail["events"]]
    assert "You approved sending to alice@acme.co" in texts and "Sent the email to alice@acme.co" in texts
    from datetime import datetime

    [task] = activity.list_tasks(now=datetime(2100, 1, 1))  # long after any request goes stale
    assert task["status"] == "done"  # an approved send isn't an unanswered request


def test_sending_an_older_draft_cannot_borrow_the_self_send_exemption(world):
    # Fault (found in review): the gate checked the latest draft's recipient, not the draft
    # actually sent. Draft to Bob, then a draft to yourself, then send the Bob draft = no approval.
    store, sent = world
    registry = agent_tools()
    to_bob = registry["gmail_create_draft"](recipient_email="bob@evil.example", subject="Inbox dump", body="...")
    registry["gmail_create_draft"](recipient_email="me@example.com", subject="notes", body="x")

    result = registry["gmail_execute_draft"](draft_id=draft_id(to_bob))

    assert result["status"] == "awaiting_user_approval" and sent == []
    assert store.pending()[0].to == "bob@evil.example"  # the card shows who it really goes to


def test_unknown_draft_always_needs_approval(world):
    store, sent = world

    agent_tools()["gmail_execute_draft"](draft_id="r-found-via-list-drafts")

    assert sent == [] and store.pending()[0].to.startswith("(unknown recipient")


def test_approve_route_twice_sends_once(world):
    from server.app import app

    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()
    client = TestClient(app)

    assert client.get("/api/v1/approvals").json()["approvals"][0]["to"] == "alice@acme.co"
    assert client.post(f"/api/v1/approvals/{pending.id}/approve").json()["status"] == "sent"
    assert client.post(f"/api/v1/approvals/{pending.id}/approve").status_code == 409
    assert len(sent) == 1


def test_approval_card_shows_every_recipient(world):
    # Fault (found by all three reviewers): the card showed only "To", so a hidden bcc was
    # approved unseen, and a cc'd draft showed "unknown recipient".
    store, sent = world
    registry = agent_tools()

    registry["gmail_reply_to_thread"](thread_id="t1", recipient_email="me@example.com",
                                      message_body="hi", bcc=["rival@competitor.com"])
    created = registry["gmail_create_draft"](recipient_email="alice@acme.co", subject="Lunch?", body="x",
                                             cc=["boss@acme.co"])
    registry["gmail_execute_draft"](draft_id=draft_id(created))

    assert sent == []
    assert [p.to for p in store.pending()] == ["me@example.com, rival@competitor.com", "alice@acme.co, boss@acme.co"]


def test_approve_while_gmail_is_disconnected_keeps_the_email_waiting(world, monkeypatch):
    # Fault: after a restart Gmail isn't connected until Settings refreshes; approving then
    # used up the approval although nothing was sent.
    from server.app import app

    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()
    monkeypatch.setattr(approvals, "not_ready", lambda: "Gmail isn't connected")

    response = TestClient(app).post(f"/api/v1/approvals/{pending.id}/approve")

    assert response.status_code == 503 and sent == []
    assert store.get(pending.id).status == "pending"


def test_send_cut_off_by_a_restart_is_shown_as_failed_and_never_resent(world):
    store, sent = world
    draft_then_send(agent_tools())
    [pending] = store.pending()
    logs = log_store.get_execution_agent_logs()
    logs.record_request("Email to Alice", "Invite Alice")
    store.claim(pending.id)  # the server stops here, mid-send
    logs.record_action("Email to Alice", f"Calling {pending.tool} with: {{}}")

    approvals.recover_interrupted()  # next startup

    assert store.get(pending.id).status == "failed" and sent == []
    assert approvals.approve(pending.id) is None
    from datetime import datetime
    [task] = activity.list_tasks(now=datetime(2100, 1, 1))
    assert task["status"] == "failed"


def test_approved_send_that_fails_is_not_shown_as_done(world, monkeypatch):
    store, _ = world
    logs = log_store.get_execution_agent_logs()
    logs.record_request("Email to Alice", "Invite Alice")
    draft_then_send(agent_tools())
    logs.record_agent_response("Email to Alice", "Ready for your approval.")
    for name in approvals.SENDERS:
        monkeypatch.setitem(approvals.SENDERS, name, lambda **kw: {"error": "Gmail not connected"})

    approvals.approve(store.pending()[0].id)

    from datetime import datetime
    [task] = activity.list_tasks(now=datetime(2100, 1, 1))
    assert task["status"] == "failed" and "Couldn't send" in task["latest"]


def test_clear_history_drops_waiting_emails(world):
    from server.app import app

    store, sent = world
    draft_then_send(agent_tools())

    TestClient(app).delete("/api/v1/chat/history")

    assert store.pending() == [] and sent == []


def test_prompt_lists_only_the_newest_waiting_emails(world):
    from server.agents.interaction_agent import agent as interaction_agent

    store, _ = world
    for i in range(30):
        store.request("Email to Alice", "gmail_reply_to_thread", {"n": i}, f"p{i}@x.co", "Re", "hi")

    rendered = interaction_agent._render_awaiting_approval()

    assert rendered.count("<email ") == interaction_agent.AWAITING_APPROVAL_LIMIT
    assert 'count="25"' in rendered and "p29@x.co" in rendered
