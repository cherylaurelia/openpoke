"""Gmail tool schemas and actions for the execution agent."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from server.services import approvals
from server.services.execution import get_execution_agent_logs
from server.services.gmail import execute_gmail_tool, get_active_gmail_user_id

_GMAIL_AGENT_NAME = "gmail-execution-agent"

_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "gmail_create_draft",
            "description": "Create a Gmail draft via Composio, supporting html/plain bodies, cc/bcc, and attachments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_email": {
                        "type": "string",
                        "description": "Primary recipient email for the draft.",
                    },
                    "subject": {"type": "string", "description": "Email subject."},
                    "body": {
                        "type": "string",
                        "description": "Email body. Use HTML markup when is_html is true.",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of CC recipient emails.",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of BCC recipient emails.",
                    },
                    "extra_recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional recipients if the draft should include more addresses.",
                    },
                    "is_html": {
                        "type": "boolean",
                        "description": "Set true when the body contains HTML content.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Existing Gmail thread id if this draft belongs to a thread.",
                    },
                    "attachment": {
                        "type": "object",
                        "description": "Single attachment metadata (requires Composio-uploaded asset).",
                        "properties": {
                            "s3key": {"type": "string", "description": "S3 key of uploaded file."},
                            "name": {"type": "string", "description": "Attachment filename."},
                            "mimetype": {"type": "string", "description": "Attachment MIME type."},
                        },
                        "required": ["s3key", "name", "mimetype"],
                    },
                },
                "required": ["recipient_email", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_execute_draft",
            "description": "Send a previously created Gmail draft using Composio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "Identifier of the Gmail draft to send.",
                    },
                },
                "required": ["draft_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_forward_email",
            "description": "Forward an existing Gmail message with optional additional context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message id to forward.",
                    },
                    "recipient_email": {
                        "type": "string",
                        "description": "Email address to receive the forwarded message.",
                    },
                    "additional_text": {
                        "type": "string",
                        "description": "Optional text to prepend when forwarding.",
                    },
                },
                "required": ["message_id", "recipient_email"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_reply_to_thread",
            "description": "Send a reply within an existing Gmail thread via Composio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread id to reply to.",
                    },
                    "recipient_email": {
                        "type": "string",
                        "description": "Primary recipient for the reply (usually the original sender).",
                    },
                    "message_body": {
                        "type": "string",
                        "description": "Reply body. Use HTML markup when is_html is true.",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of CC recipient emails.",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of BCC recipient emails.",
                    },
                    "extra_recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional recipients if needed.",
                    },
                    "is_html": {
                        "type": "boolean",
                        "description": "Set true when the body contains HTML content.",
                    },
                    "attachment": {
                        "type": "object",
                        "description": "Single attachment metadata (requires Composio-uploaded asset).",
                        "properties": {
                            "s3key": {"type": "string", "description": "S3 key of uploaded file."},
                            "name": {"type": "string", "description": "Attachment filename."},
                            "mimetype": {"type": "string", "description": "Attachment MIME type."},
                        },
                        "required": ["s3key", "name", "mimetype"],
                    },
                },
                "required": ["thread_id", "recipient_email", "message_body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_delete_draft",
            "description": "Delete a specific Gmail draft using the Composio Gmail integration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "Identifier of the Gmail draft to delete.",
                    },
                },
                "required": ["draft_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_get_contacts",
            "description": "Retrieve Google contacts (connections) available to the authenticated Gmail account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_name": {
                        "type": "string",
                        "description": "Resource name to read contacts from, defaults to people/me.",
                    },
                    "person_fields": {
                        "type": "string",
                        "description": "Comma-separated People API fields to include (e.g. emailAddresses,names).",
                    },
                    "include_other_contacts": {
                        "type": "boolean",
                        "description": "Include other contacts (directory suggestions) when true.",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Pagination token for retrieving the next page of contacts.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_get_people",
            "description": "Retrieve detailed Google People records or other contacts via Composio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_name": {
                        "type": "string",
                        "description": "Resource name to fetch (defaults to people/me).",
                    },
                    "person_fields": {
                        "type": "string",
                        "description": "Comma-separated People API fields to include in the response.",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Maximum number of people records to return per page.",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Token to continue fetching the next set of results.",
                    },
                    "sync_token": {
                        "type": "string",
                        "description": "Sync token for incremental sync requests.",
                    },
                    "other_contacts": {
                        "type": "boolean",
                        "description": "Set true to list other contacts instead of connections.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_list_drafts",
            "description": "List Gmail drafts for the connected account using Composio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of drafts to return.",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Pagination token from a previous drafts list call.",
                    },
                    "verbose": {
                        "type": "boolean",
                        "description": "Include full draft details such as subject and body when true.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_search_people",
            "description": "Search Google contacts and other people records associated with the Gmail account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to match against names, emails, phone numbers, etc.",
                    },
                    "person_fields": {
                        "type": "string",
                        "description": "Comma-separated fields from the People API to include in results.",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Maximum number of people records to return.",
                    },
                    "other_contacts": {
                        "type": "boolean",
                        "description": "Include other contacts results when true.",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Pagination token to continue a previous search.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

_LOG_STORE = get_execution_agent_logs()


# Return Gmail tool schemas
def get_schemas() -> List[Dict[str, Any]]:
    """Return Gmail tool schemas."""
    
    return _SCHEMAS


# Execute a Gmail tool and record the action for the execution agent journal
def _execute(tool_name: str, composio_user_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a Gmail tool and record the action for the execution agent journal."""

    payload = {k: v for k, v in arguments.items() if v is not None}
    payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else "{}"
    try:
        result = execute_gmail_tool(tool_name, composio_user_id, arguments=payload)
    except Exception as exc:
        _LOG_STORE.record_action(
            _GMAIL_AGENT_NAME,
            description=f"{tool_name} failed | args={payload_str} | error={exc}",
        )
        raise

    _LOG_STORE.record_action(
        _GMAIL_AGENT_NAME,
        description=f"{tool_name} succeeded | args={payload_str}",
    )
    return result


