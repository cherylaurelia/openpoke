"""Plain-English activity for the Activity panel, built from what code recorded.

Every line comes from an execution agent's log, written by code right after each tool call,
not from the AI's own account of what it did. So a task that timed out after sending an
email still shows "Sent to ...", and the AI's closing summary is marked as a summary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .execution import get_agent_roster, get_execution_agent_logs
from .triggers import TriggerRecord, get_trigger_service
from .approvals import result_error

# A request with no response after this long didn't finish (e.g. the server restarted).
STALE_AFTER = timedelta(minutes=10)
# Actions that leave the user's account and can't be taken back.
_SENDS = {"gmail_execute_draft", "gmail_forward_email", "gmail_reply_to_thread"}
_CALL = re.compile(r"^Calling (\w+) with: (.*)$", re.S)


def _arg(raw_args: str, key: str) -> str:
    """Read one field from logged arguments (they may be cut off, so not always valid JSON)."""
    match = re.search(rf'"{key}"\s*:\s*"([^"]*)', raw_args)
    return match.group(1) if match else ""


def _failed(result: str) -> Optional[str]:
    """Why a tool call failed, or None if it succeeded."""
    text = result.strip()
    if text.startswith("Error:"):
        return text[6:].strip() or "unknown error"
    if not text.startswith(("{", "[")):
        return text or "unknown error"  # logs written before failures got the "Error:" prefix
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None  # cut off at 500 chars; fall back to a text check
    if isinstance(data, dict):
        return result_error(data)
    match = re.search(r'"error"\s*:\s*"([^"]+)', text)
    return match.group(1) if match else None


def _plain(text: str) -> str:
    """Agents write Markdown about "the user" for Poke; the panel shows plain text addressed to you."""
    text = re.sub(r"```[a-z]*", "", text)  # code fences
    text = re.sub(r"(\*\*|__|`)", "", text)  # bold, inline code
    text = re.sub(r"(?m)^\s*[-*•]\s+", "• ", text)  # list items, kept apart once lines are joined
    text = " ".join(text.split())
    text = re.sub(r"\bthe user's\b", "your", text, flags=re.I)
    irregular = {"has": "have", "is": "are", "was": "were", "does": "do", "hasn't": "haven't",
                 "isn't": "aren't", "wasn't": "weren't", "doesn't": "don't"}

    def you(m: "re.Match[str]") -> str:  # "the user confirms" -> "you confirm", not "you confirms"
        adverb, verb = m.group(1), m.group(2)  # "the user explicitly confirms"
        if verb.lower() in irregular:
            verb = irregular[verb.lower()]
        elif re.fullmatch(r"[a-z]+(ches|shes|sses|xes|oes)", verb):
            verb = verb[:-2]
        elif re.fullmatch(r"[a-z]+[^s]s", verb) and verb.lower() not in ("its", "this", "us"):
            verb = verb[:-1]
        return f"you {adverb}{verb}"

    text = re.sub(r"\bthe user ((?:\w+ly )?)(\w+'?\w*)", you, text, flags=re.I)
    text = re.sub(r"\bthe user\b", "you", text, flags=re.I)
    text = re.sub(r"\b(you \w+ [^.]*?)\btheir own\b", r"\1your own", text)  # "you sent it to their own address"
    text = re.sub(r"(^|[.!?] )(you|your)\b", lambda m: m.group(1) + m.group(2).capitalize(), text)
    return text[:1].upper() + text[1:]


def _short(text: str, limit: int = 140) -> str:
    """One plain line: Poke's instructions are written for agents (trigger ids, UTC times).
    The full text stays in the raw log."""
    first = re.split(r"(?<=[.!?])\s", _plain(text), maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _plain_error(error: str) -> str:
    """What went wrong and what to do next. The raw error stays in the raw log."""
    e = error.lower()
    if "server stopped" in e:
        return error  # written for people already (a restart cut the run off)
    if "402" in e or "credit" in e:
        return "Out of AI credit. Add credit to the OpenRouter account, then ask again."
    if "timed out" in e or "timeout" in e:
        return "Took too long and stopped. Ask again."
    if "gmail" in e and "connect" in e:
        return "Gmail isn't connected. Connect it in Settings, then ask again."
    if "429" in e or "rate limit" in e:
        return "The AI service is busy. Ask again in a minute."
    return "Something went wrong. Ask again; the details are in the raw log."


def _describe(tool: str, raw_args: str, drafts: Dict[str, str]) -> str:
    to = _arg(raw_args, "recipient_email")
    if tool == "gmail_create_draft":
        return f"Drafted an email to {to}" if to else "Drafted an email"
    if tool == "gmail_execute_draft":
        draft_to = drafts.get(_arg(raw_args, "draft_id"), "")  # the exact draft sent, not the latest
        return f"Sent the email to {draft_to}" if draft_to else "Sent an email draft"
    if tool == "gmail_reply_to_thread":
        return f"Replied to {to}" if to else "Replied in a thread"
    if tool == "gmail_forward_email":
        return f"Forwarded an email to {to}" if to else "Forwarded an email"
    if tool == "gmail_delete_draft":
        return "Deleted a draft"
    if tool == "gmail_list_drafts":
        return "Checked your drafts"
    if tool in ("gmail_get_contacts", "gmail_get_people", "gmail_search_people"):
        query = _arg(raw_args, "query")
        return f"Looked up {query} in your contacts" if query else "Looked up your contacts"
    if tool == "task_email_search":
        return "Searched your inbox"  # the agent's query (search operators, dates) is in the raw log
    if tool == "createTrigger":
        return "Scheduled a reminder"
    if tool == "updateTrigger":
        return "Paused a reminder" if _arg(raw_args, "status") == "paused" else "Updated a reminder"
    if tool == "listTriggers":
        return "Checked your reminders"
    return f"Used {tool}"


def _events(agent_name: str) -> List[Dict[str, Any]]:
    """The timeline. Each event's kind, not its wording, is what status and badges read."""
    events: List[Dict[str, Any]] = []
    calls: Dict[str, str] = {}  # tool -> raw args of the call waiting for its result
    drafts: Dict[str, str] = {}  # draft id -> recipient, from this task's own drafts

    def add(at: str, kind: str, text: str) -> None:
        events.append({"at": at, "kind": kind, "text": text})

    for tag, at, payload in get_execution_agent_logs().iter_entries(agent_name):
        if tag == "agent_request":
            add(at, "asked", _short(payload))
        elif tag == "agent_action" and payload.startswith("Follow-up: "):
            add(at, "action", payload[len("Follow-up: "):])
        elif tag == "agent_action" and payload.startswith(("User approved:", "User declined:")):
            verb, _, what = payload.partition(":")
            to = what.split(" to ", 1)[-1].strip()
            if verb == "User approved":
                add(at, "approved", f"You approved sending to {to}")
            else:
                add(at, "declined", f"You declined sending to {to}")
        elif tag == "agent_action":
            match = _CALL.match(payload)
            if match:
                calls[match.group(1)] = match.group(2)
        elif tag == "tool_response":
            tool, _, result = payload.partition(": ")
            if tool not in calls:
                continue
            raw_args = calls.pop(tool)  # matched by tool name: another tool may have run meanwhile
            to = _arg(raw_args, "recipient_email") or drafts.get(_arg(raw_args, "draft_id"), "")
            if '"awaiting_user_approval"' in result:
                add(at, "waiting", f"Waiting for your approval to send to {to}" if to else "Waiting for your approval to send")
                continue
            error = _failed(result)
            text = _describe(tool, raw_args, drafts)
            if tool == "gmail_create_draft" and not error:
                for draft_id in re.findall(r'"id"\s*:\s*"?([\w-]+)', result):  # may be cut off
                    drafts[draft_id] = _arg(raw_args, "recipient_email")
            if error and tool in _SENDS:
                add(at, "send_failed", f"Couldn't send to {to or 'the recipient'}. {_plain_error(error)}")
            elif error:
                add(at, "failed", f"{text} failed. {_plain_error(error)}")
            else:
                add(at, "sent" if tool in _SENDS else "action", text)
        elif tag == "agent_response":
            text = payload.strip()
            if text.startswith("Error:"):
                add(at, "run_failed", _plain_error(text[6:]))
            else:
                add(at, "summary", _plain(text))  # in full; the panel shows a few lines and a toggle
    return events


def _parse_time(at: str) -> Optional[datetime]:
    try:
        return datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _since_last_ask(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    last_ask = max((i for i, e in enumerate(events) if e["kind"] == "asked"), default=-1)
    return events[last_ask + 1:]


def _elapsed(start: datetime, end: datetime) -> timedelta:
    """Real time between two local wall-clock log times, so a daylight-saving jump doesn't add
    or remove an hour."""
    from ..utils.timezones import resolve_user_timezone

    tz = resolve_user_timezone()
    return end.replace(tzinfo=tz).astimezone(timezone.utc) - start.replace(tzinfo=tz).astimezone(timezone.utc)


def _status(events: List[Dict[str, Any]], now: datetime) -> str:
    """Worked out from the log: the AI's own summary never decides the status."""
    run = _since_last_ask(events)
    sends = [e["kind"] for e in run if e["kind"] in ("sent", "send_failed")]
    if any(e["kind"] == "run_failed" for e in run) or sends[-1:] == ["send_failed"]:
        return "failed"
    if any(e["kind"] == "summary" for e in run):
        return "done"
    asks = [e for e in events if e["kind"] == "asked"]
    if not asks:
        return "done"  # only background steps (follow-up checks): nothing was asked, so nothing is running
    asked_at = _parse_time(asks[-1]["at"])
    if asked_at and _elapsed(asked_at, now) > STALE_AFTER:
        return "failed"
    return "working"


def list_tasks(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    from ..utils.timezones import now_in_user_timezone

    now = now or _parse_time(now_in_user_timezone("%Y-%m-%d %H:%M:%S")) or datetime.now()
    roster = get_agent_roster()
    tasks = []
    for agent in roster.recent(len(roster)):
        events = _events(agent.name)
        if not events:
            continue
        status = _status(events, now)
        last = next((e for e in reversed(events) if e["kind"] != "summary"), events[-1])
        latest = last["text"]
        if status == "failed":
            if last["kind"] not in ("run_failed", "send_failed", "failed"):
                latest = "Didn't finish. Ask again in the chat."
            sent = [e["text"] for e in _since_last_ask(events) if e["kind"] == "sent"]
            if sent:
                # The fact that matters most: something went out even though the task failed.
                latest = f"{sent[-1]}, then couldn't finish"
        tasks.append({
            "id": agent.key,
            "title": agent.name,
            "status": status,
            "latest": latest,
            "last_activity": events[-1]["at"],
            "sent_something": any(e["kind"] == "sent" for e in events),
        })
    tasks.sort(key=lambda t: t["last_activity"], reverse=True)
    return tasks


def task_detail(task_id: str) -> Optional[Dict[str, Any]]:
    agent = get_agent_roster().get(task_id)
    if agent is None:
        return None
    technical = [
        {"at": at, "tag": tag, "text": payload[:300]}
        for tag, at, payload in get_execution_agent_logs().iter_entries(agent.name)
    ]
    from ..utils.timezones import now_in_user_timezone

    events = _events(agent.name)
    now = _parse_time(now_in_user_timezone("%Y-%m-%d %H:%M:%S")) or datetime.now()
    return {"id": agent.key, "title": agent.name, "description": agent.description,
            "status": _status(events, now) if events else "done", "events": events, "technical": technical}


_DAYS = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}


def _repeats(rule: Optional[str]) -> Optional[str]:
    """RRULE text to plain English, for the common cases; anything else is just 'repeats'."""
    if not rule:
        return None
    parts = dict(p.split("=", 1) for p in re.findall(r"[A-Z]+=[^;\s]+", rule.upper()))
    every = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(parts.get("FREQ", ""))
    if not every:
        return "repeats"
    interval = int(parts.get("INTERVAL", "1") or 1)
    days = [_DAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",") if d[-2:] in _DAYS]
    if every == "week" and days and interval == 1:
        return "every " + ", ".join(days)
    return f"every {every}" if interval == 1 else f"every {interval} {every}s"


