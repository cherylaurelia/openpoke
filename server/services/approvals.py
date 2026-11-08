"""Emails wait for the user's approval, enforced in code rather than asked for in a prompt.

The send, reply and forward tools no longer send. They create a pending action and return.
Only approve() sends, and only once: the record moves pending -> sending under a lock and
is saved before the email goes out, so a double click, a retried request, or the agent
asking again can't send it twice. Emails to the user's own address skip approval, so
"email me a summary every night" still works while they sleep.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..logging_config import logger
from .execution import get_execution_agent_logs

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Approval:
    id: str
    agent_name: str
    tool: str
    arguments: Dict[str, Any]
    to: str
    subject: str
    preview: str
    status: str = "pending"  # pending | sending | sent | failed | declined
    created_at: str = field(default_factory=_now)
    decided_at: str = ""
    result: str = ""


class ApprovalStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._drafts: Dict[str, Dict[str, Any]] = {}  # draft id -> who it's to, subject, body

    def _load(self) -> List[Approval]:
        try:
            return [Approval(**row) for row in json.loads(self._path.read_text())]
        except FileNotFoundError:
            return []
        except Exception as exc:  # a corrupt file must not block the app
            logger.warning(f"Failed to load approvals: {exc}")
            return []

    def _save(self, rows: List[Approval]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump([asdict(r) for r in rows], f, indent=2)
        os.replace(tmp, self._path)

    def remember_draft(self, draft_ids: List[str], to: List[str], subject: str, body: str) -> None:
        """Sending a draft only names its id, so keep each draft's details under its id."""
        for draft_id in draft_ids:
            self._drafts[draft_id] = {"to": to, "subject": subject, "body": body}

    def draft(self, draft_id: str) -> Dict[str, Any]:
        """The draft being sent, or {} if we didn't create it (then it always needs approval)."""
        return self._drafts.get(draft_id, {})

    def request(self, agent_name: str, tool: str, arguments: Dict[str, Any],
                to: str, subject: str, preview: str) -> Approval:
        """Create a pending action, or return the identical one already waiting."""
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.status == "pending" and row.tool == tool and row.arguments == arguments:
                    return row
            approval = Approval(id=f"p{uuid.uuid4().hex[:6]}", agent_name=agent_name, tool=tool,
                                arguments=arguments, to=to, subject=subject, preview=preview)
            rows.append(approval)
            self._save(rows)
            return approval

    def pending(self) -> List[Approval]:
        return [r for r in self._load() if r.status == "pending"]

    def get(self, approval_id: str) -> Optional[Approval]:
        return next((r for r in self._load() if r.id == approval_id), None)

    def _transition(self, approval_id: str, from_status: str, to_status: str, result: str = "") -> Optional[Approval]:
        with self._lock:
            rows = self._load()
            row = next((r for r in rows if r.id == approval_id), None)
            if row is None or row.status != from_status:
                return None
            row.status, row.result = to_status, result
            row.decided_at = row.decided_at or _now()
            self._save(rows)
            return row

    def claim(self, approval_id: str) -> Optional[Approval]:
        """pending -> sending, exactly once. None if it was already claimed or decided."""
        return self._transition(approval_id, "pending", "sending")

    def finish(self, approval_id: str, ok: bool, result: str) -> None:
        self._transition(approval_id, "sending", "sent" if ok else "failed", result[:300])

    def decline(self, approval_id: str) -> Optional[Approval]:
        return self._transition(approval_id, "pending", "declined")

    def interrupted(self) -> List[Approval]:
        """Sends a stopped server left half done. Marked failed, never retried: the email may
        or may not have gone out, and sending it again could make it twice."""
        with self._lock:
            rows = self._load()
            stuck = [r for r in rows if r.status == "sending"]
            for row in stuck:
                row.status, row.result = "failed", "The server stopped while sending. Check your Sent folder."
            if stuck:
                self._save(rows)
            return stuck

    def clear(self) -> None:
        with self._lock:
            self._drafts.clear()
            self._path.unlink(missing_ok=True)


