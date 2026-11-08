"""Waiting on: Poke remembers who owes you a reply and chases them, without sending on its own.

After you approve an email to someone else, Poke waits for their reply. If none comes within
FOLLOW_UP_AFTER (2 days by default), it drafts one short nudge as a reply in the same thread
and asks you about it, through the same approval gate as every other email. It nudges once:
if there is still no reply after another FOLLOW_UP_AFTER, it tells you once and stops. A reply
at any point closes it. The checking is plain code (one Gmail search per open item), not the
AI, so it costs nothing and does the same thing every time.

    waiting -> nudge_ready (nudge waiting for your OK) -> nudged -> given_up
    any open state -> replied | stopped
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_config import logger
from .execution import get_execution_agent_logs

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FOLLOW_UP_AFTER = timedelta(minutes=float(os.getenv("OPENPOKE_FOLLOW_UP_AFTER_MINUTES", 2 * 24 * 60)))
CHECK_EVERY_SECONDS = float(os.getenv("OPENPOKE_FOLLOW_UP_CHECK_SECONDS", 300))
# No messages from 10pm to 8am in your timezone; checks wait until morning. "off" disables (for testing).
_quiet_env = os.getenv("OPENPOKE_FOLLOW_UP_QUIET_HOURS", "22-8")
QUIET_HOURS = None if _quiet_env == "off" else tuple(int(h) for h in _quiet_env.split("-"))
OPEN = ("waiting", "nudge_ready", "nudged")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FollowUp:
    id: str
    agent_name: str
    to: str  # who owes the reply
    subject: str
    thread_id: str
    sent_at: str  # ISO, UTC
    due_at: str  # when to nudge, or, after a nudge, when to give up
    status: str = "waiting"
    nudge_approval_id: str = ""
    reply_preview: str = ""
    updated_at: str = ""


class FollowUpStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> List[FollowUp]:
        try:
            return [FollowUp(**row) for row in json.loads(self._path.read_text())]
        except FileNotFoundError:
            return []
        except Exception as exc:  # a corrupt file must not block the app
            logger.warning(f"Failed to load follow-ups: {exc}")
            return []

    def _save(self, rows: List[FollowUp]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump([asdict(r) for r in rows], f, indent=2)
        os.replace(tmp, self._path)

    def all(self) -> List[FollowUp]:
        return self._load()

    def add(self, item: FollowUp) -> None:
        with self._lock:
            rows = [r for r in self._load() if not (r.status in OPEN and r.to == item.to and r.thread_id == item.thread_id)]
            rows.append(item)  # a newer email in the same conversation replaces the older wait
            self._save(rows)

    def update(self, item_id: str, **fields: Any) -> Optional[FollowUp]:
        with self._lock:
            rows = self._load()
            row = next((r for r in rows if r.id == item_id), None)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = _now().isoformat(timespec="seconds")
            self._save(rows)
            return row

    def clear(self) -> None:
        with self._lock:
            self._path.unlink(missing_ok=True)


_store = FollowUpStore(_DATA_DIR / "followups.json")


def get_follow_up_store() -> FollowUpStore:
    return _store


def _log(agent_name: str, text: str) -> None:
    get_execution_agent_logs().record_action(agent_name, f"Follow-up: {text}")


def _day(iso: str) -> str:
    from ..utils.timezones import convert_to_user_timezone

    return convert_to_user_timezone(datetime.fromisoformat(iso)).strftime("%a %b %-d")


def on_sent(approval: Any, result: Dict[str, Any]) -> None:
    """Called after an approved email went out. Start waiting for a reply, or record a nudge."""
    store = get_follow_up_store()
    nudged = next((r for r in store.all() if r.nudge_approval_id == approval.id), None)
    if nudged:
        row = store.update(nudged.id, status="nudged", due_at=(_now() + FOLLOW_UP_AFTER).isoformat(timespec="seconds"))
        if row:
            _log(row.agent_name, f"Nudged {row.to}. Waiting until {_day(row.due_at)}.")
        return
    to = str(approval.arguments.get("recipient_email") or approval.to.split(", ")[0])
    if "@" not in to:
        return  # a draft we didn't see created: we don't know who to wait on
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    thread_id = str(approval.arguments.get("thread_id") or data.get("threadId") or data.get("thread_id") or "")
    sent = _now()
    item = FollowUp(
        id=f"f{uuid.uuid4().hex[:6]}", agent_name=approval.agent_name, to=to, subject=approval.subject,
        thread_id=thread_id, sent_at=sent.isoformat(timespec="seconds"),
        due_at=(sent + FOLLOW_UP_AFTER).isoformat(timespec="seconds"),
    )
    store.add(item)
    _log(item.agent_name, f"Waiting for a reply from {to}. Checking in {_day(item.due_at)}.")


def on_declined(approval: Any) -> None:
    """Declining the nudge means: stop chasing this one."""
    store = get_follow_up_store()
    row = next((r for r in store.all() if r.nudge_approval_id == approval.id and r.status in OPEN), None)
    if row:
        store.update(row.id, status="stopped")
        _log(row.agent_name, f"Stopped following up with {row.to}.")


def stop(item_id: str) -> Optional[FollowUp]:
    store = get_follow_up_store()
    row = next((r for r in store.all() if r.id == item_id), None)
    if row is None or row.status not in OPEN:
        return None
    if row.nudge_approval_id:  # a nudge still waiting for approval must not be sendable afterwards
        from . import approvals

        approvals.get_approval_store().decline(row.nudge_approval_id)
    row = store.update(item_id, status="stopped")
    if row:
        _log(row.agent_name, f"Stopped following up with {row.to}.")
    return row


def _nudge_text(subject: str) -> str:
    about = "" if subject in ("", "Reply in thread") or subject.startswith("Forward of") else f' about "{subject}"'
    return f"Hi,\n\nJust following up on my email below{about}. Let me know when you get a chance.\n\nThanks!"


def _replies_since(to: str, since: datetime) -> List[Any]:
    """Emails from `to` since `since`, newest first. Raises if Gmail can't be reached."""
    from .gmail.client import execute_gmail_tool, get_active_gmail_user_id
    from .gmail.processing import EmailTextCleaner, parse_gmail_fetch_response

    user_id = get_active_gmail_user_id()
    if not user_id:
        raise RuntimeError("Gmail isn't connected")
    query = f"from:{to} after:{int(since.timestamp())}"
    raw = execute_gmail_tool("GMAIL_FETCH_EMAILS", user_id, arguments={"query": query, "include_payload": True, "max_results": 3})
    emails, _ = parse_gmail_fetch_response(raw, query=query, cleaner=EmailTextCleaner(max_url_length=60))
    return emails


