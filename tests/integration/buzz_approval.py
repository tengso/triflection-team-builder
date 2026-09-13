"""Native Buzz adapter and Hermes approval queue; no live messages or commands."""

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from plugins.platforms.buzz.adapter import BuzzAdapter
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry, _await_gateway_decision

OWNER = "a" * 64
BOT = "b" * 64
CHANNEL = "test-channel"
os.environ["TEAM_BUILDER_OWNER"] = OWNER
os.environ.pop("TEAM_BUILDER_OFFICE", None)


async def check():
    adapter = BuzzAdapter(
        PlatformConfig(
            enabled=True,
            extra={"relay_url": "http://test.invalid", "require_mention": True},
        )
    )
    adapter._self_pubkey = BOT
    adapter._display_name = "Engineer"
    adapter._channel_state[CHANNEL] = {"chat_type": "group", "seen": {}, "last_ts": 0}
    adapter._run_cli = AsyncMock(return_value=(0, "[]", ""))
    adapter._is_sender_authorized = lambda *args, **kwargs: True
    adapter._resolve_user_name = AsyncMock(return_value="Owner")
    adapter._dispatch_message = AsyncMock()
    adapter._message_handler = AsyncMock()
    sent = []

    async def send(chat_id, content, **kwargs):
        mid = f"sent-{len(sent)}"
        sent.append((mid, content))
        adapter._remember_event_meta(chat_id, mid, BOT, content)
        return SimpleNamespace(success=True, message_id=mid)

    adapter.send = send
    serial = 0

    async def inbound(
        text, parent=None, author=OWNER, channel=CHANNEL, timestamp=None, mention=False
    ):
        nonlocal serial
        serial += 1
        event = {
            "id": f"event-{serial}",
            "kind": 9,
            "created_at": timestamp or int(time.time()),
            "pubkey": author,
            "content": text,
            "tags": [["h", channel]],
        }
        if parent:
            event["tags"].append(["e", parent, "", "reply"])
        if mention:
            event["tags"].append(["p", BOT])
        adapter._channel_state.setdefault(
            channel, {"chat_type": "group", "seen": {}, "last_ts": 0}
        )
        await adapter._handle_event(channel, adapter._channel_state[channel], event)

    async def pending(key, anchor="owner-root", **flags):
        entry = _ApprovalEntry({"command": "echo test", "description": "test approval"})
        with approval._lock:
            approval._gateway_queues.setdefault(key, []).append(entry)
        result = await adapter.send_exec_approval(
            chat_id=CHANNEL,
            command="echo test",
            session_key=key,
            description="test approval",
            metadata={"reply_to_message_id": anchor},
            **flags,
        )
        return entry, result.message_id

    # Reproduce the reported topology: no mention, reply to the owner's original
    # message, while the pending approval belongs to a top-level session.
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()

    def notify(data):
        async def publish():
            await adapter.send_exec_approval(
                chat_id=CHANNEL,
                command="echo harmless",
                session_key="top-level",
                description="test approval",
                metadata={"reply_to_message_id": "original-owner-message"},
            )
            ready.set()

        asyncio.run_coroutine_threadsafe(publish(), loop).result(timeout=10)

    task = asyncio.create_task(
        asyncio.to_thread(
            _await_gateway_decision,
            "top-level",
            notify,
            {"command": "echo harmless", "description": "test approval"},
        )
    )
    await asyncio.wait_for(ready.wait(), 10)
    await inbound("/approve", "original-owner-message")
    decision = await asyncio.wait_for(task, 10)
    assert decision["resolved"] and decision["choice"] == "once"
    assert not adapter._dispatch_message.called

    entry, prompt = await pending("threaded")
    await inbound("/approve", prompt, author="c" * 64)
    assert not entry.event.is_set()
    await inbound("/approve", prompt, timestamp=1)
    assert not entry.event.is_set()
    await inbound(
        "/approve " + entry.data["request_id"], channel="wrong-channel", mention=True
    )
    assert not entry.event.is_set()
    await inbound("/approve session", prompt)
    assert entry.result == "session"
    # A second prompt in the same thread must not accept an old root reply or a
    # replay of the first prompt. Explicit IDs still work without a mention.
    next_entry, _next_prompt = await pending("threaded")
    await inbound("/approve", prompt)
    assert not next_entry.event.is_set()
    await inbound("/approve", "owner-root")
    assert not next_entry.event.is_set()
    await inbound("/deny " + next_entry.data["request_id"])
    assert next_entry.result == "deny"

    one_time, prompt = await pending(
        "restricted",
        anchor="restricted-root",
        allow_session=False,
        allow_permanent=False,
    )
    await inbound("/approve always", prompt)
    await inbound("/approve session", prompt)
    assert not one_time.event.is_set()
    await inbound("/approve", prompt)
    assert one_time.result == "once"
    expired, prompt = await pending("expired", anchor="expired-root")
    approval.unregister_gateway_notify("expired")
    await inbound("/approve", prompt)
    assert expired.result is None

    # Ordinary slash commands remain parseable; conversational messages retain
    # verified source metadata for management authorization.
    await inbound("/status", mention=True)
    text = adapter._dispatch_message.call_args.kwargs["text"]
    assert MessageEvent(text=text).get_command() == "status"
    await inbound("create a team", mention=True)
    assert adapter._dispatch_message.call_args.kwargs["text"].startswith(
        "[Buzz source_event_id="
    )
    assert not approval._gateway_queues
    print(
        "PASS: native Buzz approval routing, blocking-wait resolution, owner isolation, request binding, expiry, scope restrictions, and slash parsing"
    )


asyncio.run(check())