# Create a Gmail draft via Composio with support for HTML, attachments, and threading
def gmail_create_draft(
    recipient_email: str,
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    extra_recipients: Optional[List[str]] = None,
    is_html: Optional[bool] = None,
    thread_id: Optional[str] = None,
    attachment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {
        "recipient_email": recipient_email,
        "subject": subject,
        "body": body,
        "cc": cc,
        "bcc": bcc,
        "extra_recipients": extra_recipients,
        "is_html": is_html,
        "thread_id": thread_id,
        "attachment": attachment,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_CREATE_EMAIL_DRAFT", composio_user_id, arguments)


# Send a previously created Gmail draft using Composio
def gmail_execute_draft(
    draft_id: str,
) -> Dict[str, Any]:
    arguments = {"draft_id": draft_id}
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_SEND_DRAFT", composio_user_id, arguments)


# Forward an existing Gmail message with optional additional context
def gmail_forward_email(
    message_id: str,
    recipient_email: str,
    additional_text: Optional[str] = None,
) -> Dict[str, Any]:
    arguments = {
        "message_id": message_id,
        "recipient_email": recipient_email,
        "additional_text": additional_text,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_FORWARD_MESSAGE", composio_user_id, arguments)


# Send a reply within an existing Gmail thread via Composio
def gmail_reply_to_thread(
    thread_id: str,
    recipient_email: str,
    message_body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    extra_recipients: Optional[List[str]] = None,
    is_html: Optional[bool] = None,
    attachment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    arguments = {
        "thread_id": thread_id,
        "recipient_email": recipient_email,
        "message_body": message_body,
        "cc": cc,
        "bcc": bcc,
        "extra_recipients": extra_recipients,
        "is_html": is_html,
        "attachment": attachment,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_REPLY_TO_THREAD", composio_user_id, arguments)


# Delete a specific Gmail draft using the Composio Gmail integration
def gmail_delete_draft(
    draft_id: str,
) -> Dict[str, Any]:
    arguments = {"draft_id": draft_id}
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_DELETE_DRAFT", composio_user_id, arguments)


def gmail_get_contacts(
    resource_name: Optional[str] = None,
    person_fields: Optional[str] = None,
    include_other_contacts: Optional[bool] = None,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    arguments = {
        "resource_name": resource_name,
        "person_fields": person_fields,
        "include_other_contacts": include_other_contacts,
        "page_token": page_token,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_GET_CONTACTS", composio_user_id, arguments)


def gmail_get_people(
    resource_name: Optional[str] = None,
    person_fields: Optional[str] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    sync_token: Optional[str] = None,
    other_contacts: Optional[bool] = None,
) -> Dict[str, Any]:
    arguments = {
        "resource_name": resource_name,
        "person_fields": person_fields,
        "page_size": page_size,
        "page_token": page_token,
        "sync_token": sync_token,
        "other_contacts": other_contacts,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_GET_PEOPLE", composio_user_id, arguments)


def gmail_list_drafts(
    max_results: Optional[int] = None,
    page_token: Optional[str] = None,
    verbose: Optional[bool] = None,
) -> Dict[str, Any]:
    arguments = {
        "max_results": max_results,
        "page_token": page_token,
        "verbose": verbose,
    }
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_LIST_DRAFTS", composio_user_id, arguments)


def gmail_search_people(
    query: str,
    person_fields: Optional[str] = None,
    page_size: Optional[int] = None,
    other_contacts: Optional[bool] = None,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {
        "query": query,
        "person_fields": person_fields,
        "other_contacts": other_contacts,
    }
    if page_size is not None:
        arguments["pageSize"] = page_size
    if page_token is not None:
        arguments["pageToken"] = page_token
    composio_user_id = get_active_gmail_user_id()
    if not composio_user_id:
        return {"error": "Gmail not connected. Please connect Gmail in settings first."}
    return _execute("GMAIL_SEARCH_PEOPLE", composio_user_id, arguments)


# The tools that send email. Agents get gated versions; only approvals.approve() calls these.
approvals.SENDERS.update({
    "gmail_execute_draft": gmail_execute_draft,
    "gmail_forward_email": gmail_forward_email,
    "gmail_reply_to_thread": gmail_reply_to_thread,
})


def _own_email() -> str:
    """The connected Gmail address, if already known (no network call). Empty if unknown."""
    from server.services.gmail.client import _extract_email, _get_cached_profile

    return (_extract_email(_get_cached_profile(get_active_gmail_user_id())) or "").lower()


def _recipients(arguments: Dict[str, Any]) -> List[str]:
    """Everyone the email goes to: To, cc, bcc and extra recipients."""
    found = [arguments.get("recipient_email")]
    for key in ("cc", "bcc", "extra_recipients"):
        value = arguments.get(key) or []
        found.extend([value] if isinstance(value, str) else value)
    return [str(r) for r in found if r and str(r).strip()]  # unstripped: " me@x" isn't treated as me


def _create_draft(**arguments: Any) -> Dict[str, Any]:
    result = gmail_create_draft(**arguments)
    if not approvals.result_error(result):
        approvals.get_approval_store().remember_draft(
            approvals.draft_ids(result), _recipients(arguments),
            arguments.get("subject", ""), arguments.get("body", ""))
    return result


def _gated(agent_name: str, tool: str) -> Callable[..., Dict[str, Any]]:
    """Ask for approval instead of sending, unless the email only goes to the user."""

    def run(**arguments: Any) -> Dict[str, Any]:
        arguments = {k: v for k, v in arguments.items() if v is not None}
        store = approvals.get_approval_store()
        if tool == "gmail_execute_draft":
            # Look up the exact draft being sent. One we didn't see created has no known
            # recipient, so it can never skip approval.
            draft = store.draft(str(arguments.get("draft_id", "")))
            to, subject, preview = draft.get("to", []), draft.get("subject", ""), draft.get("body", "")
        else:
            to = _recipients(arguments)
            if tool == "gmail_reply_to_thread":
                subject, preview = "Reply in thread", arguments.get("message_body", "")
            else:
                subject = f"Forward of message {arguments.get('message_id', '?')}"
                preview = arguments.get("additional_text", "")
        if to and {r.lower() for r in to} == {_own_email()}:
            return approvals.SENDERS[tool](**arguments)  # only to the user: no approval needed
        shown_to = ", ".join(to) or "(unknown recipient: check the draft in Gmail)"
        pending = store.request(agent_name, tool, arguments, shown_to, subject, preview)
        return {
            "status": "awaiting_user_approval",
            "approval_id": pending.id,
            "note": "Nothing was sent. Sending needs the user's approval, which happens outside "
            "your tools. Report that the email is ready and waiting for their approval.",
        }

    return run


# Return Gmail tool callables
def build_registry(agent_name: str) -> Dict[str, Callable[..., Any]]:
    """Return Gmail tool callables. Sending tools are gated behind the user's approval."""

    return {
        "gmail_create_draft": _create_draft,
        "gmail_execute_draft": _gated(agent_name, "gmail_execute_draft"),
        "gmail_delete_draft": gmail_delete_draft,
        "gmail_forward_email": _gated(agent_name, "gmail_forward_email"),
        "gmail_reply_to_thread": _gated(agent_name, "gmail_reply_to_thread"),
        "gmail_get_contacts": gmail_get_contacts,
        "gmail_get_people": gmail_get_people,
        "gmail_list_drafts": gmail_list_drafts,
        "gmail_search_people": gmail_search_people,
    }


__all__ = [
    "build_registry",
    "get_schemas",
    "gmail_create_draft",
    "gmail_execute_draft",
    "gmail_delete_draft",
    "gmail_forward_email",
    "gmail_reply_to_thread",
    "gmail_get_contacts",
    "gmail_get_people",
    "gmail_list_drafts",
    "gmail_search_people",
]