def _quiet(now: datetime) -> bool:
    from ..utils.timezones import convert_to_user_timezone

    if QUIET_HOURS is None:
        return False
    hour = convert_to_user_timezone(now).hour
    start, end = QUIET_HOURS
    return hour >= start or hour < end


async def check_once(now: Optional[datetime] = None) -> List[str]:
    """One pass over open follow-ups. Returns the messages sent to Poke (for tests and logs)."""
    from . import approvals

    now = now or _now()
    if _quiet(now):
        return []
    store = get_follow_up_store()
    told: List[str] = []
    for row in [r for r in store.all() if r.status in OPEN]:
        sent_at = datetime.fromisoformat(row.sent_at)
        try:
            replies = await asyncio.to_thread(_replies_since, row.to, sent_at - timedelta(minutes=1))
        except Exception as exc:
            logger.info(f"Follow-up check skipped for {row.to}: {exc}")
            continue
        if replies:
            preview = " ".join((replies[0].clean_text or replies[0].subject or "").split())[:160]
            store.update(row.id, status="replied", reply_preview=preview)
            if row.nudge_approval_id:
                approvals.get_approval_store().decline(row.nudge_approval_id)  # no longer needed
            _log(row.agent_name, f"{row.to} replied.")
            told.append(f'{row.to} replied to "{row.subject}". Their reply starts: "{preview}"')
            continue
        if now < datetime.fromisoformat(row.due_at):
            continue
        since = _day(row.sent_at)
        if row.status == "waiting" and row.thread_id:
            body = _nudge_text(row.subject)
            nudge = approvals.get_approval_store().request(
                row.agent_name, "gmail_reply_to_thread",
                {"thread_id": row.thread_id, "recipient_email": row.to, "message_body": body},
                row.to, f"Following up: {row.subject}", body,
            )
            store.update(row.id, status="nudge_ready", nudge_approval_id=nudge.id)
            _log(row.agent_name, f"No reply from {row.to} since {since}. Drafted a nudge for your OK.")
            told.append(f'{row.to} hasn\'t replied to "{row.subject}" since {since}. A short nudge is drafted and '
                        f"waiting in <awaiting_approval> (id {nudge.id}). Ask the user whether to send it.")
        elif row.status in ("waiting", "nudged"):
            store.update(row.id, status="given_up")
            _log(row.agent_name, f"Still no reply from {row.to}. Stopped following up.")
            told.append(f'{row.to} still hasn\'t replied to "{row.subject}" (sent {since}). '
                        "Tell the user once; you've stopped following up.")
    for message in told:
        await _tell_poke(message)
    return told


async def _tell_poke(message: str) -> None:
    from ..agents.interaction_agent.runtime import InteractionAgentRuntime

    try:
        await InteractionAgentRuntime().handle_agent_message(f"Follow-up notification:\n{message}")
    except Exception as exc:  # pragma: no cover - chat sync is best effort
        logger.warning(f"Could not tell Poke about a follow-up: {exc}")


class FollowUpWatcher:
    """Runs check_once every CHECK_EVERY_SECONDS while the server is up."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="follow-up-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await check_once()
            except Exception as exc:  # pragma: no cover - keep the loop alive
                logger.warning(f"Follow-up check failed: {exc}")
            await asyncio.sleep(CHECK_EVERY_SECONDS)


_watcher = FollowUpWatcher()


def get_follow_up_watcher() -> FollowUpWatcher:
    return _watcher
