"""A slow Gmail call in the important-email watcher must not freeze the server."""

import asyncio
import time

from server.services.gmail import importance_watcher
from server.services.gmail.seen_store import GmailSeenStore


def test_a_hanging_gmail_fetch_does_not_block_other_requests(tmp_path, monkeypatch):
    def slow_fetch(*args, **kwargs):
        time.sleep(0.5)  # a connection that stalls, as after the laptop sleeps
        return {"successful": True, "data": {"messages": []}}

    monkeypatch.setattr(importance_watcher, "execute_gmail_tool", slow_fetch)
    monkeypatch.setattr(importance_watcher, "get_active_gmail_user_id", lambda: "user-1")
    watcher = importance_watcher.ImportantEmailWatcher(seen_store=GmailSeenStore(tmp_path / "seen.json", 10))

    async def main():
        poll = asyncio.create_task(watcher._poll_once())
        started = time.monotonic()
        await asyncio.sleep(0.05)  # stands in for any other request the server is handling
        waited = time.monotonic() - started
        await poll
        return waited

    assert asyncio.run(main()) < 0.3  # the other request ran while Gmail was still stalled
