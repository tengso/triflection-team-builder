"""Narrow adapter compatibility patch, applied once while building the image.

Fail closed on an incompatible Hermes source instead of silently losing sender
metadata or COA's office mention rules. No changes to a live Hermes installation.
"""

from pathlib import Path

path = Path("/opt/hermes/plugins/platforms/buzz/adapter.py")
source = path.read_text()
replacements = {
    "if not is_dm and self.require_mention and not self._is_addressed(event) and not reply_to_is_own:": "if not (pubkey == os.getenv('TEAM_BUILDER_OWNER') and channel_id == os.getenv('TEAM_BUILDER_OFFICE')) and not is_dm and self.require_mention and not self._is_addressed(event) and not reply_to_is_own:",
    "text=dispatch_text, chat_id=channel_id, chat_type=chat_type, user_id=pubkey,": "text=(dispatch_text if dispatch_text.lstrip().startswith('/') else f'[Buzz source_event_id={event_id} sender={pubkey}]\\n{dispatch_text}'), chat_id=channel_id, chat_type=chat_type, user_id=pubkey,",
    "        # Channels dispatch only when addressed (@mention or p-tag) or replying to us (Signal/WhatsApp parity),": "        from team_builder.buzz_approval import handle_reply\n        if await handle_reply(self, event, channel_id, self._strip_mention(content), reply_to_is_own):\n            return\n        # Channels dispatch only when addressed (@mention or p-tag) or replying to us (Signal/WhatsApp parity),",
    "    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:": "    async def send_exec_approval(self, **kwargs):\n        from team_builder.buzz_approval import send_exec_approval\n        return await send_exec_approval(self, **kwargs)\n\n    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:",
}
for original, replacement in replacements.items():
    if source.count(original) != 1:
        raise SystemExit(
            "Unsupported Hermes Buzz adapter; qualify a compatible pinned image"
        )
    source = source.replace(original, replacement)
path.write_text(source)
