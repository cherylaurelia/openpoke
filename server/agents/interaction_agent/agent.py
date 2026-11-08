"""Interaction agent helpers for prompt construction."""

from html import escape
from pathlib import Path
from typing import Dict, List

from ...services import approvals, followups
from ...services.execution import get_agent_roster

_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()


# Load and return the pre-defined system prompt from markdown file
def build_system_prompt() -> str:
    """Return the static system prompt for the interaction agent."""
    return SYSTEM_PROMPT


# Build structured message with conversation history, active agents, and current turn
def prepare_message_with_history(
    latest_text: str,
    transcript: str,
    message_type: str = "user",
) -> List[Dict[str, str]]:
    """Compose a message that bundles history, roster, and the latest turn."""
    sections: List[str] = []

    sections.append(_render_conversation_history(transcript))
    sections.append(f"<active_agents>\n{_render_active_agents()}\n</active_agents>")
    waiting = _render_awaiting_approval()
    if waiting:
        sections.append(f"<awaiting_approval>\n{waiting}\n</awaiting_approval>")
    waiting_on = _render_waiting_on()
    if waiting_on:
        sections.append(f"<waiting_on>\n{waiting_on}\n</waiting_on>")
    sections.append(_render_current_turn(latest_text, message_type))

    content = "\n\n".join(sections)
    return [{"role": "user", "content": content}]


# Format conversation transcript into XML tags for LLM context
def _render_conversation_history(transcript: str) -> str:
    history = transcript.strip()
    if not history:
        history = "None"
    return f"<conversation_history>\n{history}\n</conversation_history>"


# Show only the most recently used agents, so the prompt stops growing with the roster.
# GUESS: tune with the routing evals.
RECENT_AGENT_LIMIT = 20


# Format recently used execution agents into XML tags for LLM awareness, newest first
def _render_active_agents() -> str:
    roster = get_agent_roster()
    roster.load()
    agents = roster.recent(RECENT_AGENT_LIMIT)

    if not agents:
        return "None"

    rendered: List[str] = []
    for agent in agents:
        name = escape(agent.name, quote=True)
        description = escape(agent.description, quote=True)
        rendered.append(f'<agent name="{name}" description="{description}" />')

    hidden = len(roster) - len(agents)
    if hidden > 0:
        rendered.append(
            f'<older_agents count="{hidden}">Not shown. Use find_agents to search them.</older_agents>'
        )

    return "\n".join(rendered)


# Like the agent list, only the newest few go in the prompt, so unapproved emails (say, from a
# nightly reminder nobody answers) can't grow it without limit. The panel still shows them all.
AWAITING_APPROVAL_LIMIT = 5


def _render_awaiting_approval() -> str:
    """Emails the user hasn't approved yet, so Poke can match a "yes, send it" to one."""
    pending = approvals.get_approval_store().pending()
    rows = []
    for a in pending[-AWAITING_APPROVAL_LIMIT:]:
        rows.append(
            f'<email id="{escape(a.id, quote=True)}" to="{escape(a.to, quote=True)}" '
            f'subject="{escape(a.subject, quote=True)}">{escape(a.preview[:200], quote=False)}</email>'
        )
    hidden = len(pending) - len(rows)
    if hidden > 0:
        rows.insert(0, f'<older count="{hidden}">Not shown. The user can see them in the Activity panel.</older>')
    return "\n".join(rows)


def _render_waiting_on() -> str:
    """Conversations the user is waiting on a reply for, newest first, capped like the agent list."""
    rows = [r for r in followups.get_follow_up_store().all() if r.status in followups.OPEN][-AWAITING_APPROVAL_LIMIT:]
    return "\n".join(
        f'<reply_from id="{escape(r.id, quote=True)}" who="{escape(r.to, quote=True)}" '
        f'subject="{escape(r.subject, quote=True)}" sent="{r.sent_at[:10]}" status="{r.status}"/>'
        for r in reversed(rows)
    )


# Wrap the current message in appropriate XML tags based on sender type
def _render_current_turn(latest_text: str, message_type: str) -> str:
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    body = latest_text.strip()
    return f"<{tag}>\n{body}\n</{tag}>"
