"""Activity panel data: built from code-written logs, not the AI's claims."""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server.services import activity
from server.services.execution import log_store
from server.services.execution import roster as roster_module
from server.services.execution.log_store import ExecutionAgentLogStore
from server.services.execution.roster import AgentRoster
from server.services.triggers.service import TriggerService
from server.services.triggers.store import TriggerStore

NOW = datetime(2026, 11, 7, 12, 0, 0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    roster = AgentRoster(tmp_path / "roster.json")
    logs = ExecutionAgentLogStore(tmp_path / "logs")
    triggers = TriggerService(TriggerStore(tmp_path / "triggers.db"))
    monkeypatch.setattr(roster_module, "_agent_roster", roster)
    monkeypatch.setattr(log_store, "_execution_agent_logs", logs)
    monkeypatch.setattr(activity, "get_trigger_service", lambda: triggers)
    stamps = iter(f"2026-11-07 11:{m:02d}:00" for m in range(60))
    monkeypatch.setattr(log_store, "now_in_user_timezone", lambda fmt: next(stamps))
    return roster, logs, triggers


def alice_sent_then_timed_out(roster, logs):
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    logs.record_request("Email to Alice", "Invite Alice to lunch Friday")
    logs.record_action("Email to Alice", 'Calling gmail_create_draft with: {"recipient_email": "alice@acme.co", "subject": "Lunch"}')
    logs.record_tool_response("Email to Alice", "gmail_create_draft", '{"successful": true, "data": {"id": "d1"}}')
    logs.record_action("Email to Alice", 'Calling gmail_execute_draft with: {"draft_id": "d1"}')
    logs.record_tool_response("Email to Alice", "gmail_execute_draft", '{"successful": true}')
    logs.record_agent_response("Email to Alice", "Error: Timed out after 90 seconds")  # as the batch manager writes it


def test_sent_email_shows_even_when_the_task_failed(world):
    # The correctness point: the AI's run "failed", but the code-recorded send is still shown.
    roster, logs, _ = world
    alice_sent_then_timed_out(roster, logs)

    [task] = activity.list_tasks(now=NOW)
    events = activity.task_detail(task["id"])["events"]

    assert task["status"] == "failed" and task["sent_something"]
    assert task["latest"] == "Sent the email to alice@acme.co, then couldn't finish"
    assert {"kind": "sent", "text": "Sent the email to alice@acme.co"} in [
        {k: e[k] for k in ("kind", "text")} for e in events]


def test_failed_tool_call_is_not_shown_as_done(world):
    roster, logs, _ = world
    roster.add_agent("Reply to Bob", "Invoice question from Bob")
    logs.record_request("Reply to Bob", "Reply to Bob about the invoice")
    logs.record_action("Reply to Bob", 'Calling gmail_reply_to_thread with: {"recipient_email": "bob@x.co"}')
    logs.record_tool_response("Reply to Bob", "gmail_reply_to_thread", "Gmail not connected")

    [event] = [e for e in activity.task_detail("reply-to-bob")["events"] if e["kind"] != "asked"]

    assert event["kind"] == "send_failed" and event["text"].startswith("Couldn't send to bob@x.co")
    assert activity.list_tasks(now=NOW)[0]["status"] == "failed"


def test_unanswered_request_is_working_then_didnt_finish(world):
    roster, logs, _ = world
    roster.add_agent("Flight change", "Move the Tokyo flight")
    logs.record_request("Flight change", "Move my Tokyo flight to Friday")  # logged at 11:00

    assert activity.list_tasks(now=datetime(2026, 11, 7, 11, 5))[0]["status"] == "working"
    stale = activity.list_tasks(now=datetime(2026, 11, 7, 11, 0) + timedelta(minutes=30))[0]
    assert stale["status"] == "failed" and stale["latest"] == "Didn't finish. Ask again in the chat."


def test_reminders_listed_in_plain_english_and_cancel_pauses(world):
    roster, _, triggers = world
    roster.add_agent("Rent reminder", "Monthly rent reminder")
    t = triggers.create_trigger(agent_name="Rent reminder", payload="Remind the user to pay rent",
                                recurrence_rule="FREQ=WEEKLY;BYDAY=FR", start_time="2026-11-14T09:00:00",
                                timezone_name="America/Toronto")

    [r] = activity.list_reminders()
    assert (r["what"], r["repeats"], r["state"]) == ("Remind the user to pay rent", "every Fri", "scheduled")

    assert activity.cancel_reminder(t.id)
    assert activity.list_reminders() == []  # cancelled: no longer coming up
    assert triggers.get_due_triggers(before=datetime(2030, 1, 1).astimezone()) == []  # won't fire
    assert activity.resume_reminder(t.id)  # Undo
    assert [r["state"] for r in activity.list_reminders()] == ["scheduled"]


def test_routes_are_wired(world):
    from server.app import app

    roster, logs, _ = world
    alice_sent_then_timed_out(roster, logs)
    client = TestClient(app)

    assert client.get("/api/v1/activity").json()["tasks"][0]["title"] == "Email to Alice"
    assert client.get("/api/v1/activity/email-to-alice").status_code == 200
    assert client.get("/api/v1/activity/nope").status_code == 404
    assert client.get("/api/v1/reminders").json() == {"reminders": []}
    assert client.post("/api/v1/reminders/999/cancel").status_code == 404


def test_reminder_runs_once_even_if_the_server_restarts_mid_run(world, monkeypatch):
    # Fault (reproduced earlier): the trigger moved forward only after its agent finished,
    # so a restart mid-run fired a one-off reminder twice.
    import asyncio
    from server.agents.execution_agent.runtime import ExecutionResult
    from server.services import trigger_scheduler as ts

    roster, _, triggers = world
    roster.add_agent("Pay bill reminder", "Email the insurer about the bill")
    due = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat()
    triggers.create_trigger(agent_name="Pay bill reminder", payload="Email the insurer", start_time=due)
    runs = []

    class SlowAgent:
        async def execute_agent(self, agent_name, instructions, request_id=None):
            runs.append(agent_name)  # in real life, the email goes out here
            await asyncio.sleep(2)
            return ExecutionResult(agent_name=agent_name, success=True, response="sent")

    monkeypatch.setattr(ts, "ExecutionBatchManager", SlowAgent)

    def scheduler():
        s = ts.TriggerScheduler(poll_interval_seconds=0.05)
        s._service = triggers
        return s

    async def crash_then_restart():
        first = scheduler()
        await first.start()
        await asyncio.sleep(0.3)  # the run has started
        for task in asyncio.all_tasks():
            if task.get_name().startswith("trigger-"):
                task.cancel()  # the process dies mid-run
        await first.stop()
        second = scheduler()
        await second.start()
        await asyncio.sleep(0.5)
        await second.stop()

    asyncio.run(crash_then_restart())

    assert runs == ["Pay bill reminder"]
    [r] = activity.list_reminders()
    assert r["state"] == "didnt_finish"  # visibly cut off, not silently repeated


def test_timed_out_run_shows_as_failed_not_working(world, monkeypatch):
    # Fault (found by adversarial review): a timeout cancelled the run before it logged its
    # end, so the panel said "Working" for 10 minutes after Poke had been told it failed.
    import asyncio
    from unittest.mock import AsyncMock
    from server.agents.execution_agent import batch_manager

    roster, logs, _ = world
    roster.add_agent("Inbox summary", "Summarize the inbox")
    logs.record_request("Inbox summary", "Summarize my inbox")

    class SlowRuntime:
        def __init__(self, agent_name):
            pass

        async def execute(self, instructions):
            await asyncio.sleep(5)

    monkeypatch.setattr(batch_manager, "ExecutionAgentRuntime", SlowRuntime)
    manager = batch_manager.ExecutionBatchManager(timeout_seconds=0.1)
    monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", AsyncMock())

    asyncio.run(manager.execute_agent("Inbox summary", "Summarize my inbox"))

    [task] = activity.list_tasks(now=NOW)
    assert task["status"] == "failed" and task["latest"] == "Took too long and stopped. Ask again."


def test_send_result_is_kept_when_the_agent_runs_another_tool_meanwhile(world):
    # Fault: a tool's result was paired with the most recent call, whatever its tool.
    roster, logs, _ = world
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    logs.record_request("Email to Alice", "Invite Alice")
    logs.record_action("Email to Alice", 'Calling gmail_reply_to_thread with: {"recipient_email": "alice@acme.co"}')
    logs.record_action("Email to Alice", 'Calling task_email_search with: {"search_query": "lunch"}')
    logs.record_tool_response("Email to Alice", "task_email_search", '{"results": []}')
    logs.record_tool_response("Email to Alice", "gmail_reply_to_thread", '{"successful": true}')

    kinds = [e["kind"] for e in activity.task_detail("email-to-alice")["events"]]

    assert "sent" in kinds and activity.list_tasks(now=NOW)[0]["sent_something"]


def test_failed_later_run_is_not_blamed_for_an_earlier_send(world):
    roster, logs, _ = world
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    logs.record_request("Email to Alice", "Reply to Alice")
    logs.record_action("Email to Alice", 'Calling gmail_reply_to_thread with: {"recipient_email": "alice@acme.co"}')
    logs.record_tool_response("Email to Alice", "gmail_reply_to_thread", '{"successful": true}')
    logs.record_agent_response("Email to Alice", "Replied.")
    logs.record_request("Email to Alice", "Search for her answer")
    logs.record_agent_response("Email to Alice", "Error: LLM request failed: 429")

    [task] = activity.list_tasks(now=NOW)

    assert task["status"] == "failed" and task["latest"] == "The AI service is busy. Ask again in a minute."


def test_error_text_that_looks_like_json_is_still_a_failure(world, monkeypatch):
    # Fault (adversarial review): the runtime logged a failed tool's bare error text, so an
    # error that happened to start with "{" or "[" read as a successful send.
    import asyncio
    from unittest.mock import AsyncMock
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime

    roster, logs, _ = world
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    logs.record_request("Email to Alice", "Reply to Alice")

    def broken_reply(**kwargs):
        raise RuntimeError("{'code': 400, 'message': 'Invalid To header'}")

    call = {"id": "1", "function": {"name": "gmail_reply_to_thread",
                                    "arguments": '{"thread_id": "t", "recipient_email": "alice@acme.co", "message_body": "hi"}'}}
    runtime = ExecutionAgentRuntime("Email to Alice")
    runtime.tool_registry = {"gmail_reply_to_thread": broken_reply}
    monkeypatch.setattr(runtime, "_make_llm_call", AsyncMock(side_effect=[
        {"choices": [{"message": {"content": "", "tool_calls": [call]}}]},
        {"choices": [{"message": {"content": "Couldn't reply."}}]},
    ]))

    asyncio.run(runtime.execute("Reply to Alice"))

    kinds = [e["kind"] for e in activity.task_detail("email-to-alice")["events"]]
    assert "send_failed" in kinds and "sent" not in kinds


def test_pasted_line_separator_does_not_hide_a_log_entry(world):
    roster, logs, _ = world
    roster.add_agent("Email to Alice", "Lunch invite to Alice")
    logs.record_request("Email to Alice", "Tell Alice:\u2028see you Friday")

    [event] = activity.task_detail("email-to-alice")["events"]

    assert event["kind"] == "asked" and "see you Friday" in event["text"]


def test_last_run_of_a_bounded_reminder_that_failed_stays_visible(world):
    roster, _, triggers = world
    roster.add_agent("Rent reminder", "Monthly rent reminder")
    t = triggers.create_trigger(agent_name="Rent reminder", payload="Remind the user to pay rent",
                                recurrence_rule="FREQ=DAILY;COUNT=1", start_time="2026-11-14T09:00:00",
                                timezone_name="America/Toronto")
    triggers.schedule_next_occurrence(t, fired_at=datetime(2026, 11, 14, 15).astimezone())  # last one
    triggers.record_failure(t, "Timeout")

    [r] = activity.list_reminders()

    assert (r["state"], r["last_error"]) == ("didnt_finish", "Took too long and stopped. Ask again.")


def test_panel_refresh_never_replaces_the_live_roster(world):
    # Fault: the panel reloaded roster.json from a worker thread; a stale read swapped in over
    # an agent created at the same moment, and the next save deleted it from disk.
    roster, logs, _ = world
    roster.add_agent("Old agent", "Something old")
    stale = roster._roster_path.read_text()
    roster.add_agent("New agent", "Just created")
    roster._roster_path.write_text(stale)  # what a reader that saw the file a moment ago holds

    activity.list_tasks(now=NOW)
    activity.list_reminders()
    activity.task_detail("old-agent")

    assert roster.get("New agent") is not None


def test_daylight_saving_jump_does_not_make_a_running_task_look_failed(world, monkeypatch):
    # Fault (adversarial review): 01:59 -> 03:00 on the spring-forward night is 1 real minute,
    # but the wall clock said 61, so a task still running showed "Didn't finish".
    from zoneinfo import ZoneInfo
    from server.utils import timezones

    roster, logs, _ = world
    monkeypatch.setattr(timezones, "resolve_user_timezone", lambda default="UTC": ZoneInfo("America/Toronto"))
    monkeypatch.setattr(log_store, "now_in_user_timezone", lambda fmt: "2026-03-08 01:59:00")
    roster.add_agent("Flight change", "Move the Tokyo flight")
    logs.record_request("Flight change", "Move my Tokyo flight to Friday")

    [task] = activity.list_tasks(now=datetime(2026, 3, 8, 3, 0, 20))

    assert task["status"] == "working"


def test_timeline_lines_are_short_and_plain(world):
    # Seen using the app: Poke's instructions (trigger ids, UTC times) showed word for word.
    roster, logs, _ = world
    roster.add_agent("Reminder - water", "Water reminder")
    logs.record_request("Reminder - water", "Set a reminder for the user to drink water in 2 minutes. "
                        "Current time is 19:28; store it as 2026-11-08T00:30:00Z and report the trigger ID.")
    logs.record_agent_response("Reminder - water", "The user has a reminder at 7:30pm. It is trigger 1, stored as 00:30Z.")

    texts = [e["text"] for e in activity.task_detail("reminder-water")["events"]]

    assert texts == ["Set a reminder for you to drink water in 2 minutes.",
                     "You have a reminder at 7:30pm. It is trigger 1, stored as 00:30Z."]  # result in full
    assert "00:30:00Z" in json.dumps(activity.task_detail("reminder-water")["technical"])  # full text kept


def test_cancelled_reminder_has_no_next_run(world):
    # Seen using the app: cancelling left the next date in place, and the agent told the user
    # it couldn't promise the reminder wouldn't fire.
    roster, _, triggers = world
    t = triggers.create_trigger(agent_name="Reminder - water", payload="Send Alice Wong the contract",
                                recurrence_rule="FREQ=WEEKLY;BYDAY=MO", start_time="2026-11-09T10:00:00",
                                timezone_name="America/Toronto")

    done = triggers.update_trigger(t.id, agent_name="Reminder - water", status="completed")

    assert done.next_trigger is None and activity.list_reminders() == []


def test_errors_say_what_to_do_next(world):
    # The raw OpenRouter 402 JSON showed in the timeline with no next step (interface review).
    roster, logs, _ = world
    roster.add_agent("Inbox", "Inbox summary")
    logs.record_request("Inbox", "Summarize the inbox")
    logs.record_agent_response("Inbox", "Error: OpenRouter request failed (402): {'message': 'exceed your available credits'}")

    [_, failed] = activity.task_detail("inbox")["events"]

    assert failed == {"at": failed["at"], "kind": "run_failed",
                      "text": "Out of AI credit. Add credit to the OpenRouter account, then ask again."}


def test_results_are_plain_text_addressed_to_you(world):
    # Seen using the app: the agent's Markdown and third-person grammar showed as-is.
    roster, logs, _ = world
    roster.add_agent("Email to Self", "Note to self")
    logs.record_request("Email to Self", "The user confirmed. Send the draft.")
    logs.record_agent_response("Email to Self", "I sent it.\n\n- **To:** me@x.co\n- **Body:** ```\nHi\n```\n"
                               "I'll only send it if the user explicitly confirms. The user sent it to their own address.")

    asked, result = [e["text"] for e in activity.task_detail("email-to-self")["events"]]

    assert asked == "You confirmed."
    assert result == ("I sent it. • To: me@x.co • Body: Hi I'll only send it if you explicitly confirm. "
                      "You sent it to your own address.")
