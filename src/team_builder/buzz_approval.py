"""Bind Buzz command approvals to a specific in-process Hermes request."""

import os
import time
from collections import OrderedDict


async def send_exec_approval(
    adapter,
    chat_id,
    command,
    session_key,
    description,
    metadata=None,
    allow_permanent=True,
    allow_session=True,
    smart_denied=False,
):
    from gateway.platforms.base import SendResult
    from gateway.run import _format_exec_approval_fallback
    from tools.approval import list_gateway_approvals

    routes = getattr(adapter, "_team_approval_routes", None)
    if routes is None:
        routes = adapter._team_approval_routes = OrderedDict()
    known = {r["request_id"] for r in routes.values()}
    pending = [
        p for p in list_gateway_approvals(session_key) if p["request_id"] not in known
    ]
    if len(pending) != 1:
        # Never guess which of several queued commands a prompt would approve.
        raise RuntimeError("Cannot identify a unique Buzz approval request")
    request = pending[0]
    request_id = request["request_id"]
    meta = metadata or {}
    anchor = adapter._resolve_reply_anchor(
        meta.get("thread_id") or meta.get("reply_to_message_id")
    )
    content = _format_exec_approval_fallback(
        command,
        description,
        "/",
        allow_permanent=allow_permanent,
        allow_session=allow_session,
        smart_denied=smart_denied,
    )
    content += (
        "\n\nBuzz: reply directly to this prompt, or send `/approve "
        + request_id
        + "` in this channel. Use `/deny "
        + request_id
        + "` to cancel. Only the community owner can approve. Expired prompts require a new request."
    )
    result = await adapter.send(
        chat_id, content, metadata={**meta, "is_approval_prompt": True}
    )
    if not result.success or not result.message_id:
        return SendResult(success=False, error="Could not deliver Buzz approval prompt")
    routes[result.message_id] = {
        "request_id": request_id,
        "session_key": session_key,
        "channel": str(chat_id),
        "anchor": anchor,
        "created_at": int(time.time()),
        "allow_session": allow_session,
        "allow_permanent": allow_permanent,
    }
    # Old prompt replies cannot resolve newer requests. Keep tombstones in memory
    # through the gateway lifetime; after restart all prompts expire fail-closed.
    return result


async def handle_reply(adapter, event, channel_id, text, reply_to_is_own):
    words = text.strip().split()
    if not words or words[0].lower() not in ("/approve", "/deny"):
        return False
    routes = getattr(adapter, "_team_approval_routes", {})
    reply_ids = {t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == "e"}
    in_channel = [
        (mid, r) for mid, r in routes.items() if r["channel"] == str(channel_id)
    ]
    args = words[1:]
    explicit = [(mid, r) for mid, r in in_channel if r["request_id"] in args]
    direct = [(mid, r) for mid, r in in_channel if mid in reply_ids]
    candidates = (
        explicit
        or direct
        or [(mid, r) for mid, r in in_channel if r["anchor"] in reply_ids]
    )
    addressed = reply_to_is_own or adapter._is_addressed(event)
    if not candidates and not addressed:
        # Another agent's approval (or an unaddressed command) is not ours.
        return True
    if event.get("pubkey") != os.environ.get("TEAM_BUILDER_OWNER"):
        await adapter.send(
            channel_id,
            "Only the community owner can approve or deny this command.",
            reply_to=event["id"],
        )
        return True
    if adapter._is_sender_authorized(event["pubkey"], "group", channel_id) is not True:
        return True
    if len(candidates) != 1:
        await adapter.send(
            channel_id,
            "No unique pending approval matches this reply. Reply directly to the latest approval prompt or use its request ID; after a restart or timeout, ask the agent to retry the task.",
            reply_to=event["id"],
        )
        return True
    _, route = candidates[0]
    from tools.approval import list_gateway_approvals, resolve_gateway_approval

    remaining = [a.lower() for a in args if a != route["request_id"]]
    choice = (
        "deny"
        if words[0].lower() == "/deny"
        else (remaining[0] if remaining else "once")
    )
    if (
        len(remaining) > 1
        or choice not in ("once", "session", "always", "deny")
        or (words[0].lower() == "/deny" and remaining)
    ):
        await adapter.send(
            channel_id,
            "Use /approve [request-id] [session|always] or /deny [request-id].",
            reply_to=event["id"],
        )
        return True
    active = any(
        p["request_id"] == route["request_id"]
        for p in list_gateway_approvals(route["session_key"])
    )
    if not active or int(event.get("created_at", 0)) < route["created_at"]:
        await adapter.send(
            channel_id,
            "This approval has expired or was already answered. Ask the agent to retry; a fresh command needs a fresh approval.",
            reply_to=event["id"],
        )
        return True
    if (choice == "session" and not route["allow_session"]) or (
        choice == "always" and not route["allow_permanent"]
    ):
        await adapter.send(
            channel_id,
            "This command allows one-time approval only. Reply /approve to this prompt.",
            reply_to=event["id"],
        )
        return True
    count = resolve_gateway_approval(
        route["session_key"], choice, request_id=route["request_id"]
    )
    await adapter.send(
        channel_id,
        (
            "Command denied."
            if choice == "deny"
            else "Command approved (" + choice + ")."
        )
        if count
        else "This approval has expired; ask the agent to retry.",
        reply_to=event["id"],
    )
    return True