def _reminder(t: TriggerRecord, running: set[int]) -> Optional[Dict[str, Any]]:
    if t.status == "paused" or (t.status == "completed" and not t.last_error):
        return None  # cancelled, or finished fine: not "coming up"
    if t.id in running:
        state = "running"
    elif t.last_error or not t.next_trigger:
        state = "didnt_finish"  # last run failed or was cut off; may still have a next run
    else:
        state = "scheduled"
    return {"id": t.id, "what": t.payload, "next_run": t.next_trigger,
            "repeats": _repeats(t.recurrence_rule), "state": state,
            "last_error": _plain_error(t.last_error) if t.last_error else None}


def list_reminders() -> List[Dict[str, Any]]:
    from .trigger_scheduler import get_trigger_scheduler

    running = get_trigger_scheduler().running
    rows = [_reminder(t, running) for t in get_trigger_service().list_all_triggers()]
    order = {"didnt_finish": 0, "running": 1, "scheduled": 2}
    return sorted((r for r in rows if r), key=lambda r: order[r["state"]])


def list_follow_ups(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Open follow-ups, plus replies from the last day so you see them land."""
    from . import followups

    now = now or datetime.now(timezone.utc)
    rows = []
    for r in reversed(followups.get_follow_up_store().all()):
        recent = r.updated_at and now - datetime.fromisoformat(r.updated_at) < timedelta(days=1)
        if r.status in followups.OPEN or (r.status == "replied" and recent):
            rows.append({"id": r.id, "who": r.to, "subject": r.subject, "since": r.sent_at,
                         "check_in": r.due_at, "status": r.status, "reply": r.reply_preview})
    return rows


def cancel_reminder(reminder_id: int) -> bool:
    return get_trigger_service().set_status(reminder_id, "paused") is not None


def resume_reminder(reminder_id: int) -> bool:
    """Undo a cancel."""
    return get_trigger_service().set_status(reminder_id, "active") is not None
