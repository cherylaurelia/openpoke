"""Red tests for the agent-overload fix. Run: .venv/bin/python -m pytest tests -q

Each test names the fault it catches. Before trusting a test, revert the fix it guards
and watch it fail.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.agents.interaction_agent import agent as interaction_agent
from server.agents.interaction_agent import tools
from server.agents.execution_agent import batch_manager
from server.agents.execution_agent.runtime import ExecutionResult
from server.services.execution import log_store
from server.services.execution import roster as roster_module
from server.services.execution.log_store import ExecutionAgentLogStore
from server.services.execution.roster import AgentRoster


@pytest.fixture
def roster(tmp_path, monkeypatch):
    fresh = AgentRoster(tmp_path / "roster.json")
    monkeypatch.setattr(roster_module, "_agent_roster", fresh)
    logs = ExecutionAgentLogStore(tmp_path / "logs")
    monkeypatch.setattr(tools, "get_execution_agent_logs", lambda: logs)
    monkeypatch.setattr(log_store, "_execution_agent_logs", logs)
    dispatch = AsyncMock()
    monkeypatch.setattr(tools._EXECUTION_BATCH_MANAGER, "execute_agent", dispatch)
    fresh.dispatch = dispatch
    fresh.logs = logs
    tools.set_user_turn(None)
    yield fresh
    tools.set_user_turn(None)


def send(**kwargs):
    """Call the tool inside an event loop, the way the server does."""

    async def run():
        result = tools.send_message_to_agent(**kwargs)
        await asyncio.sleep(0)  # let the dispatch task start
        return result

    return asyncio.run(run())


def test_case_variant_reuses_agent_instead_of_duplicating(roster):
    # Fault: "email to alice" created a second agent sharing Alice's log file.
    roster.add_agent("Email to Alice", "Lunch invite to Alice")

    result = send(agent_name="email to alice!", instructions="did she reply?")

    assert result.success and not result.payload["new_agent_created"]
    assert len(roster) == 1
    roster.dispatch.assert_called_once_with("Email to Alice", "did she reply?")


def test_old_roster_with_case_duplicates_loads_as_one_agent(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(["Email to Alice", "email to alice", "Rent reminder"]))

    assert AgentRoster(path).get_agents() == ["Email to Alice", "Rent reminder"]


def test_prompt_lists_recent_agents_only(roster):
    # Fault: every agent ever created was pasted into every prompt.
    for i in range(500):
        roster.add_agent(f"Task {i}", f"Work item number {i}")

    rendered = interaction_agent._render_active_agents()

    assert rendered.count("<agent ") == interaction_agent.RECENT_AGENT_LIMIT
    assert 'count="480"' in rendered


def test_unknown_name_without_description_sends_nothing(roster):
    # Fault: any typo silently created a fresh agent with no history.
    roster.add_agent("Email to Alice", "Lunch invite to Alice")

    result = send(agent_name="Dinner plans", instructions="book a table")

    assert not result.success
    assert result.payload["error"].startswith("No agent named")
    assert len(roster) == 1
    roster.dispatch.assert_not_called()


def test_lookalike_new_agent_is_flagged_until_forced(roster):
    roster.add_agent("Email to Alice", "Lunch invite to Alice")

    flagged = send(agent_name="Alice email", instructions="x", description="Follow up with Alice")
    forced = send(
        agent_name="Alice email", instructions="x", description="Alice's visa paperwork", force_new=True
    )

    assert not flagged.success and len(roster) == 2  # second call created it
    assert forced.success and forced.payload["new_agent_created"]


def test_unrelated_new_agent_is_created_directly(roster):
    # No extra round trip when nothing similar exists.
    roster.add_agent("Email to Alice", "Lunch invite to Alice")

    result = send(agent_name="Email to Bob", instructions="x", description="Invoice question for Bob")

    assert result.success and result.payload["new_agent_created"]


def test_search_weights_rare_words(roster):
    # Fault: plain word overlap ranks all 300 "Email to ..." agents equally on "email".
    for i in range(300):
        roster.add_agent(f"Email to Person {i}", f"Thread with person {i}")
    roster.add_agent("Lunch planning", "Lunch with Alice on Friday")

    top = roster.search("email alice")[0]

    assert top.agent.name == "Lunch planning"


def test_search_folds_plurals(roster):
    roster.add_agent("Invoice question - Bob", "Invoice question for Bob")

    assert roster.search("invoices")[0].agent.name == "Invoice question - Bob"


def test_any_run_keeps_agent_recent(roster, monkeypatch):
    # Fault: a reminder ran an old agent, it reported back, but it wasn't in Poke's list.
    # Every clock reading is a microsecond later and all fall in one second, so this also
    # fails if timestamps lose sub-second precision (ties would put the old agent last).
    import itertools
    from datetime import datetime, timedelta

    ticks = itertools.count(1)

    class OneSecond(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=tz) + timedelta(microseconds=next(ticks))

    monkeypatch.setattr(roster_module, "datetime", OneSecond)
    roster.add_agent("Rent reminder", "Monthly rent reminder")
    for i in range(30):
        roster.add_agent(f"Task {i}", f"Work item {i}")
    fake_runtime = MagicMock()
    fake_runtime.return_value.execute = AsyncMock(return_value=ExecutionResult("Rent reminder", True, "done"))
    monkeypatch.setattr(batch_manager, "ExecutionAgentRuntime", fake_runtime)
    manager = batch_manager.ExecutionBatchManager()
    monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", AsyncMock())

    asyncio.run(manager.execute_agent("Rent reminder", "fire"))

    assert 'name="Rent reminder"' in interaction_agent._render_active_agents()


def test_upgraded_agents_get_description_from_first_request(roster):
    roster.logs.record_request("Dinner plans", "Book a table at Nopa for Friday at 8")
    roster._roster_path.write_text(json.dumps(["Dinner plans"]))
    roster.load()

    assert roster.search("nopa")[0].agent.name == "Dinner plans"


def two_alices(roster):
    roster.add_agent("Email to Alice Chen", "Lunch invite to Alice Chen")
    roster.add_agent("Email to Alice Wong", "Contract review with Alice Wong")


def test_ambiguous_request_is_stopped(roster):
    # Fault: Poke guessed one of two equally fitting agents; the prompt alone didn't stop it.
    two_alices(roster)
    tools.set_user_turn("did alice reply?")

    result = send(agent_name="Email to Alice Chen", instructions="did she reply?")

    assert not result.success
    assert {c["agent_name"] for c in result.payload["candidates"]} == {
        "Email to Alice Chen", "Email to Alice Wong"}
    roster.dispatch.assert_not_called()


def test_distinguishing_word_is_not_stopped(roster):
    two_alices(roster)
    tools.set_user_turn("did alice chen reply?")

    assert send(agent_name="Email to Alice Chen", instructions="x").success


def test_recent_chat_can_decide(roster):
    two_alices(roster)
    chat = "<agent_message>[SUCCESS] Email to Alice Chen: Sent the lunch invite.</agent_message>"
    tools.set_user_turn("did alice reply?", chat)

    assert send(agent_name="Email to Alice Chen", instructions="x").success


def test_agent_turns_are_not_checked(roster):
    two_alices(roster)
    tools.set_user_turn(None)

    assert send(agent_name="Email to Alice Chen", instructions="x").success


def kids_dentist(roster, fillers):
    roster.add_agent("Dentist appointment - kids", "Kids' dental cleaning at Bright Smiles")
    for i in range(fillers):
        roster.add_agent(f"Task {i}", f"Work item {i}")


def test_hidden_duplicate_sharing_a_specific_word_is_flagged(roster):
    # Fault (seen live): the old agent was hidden, Poke didn't search, and a new name with
    # extra words ("reschedule") slipped past the name-only check as a duplicate.
    kids_dentist(roster, fillers=30)

    result = send(agent_name="Kids checkup", instructions="x",
                  description="Reschedule the kids' dental cleaning to next week")

    assert not result.success
    assert result.payload["closest_matches"][0]["agent_name"] == "Dentist appointment - kids"


def test_specific_word_check_is_off_while_every_agent_is_visible(roster):
    # With 20 or fewer agents Poke sees them all; separate work that shares a few words
    # (kids, dental, cleaning) shouldn't cost a round trip.
    kids_dentist(roster, fillers=5)

    result = send(agent_name="Kids haircut", instructions="x",
                  description="Haircut for the kids after their dental cleaning")

    assert result.success


def test_turn_ends_after_a_wait_only_step(roster, tmp_path, monkeypatch):
    # Fault (seen live): the model called wait again and again until the 8-step limit.
    from server.agents.interaction_agent import runtime as rt
    from server.services.conversation.log import ConversationLog

    monkeypatch.setattr(tools, "get_conversation_log", lambda: ConversationLog(tmp_path / "c.log"))
    wait_call = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "1", "function": {"name": "wait", "arguments": '{"reason": "already told them"}'}}]}}]}
    llm = AsyncMock(return_value=wait_call)
    runtime = rt.InteractionAgentRuntime()
    monkeypatch.setattr(runtime, "_make_llm_call", llm)

    asyncio.run(runtime._run_interaction_loop("system", [{"role": "user", "content": "hi"}]))

    assert llm.await_count == 1


def send_in_one_turn(*calls):
    """Several tool calls in one turn (the turn's context carries between them)."""

    async def run():
        results = [tools.send_message_to_agent(**c) for c in calls]
        await asyncio.sleep(0)
        return results

    return asyncio.run(run())


def test_cannot_dodge_an_ambiguity_stop_by_creating_a_new_agent(roster):
    # Fault (seen live): after "which one?", Poke created "Alice Chen inbox check" instead.
    roster.add_agent("Email to Alice Chen", "Lunch invite to Alice Chen")
    roster.add_agent("Alice Chen visa paperwork", "Visa paperwork for Alice Chen")
    tools.set_user_turn("anything new from alice chen?")

    stopped, dodge, unrelated = send_in_one_turn(
        dict(agent_name="Alice Chen visa paperwork", instructions="x"),
        dict(agent_name="Inbox check", instructions="x", description="Check inbox for a reply from Alice Chen"),
        dict(agent_name="Haircut", instructions="x", description="Book a haircut Saturday"),
    )

    assert not stopped.success and not dodge.success
    assert unrelated.success
    assert len(roster) == 3


def test_generic_follow_up_is_not_treated_as_ambiguous(roster):
    # "send the email" names nothing specific; context decides, so don't ask "which one?".
    for i in range(30):
        roster.add_agent(f"Email to Person {i}", f"Send the email thread with person {i}")
    roster.add_agent("Email to Alice Chen", "Send the lunch email to Alice Chen")
    tools.set_user_turn("ok send the email")

    assert send(agent_name="Email to Alice Chen", instructions="send it").success


def test_many_agents_sharing_a_name_still_ask_which(roster):
    # Fault (adversarial review): "alice" in 4+ agents counted as a generic word like "email".
    for who in ("Chen", "Wong", "Park", "Diaz"):
        roster.add_agent(f"Email to Alice {who}", f"Thread with Alice {who}")
    for i in range(5):
        roster.add_agent(f"Task {i}", f"Work item {i}")
    tools.set_user_turn("did alice reply?")

    assert not send(agent_name="Email to Alice Chen", instructions="x").success


def test_other_requests_in_the_message_cannot_pick_an_alice(roster):
    # Fault: "lunch" from the Bob request matched Alice Chen's description and broke the tie.
    two_alices(roster)
    tools.set_user_turn("did alice reply? also invite bob to lunch on friday")

    alice, bob = send_in_one_turn(
        dict(agent_name="Email to Alice Chen", instructions="did she reply?"),
        dict(agent_name="Lunch with Bob", instructions="invite", description="Lunch invite to Bob for Friday"),
    )

    assert not alice.success  # still a guess between the two Alices
    assert bob.success  # separate work in the same message isn't blocked


def test_new_agent_before_asking_cannot_dodge_the_question(roster):
    # Fault: the dodge was only blocked after a refusal, not as Poke's first move.
    two_alices(roster)
    tools.set_user_turn("did alice reply?")

    result = send(agent_name="Alice reply check", instructions="x", description="Check if Alice replied")

    assert not result.success and len(roster) == 2


def test_new_work_sharing_a_word_with_many_agents_is_not_blocked(roster):
    # Fault (live eval): "book a haircut" tied 17 "Book club" agents on "book", and the
    # no-dodging rule refused the new haircut agent as if it were a "which book club?" guess.
    for i in range(17):
        roster.add_agent(f"Book club - Person {i}", f"Book club picks with person {i}")
    for i in range(40):  # so "book" is in a minority of agents, as in a real roster
        roster.add_agent(f"Task {i}", f"Work item {i}")
    tools.set_user_turn("book me a haircut for saturday morning")

    result = send(agent_name="Haircut booking Saturday", instructions="x",
                  description="Book a haircut appointment on Saturday morning")

    assert result.success


def test_reworded_name_with_a_new_word_is_still_a_lookalike(roster):
    # Fault (adversarial review, then live): "status" is in no agent, so it got the highest
    # weight and "Tokyo flight status" looked only half like "Flight to Tokyo - March".
    roster.add_agent("Flight to Tokyo - March", "Flight to Tokyo for the March trip")
    roster.add_agent("Flight to Lisbon - April", "Flight to Lisbon for the April trip")

    result = send(agent_name="Tokyo flight status", instructions="x",
                  description="Check the status of the user's upcoming Tokyo flight")

    assert not result.success
    assert result.payload["closest_matches"][0]["agent_name"] == "Flight to Tokyo - March"


def test_accented_and_non_latin_names_are_searchable_and_checked(roster):
    # Fault (adversarial review): words were a-z only, so "zoe" missed "Zoë", "Иван" was no
    # word at all, and two Josés were never a "which one?".
    roster.add_agent("Note to Zoë", "Thank-you note to Zoë Lee")
    roster.add_agent("Иван Петров - полис", "Страховой полис Ивана Петрова")
    roster.add_agent("Claim for José García", "Claim #4471 for José García")
    roster.add_agent("Claim for José Ruiz", "Claim #5120 for José Ruiz")

    assert roster.search("zoe")[0].agent.name == "Note to Zoë"
    assert roster.search("иван")[0].agent.name == "Иван Петров - полис"
    tools.set_user_turn("any news on jose's claim?")
    assert not send(agent_name="Claim for José García", instructions="x").success