_store = ApprovalStore(_DATA_DIR / "approvals.json")


def get_approval_store() -> ApprovalStore:
    return _store


# Filled in by the Gmail tools module: tool name -> the function that really sends.
SENDERS: Dict[str, Callable[..., Dict[str, Any]]] = {}


def result_error(result: Any) -> Optional[str]:
    """Why a Gmail tool result counts as a failure, or None. Shared with the Activity panel."""
    if isinstance(result, dict) and (result.get("error") or result.get("successful") is False):
        return str(result.get("error") or "failed")
    return None


def draft_ids(result: Any) -> List[str]:
    """Every "id" in a create-draft result. The exact shape varies by Composio version, so
    collect them all; a draft is only ever looked up by the id it is sent with."""
    found: List[str] = []
    if isinstance(result, dict):
        for key, value in result.items():
            if key == "id" and isinstance(value, (str, int)):
                found.append(str(value))
            else:
                found.extend(draft_ids(value))
    elif isinstance(result, list):
        for item in result:
            found.extend(draft_ids(item))
    return found


class NotReady(Exception):
    """Sending can't work right now; checked before an approval is used up."""


def not_ready() -> Optional[str]:
    from .gmail import get_active_gmail_user_id

    if get_active_gmail_user_id():
        return None
    return "Gmail isn't connected, so nothing was sent. The email is still waiting for approval."


def approve(approval_id: str) -> Optional[Approval]:
    """Send a pending action. Returns None if it wasn't pending (so nothing was sent)."""
    reason = not_ready()
    if reason:
        raise NotReady(reason)
    store = get_approval_store()
    approval = store.claim(approval_id)
    if approval is None:
        return None
    logs = get_execution_agent_logs()
    logs.record_action(approval.agent_name, f"User approved: {approval.tool} to {approval.to}")
    # Logged in the runtime's format so the Activity panel shows it like any other step.
    logs.record_action(approval.agent_name, f"Calling {approval.tool} with: {json.dumps(approval.arguments)[:200]}")
    try:
        result = SENDERS[approval.tool](**approval.arguments)
        error = result_error(result)
    except Exception as exc:  # the claim is kept: never retried automatically
        result, error = {"error": str(exc)}, str(exc)
    logs.record_tool_response(approval.agent_name, approval.tool,
                              f"Error: {error}" if error else json.dumps(result, default=str)[:500])
    store.finish(approval_id, ok=error is None, result=error or "sent")
    if error is None:
        from . import followups

        followups.on_sent(approval, result)  # start waiting for their reply
    return store.get(approval_id)


def recover_interrupted() -> None:
    """At startup: sends cut off by a stop show as failed in the panel, never resent."""
    for approval in get_approval_store().interrupted():
        get_execution_agent_logs().record_tool_response(approval.agent_name, approval.tool, f"Error: {approval.result}")


def decline(approval_id: str) -> Optional[Approval]:
    approval = get_approval_store().decline(approval_id)
    if approval:
        from . import followups

        get_execution_agent_logs().record_action(approval.agent_name, f"User declined: {approval.tool} to {approval.to}")
        followups.on_declined(approval)
    return approval


def report_to_poke(approval: Approval) -> None:
    """Tell the interaction agent what happened, so the chat stays in sync with the panel."""
    from ..agents.interaction_agent.runtime import InteractionAgentRuntime

    outcome = {"sent": f"The user approved it and the email to {approval.to} was sent.",
               "failed": f"The user approved it but sending to {approval.to} failed: {approval.result}",
               "declined": f"The user declined sending the email to {approval.to}. Nothing was sent."}
    status = "SUCCESS" if approval.status == "sent" else "FAILED"
    message = f"[{status}] {approval.agent_name}: {outcome.get(approval.status, approval.status)}"

    async def run() -> None:
        try:
            await InteractionAgentRuntime().handle_agent_message(message)
        except Exception as exc:  # pragma: no cover - chat sync is best effort
            logger.warning(f"Could not report approval to Poke: {exc}")

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass
