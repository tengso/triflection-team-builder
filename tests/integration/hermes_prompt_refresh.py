"""Exercise the pinned Hermes restore path without model calls or chat messages."""

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.conversation_loop import _restore_or_build_system_prompt
from hermes_state import SessionDB

from team_builder.worker import refresh_session_prompts

with tempfile.TemporaryDirectory(prefix="prompt-refresh-") as directory:
    home = Path(directory)
    (home / "SOUL.md").write_text("Use link_github_repository for GitHub URLs")
    (home / "config.yaml").write_text("model: test\n")
    db = SessionDB(home / "state.db")
    db.create_session("continuing", "buzz")
    db.update_system_prompt("continuing", "Old tools: create_agent")
    assert db.get_session("continuing")["system_prompt"] == "Old tools: create_agent"
    db.close()
    assert refresh_session_prompts(home) == 1
    db = SessionDB(home / "state.db")
    assert db.get_session("continuing")["system_prompt"] is None
    history = [{"role": "user", "content": "Earlier conversation"}]
    agent = SimpleNamespace(
        _session_db=db,
        session_id="continuing",
        model="test",
        _build_system_prompt=lambda _: (home / "SOUL.md").read_text(),
    )
    # Isolate unrelated hooks and tool serialization, exercising the real
    # persisted-prompt decision and persistence with the native Hermes schema.
    with (
        patch("agent.conversation_loop.stage_surface_switch_note"),
        patch("hermes_cli.lifecycle.invoke_hook"),
        patch("agent.credits_tracker.seed_credits_at_session_start"),
        patch("tools.mcp_tool_agent.persist_agent_tool_names"),
    ):
        _restore_or_build_system_prompt(agent, None, history)
    assert "link_github_repository" in agent._cached_system_prompt
    assert db.get_session("continuing")["system_prompt"] == agent._cached_system_prompt
    assert history == [{"role": "user", "content": "Earlier conversation"}]
    db.close()
    assert refresh_session_prompts(home) == 0
    with sqlite3.connect(home / "state.db") as connection:
        assert connection.execute(
            "SELECT system_prompt_hash FROM sessions WHERE id='continuing'"
        ).fetchone()[0]
print("PASS: native Hermes continuing-session prompt rebuilt and persisted")
