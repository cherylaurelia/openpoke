"""Tool definitions for interaction agent."""

import asyncio
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from ...logging_config import logger
from ...services.conversation import get_conversation_log
from ...services import approvals, followups
from ...services.execution import AgentRoster, get_agent_roster, get_execution_agent_logs
from ...services.execution.roster import AgentRecord, Match, _words
from ..execution_agent.batch_manager import ExecutionBatchManager
from .agent import RECENT_AGENT_LIMIT


@dataclass
class ToolResult:
    """Standardized payload returned by interaction-agent tools."""

    success: bool
    payload: Any = None
    user_message: Optional[str] = None
    recorded_reply: bool = False

# Tool schemas for OpenRouter
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": "Deliver instructions to an execution agent. To reuse an agent, pass its exact name from <active_agents> or find_agents. To start a new agent, also pass a description. If the name is unknown, or the new agent looks like one that already exists, nothing is sent and the closest matches are returned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Existing agent's name, or a short name for a new agent describing the work it owns (e.g., 'Vercel Job Offer', 'Email to Sharanjeet')."
                    },
                    "instructions": {"type": "string", "description": "Instructions for the agent to execute."},
                    "description": {
                        "type": "string",
                        "description": "Only when starting a new agent: one sentence on the work it owns and who is involved (e.g., 'Lunch invite to Alice Chen for Friday, Bob also coming'). Used to find this agent later.",
                    },
                    "force_new": {
                        "type": "boolean",
                        "description": "Only after a lookalike warning, when this really is separate work from the suggested agents.",
                    },
                },
                "required": ["agent_name", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_pending",
            "description": "Send an email listed in <awaiting_approval>, only because the user just clearly said yes to that exact email. Never use it on your own initiative or for an agent's message.",
            "parameters": {
                "type": "object",
                "properties": {"approval_id": {"type": "string", "description": "The id from <awaiting_approval>, e.g. 'p3f9a1c'."}},
                "required": ["approval_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decline_pending",
            "description": "Drop an email listed in <awaiting_approval> because the user said not to send it.",
            "parameters": {
                "type": "object",
                "properties": {"approval_id": {"type": "string", "description": "The id from <awaiting_approval>."}},
                "required": ["approval_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_follow_up",
            "description": "Stop waiting on a reply listed in <waiting_on> (no more checks or nudges), when the user asks.",
            "parameters": {
                "type": "object",
                "properties": {"follow_up_id": {"type": "string", "description": "The id from <waiting_on>, e.g. 'f1a2b3c'."}},
                "required": ["follow_up_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_agents",
            "description": "Search all execution agents, including older ones not shown in <active_agents>. Use key words like people, companies, or topics (e.g., 'alice lunch').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Key words to search agent names and descriptions."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Deliver a natural-language response directly to the user. Use this for updates, confirmations, or any assistant response the user should see immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Plain-text message that will be shown to the user and recorded in the conversation log.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_draft",
            "description": "Record an email draft so the user can review the exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email for the draft.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject for the draft.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body content (plain text).",
                    },
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait silently when a message is already in conversation history to avoid duplicating responses. Adds a <wait> log entry that is not visible to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why waiting (e.g., 'Message already sent', 'Draft already created').",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

_EXECUTION_BATCH_MANAGER = ExecutionBatchManager()

# A new agent whose name words carry at least this share of weight in an existing
# agent counts as a lookalike. GUESS: tune with the routing evals.
LOOKALIKE_COVERAGE = 0.7
# Once some agents are hidden, a new agent sharing this many specific words with an
# existing one is also a lookalike. GUESS, set from the head-to-head bounce/duplicate cases.
SPECIFIC_WORDS_FOR_LOOKALIKE = 2
# "Which one?" lists at most this many agents; a bigger tie is a shared word, not a choice.
ASK_WHICH_MAX = 4


def _describe_match(match: Match) -> dict:
    return {
        "agent_name": match.agent.name,
        "description": match.agent.description,
        "last_used": match.agent.last_used,
        "matched_words": match.matched,
    }


def _refuse_new_agent(
    roster: AgentRoster, agent_name: str, description: str, force_new: bool
) -> Optional[ToolResult]:
    """Return a refusal if this new agent shouldn't be created yet, else None."""
    new_words = _words(f"{agent_name} {description}")
    for words, agents in _guesses(roster):
        # A new agent for work the user's words leave open is the guess again under a new
        # name (seen live: "Alice Chen inbox check" instead of asking which Alice). A big
        # group isn't that: "book" in 17 book clubs doesn't make a haircut booking a guess.
        if new_words & words and len(agents) <= ASK_WHICH_MAX:
            return _ask_which(agents)
    matches = roster.search(agent_name, limit=3)
    if not description:
        return ToolResult(
            success=False,
            payload={
                "error": f"No agent named '{agent_name}'. Nothing was sent.",
                "closest_matches": [_describe_match(m) for m in matches],
                "next_step": "To reuse one, call again with its exact agent_name. "
                "To start a new agent, call again with a description.",
            },
        )
    # Two shared words at least, so one coincidence ("Email to Bob" vs "Email to Alice") isn't one.
    lookalikes = [m for m in matches if m.coverage >= LOOKALIKE_COVERAGE and len(m.matched) >= 2]
    if len(roster) > RECENT_AGENT_LIMIT:
        # Some agents are hidden from Poke, so it can't spot a duplicate itself. Sharing two
        # specific words ("lisbon" + "hotel") with an existing agent is enough to check first;
        # one shared word ("book", "email") is too often a coincidence.
        seen = {m.agent.key for m in lookalikes}
        lookalikes += [
            m for m in roster.search(f"{agent_name} {description}", limit=3)
            if len(m.specific) >= SPECIFIC_WORDS_FOR_LOOKALIKE and m.agent.key not in seen
        ]
    if lookalikes and not force_new:
        return ToolResult(
            success=False,
            payload={
                "error": f"'{agent_name}' looks like an existing agent. Nothing was sent.",
                "closest_matches": [_describe_match(m) for m in lookalikes],
                "next_step": "Reuse the matching agent by its exact agent_name. "
                "If this is separate work, call again with force_new=true.",
            },
        )
    return None


# The user's message and recent chat for the turn being handled, or None on agent turns.
# Set by the runtime; read by the ambiguity check below.
_USER_TURN: ContextVar[Optional[tuple[str, str]]] = ContextVar("user_turn", default=None)
_RECENT_CHAT_CHARS = 3000


def set_user_turn(user_message: Optional[str], transcript: str = "") -> None:
    _USER_TURN.set((user_message, transcript[-_RECENT_CHAT_CHARS:]) if user_message else None)


def _guesses(roster: AgentRoster) -> list[tuple[set[str], list[AgentRecord]]]:
    """Parts of the user's message that fit several agents equally, with nothing to tell them apart.

    "did alice reply?" with Alice Chen and Alice Wong: both match only "alice", so it's a guess.
    "did alice chen reply?": only one matches "chen", so it isn't. Each sentence is checked on
    its own, so "did alice reply? also invite bob to lunch" can't borrow "lunch" to pick an
    Alice. If the recent chat names only one of them, the context has already decided.
    """
    turn = _USER_TURN.get()
    if turn is None:
        return []
    user_message, recent_chat = turn
    chat = recent_chat.lower()
    # A word in most agents ("send", "email") names nothing; "send the email" is a follow-up
    # that context resolves. A name shared by a few agents ("alice") is exactly the guess.
    common = max(3, len(roster) // 2)
    found = []
    for sentence in re.split(r"[.?!;\n]+", user_message):
        matches = roster.search(sentence, limit=len(roster))
        if not matches:
            continue
        best = set(matches[0].matched)
        tied = [m.agent for m in matches if set(m.matched) == best]
        if len(tied) < 2 or all(sum(w in m.matched for m in matches) > common for w in best):
            continue
        named = [a for a in tied if a.name.lower() in chat]
        if len(named) == 1:
            continue
        found.append((best, tied))
    return found


def _ask_which(agents: list[AgentRecord]) -> ToolResult:
    return ToolResult(
        success=False,
        payload={
            "error": "Not sent: the user's message fits more than one agent equally and the "
            "conversation doesn't say which. Nothing is being checked, even if you already "
            "told the user you're on it. Now call send_message_to_user with one short "
            "question asking which they mean, in their own terms (the people, threads, or "
            "bookings), never mentioning agents.",
            "candidates": [{"agent_name": a.name, "description": a.description} for a in agents[:ASK_WHICH_MAX]],
        },
    )


# Create or reuse execution agent and dispatch instructions asynchronously
def send_message_to_agent(
    agent_name: str,
    instructions: str,
    description: Optional[str] = None,
    force_new: bool = False,
) -> ToolResult:
    """Send instructions to an execution agent."""
    roster = get_agent_roster()
    roster.load()
    agent = roster.get(agent_name)
    is_new = agent is None

    if is_new:
        description = (description or "").strip()
        refusal = _refuse_new_agent(roster, agent_name, description, force_new)
        if refusal:
            logger.info(f"Refused new agent: {agent_name}")
            return refusal
        agent = roster.add_agent(agent_name, description)
    else:
        for _, agents in _guesses(roster):
            if any(a.key == agent.key for a in agents):
                logger.info(f"Refused ambiguous send to {agent.name}; fits {[a.name for a in agents]}")
                return _ask_which(agents)

    agent_name = agent.name  # the stored spelling, so logs and triggers stay on one agent

    get_execution_agent_logs().record_request(agent_name, instructions)

    logger.info(f"{'Created' if is_new else 'Reused'} agent: {agent_name}")

    async def _execute_async() -> None:
        try:
            result = await _EXECUTION_BATCH_MANAGER.execute_agent(agent_name, instructions)
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"Agent '{agent_name}' completed: {status}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"Agent '{agent_name}' failed: {str(exc)}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("No running event loop available for async execution")
        return ToolResult(success=False, payload={"error": "No event loop available"})

    loop.create_task(_execute_async())

    return ToolResult(
        success=True,
        payload={
            "status": "submitted",
            "agent_name": agent_name,
            "new_agent_created": is_new,
        },
    )


def _decide(approval_id: str, approve: bool) -> ToolResult:
    """Approve or decline from chat. Code only allows this on a turn the user started."""
    if _USER_TURN.get() is None:
        return ToolResult(
            success=False,
            payload={"error": "Only the user can approve or decline, and only by replying. Ask them."},
        )
    store = approvals.get_approval_store()
    pending = store.get(approval_id)
    if pending is None or pending.status != "pending":
        return ToolResult(success=False, payload={"error": f"Nothing is waiting with id {approval_id!r}."})
    if not approve:
        approvals.decline(approval_id)
        return ToolResult(success=True, payload={"status": "declined", "to": pending.to})
    reason = approvals.not_ready()
    if reason:
        return ToolResult(success=False, payload={"error": reason})

    async def send() -> None:
        try:
            done = await asyncio.to_thread(approvals.approve, approval_id)
        except approvals.NotReady:
            return  # Gmail dropped between the check and the send; nothing was sent
        if done:
            approvals.report_to_poke(done)

    asyncio.get_running_loop().create_task(send())
    return ToolResult(success=True, payload={"status": "sending", "to": pending.to})


def find_agents(query: str) -> ToolResult:
    """Search every agent by name and description."""
    roster = get_agent_roster()
    roster.load()
    matches = roster.search(query, limit=5)
    if not matches:
        return ToolResult(
            success=True,
            payload={"matches": [], "note": "No agents match. Try other key words."},
        )
    return ToolResult(success=True, payload={"matches": [_describe_match(m) for m in matches]})


# Send immediate message to user and record in conversation history
def send_message_to_user(message: str) -> ToolResult:
    """Record a user-visible reply in the conversation log."""
    log = get_conversation_log()
    log.record_reply(message)

    return ToolResult(
        success=True,
        payload={"status": "delivered"},
        user_message=message,
        recorded_reply=True,
    )


# Format and record email draft for user review
def send_draft(
    to: str,
    subject: str,
    body: str,
) -> ToolResult:
    """Record a draft update in the conversation log for the interaction agent."""
    log = get_conversation_log()

    message = f"To: {to}\nSubject: {subject}\n\n{body}"

    log.record_reply(message)
    logger.info(f"Draft recorded for: {to}")

    return ToolResult(
        success=True,
        payload={
            "status": "draft_recorded",
            "to": to,
            "subject": subject,
        },
        recorded_reply=True,
    )


# Record silent wait state to avoid duplicate responses
def wait(reason: str) -> ToolResult:
    """Wait silently and add a wait log entry that is not visible to the user."""
    log = get_conversation_log()
    
    # Record a dedicated wait entry so the UI knows to ignore it
    log.record_wait(reason)
    

    return ToolResult(
        success=True,
        payload={
            "status": "waiting",
            "reason": reason,
        },
        recorded_reply=True,
    )


# Return predefined tool schemas for LLM function calling
def get_tool_schemas():
    """Return OpenAI-compatible tool schemas."""
    return TOOL_SCHEMAS


# Route tool calls to appropriate handlers with argument validation and error handling
def handle_tool_call(name: str, arguments: Any) -> ToolResult:
    """Handle tool calls from interaction agent."""
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return ToolResult(success=False, payload={"error": "Invalid arguments format"})

        if name == "send_message_to_agent":
            return send_message_to_agent(**args)
        if name == "find_agents":
            return find_agents(**args)
        if name == "stop_follow_up":
            stopped = followups.stop(args["follow_up_id"])
            return ToolResult(success=bool(stopped), payload={"status": "stopped"} if stopped else
                              {"error": "Nothing open with that id."})
        if name in ("approve_pending", "decline_pending"):
            return _decide(args["approval_id"], approve=name == "approve_pending")
        if name == "send_message_to_user":
            return send_message_to_user(**args)
        if name == "send_draft":
            return send_draft(**args)
        if name == "wait":
            return wait(**args)

        logger.warning("unexpected tool", extra={"tool": name})
        return ToolResult(success=False, payload={"error": f"Unknown tool: {name}"})
    except json.JSONDecodeError:
        return ToolResult(success=False, payload={"error": "Invalid JSON"})
    except TypeError as exc:
        return ToolResult(success=False, payload={"error": f"Missing required arguments: {exc}"})
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("tool call failed", extra={"tool": name, "error": str(exc)})
        return ToolResult(success=False, payload={"error": "Failed to execute"})
