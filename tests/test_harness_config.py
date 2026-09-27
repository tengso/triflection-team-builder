import json
import os

import pytest

from team_builder import harness_config
from team_builder.storage import private_write


def managed(tmp_path, harness="pi", provider="openrouter", mcp=None, skills=None):
    directory = tmp_path / "managed"
    config = {
        "harness": harness,
        "agent_id": "a",
        "agent_name": "Agent",
        "model": "m1",
        "provider": provider,
        "base_url": "https://llm.local/v1" if provider == "custom" else None,
        "key_env": "OPENROUTER_API_KEY",
        "relay_url": "http://relay",
        "channels": ["c1"],
        "owner": "o" * 64,
        "mcp_servers": mcp or {},
    }
    private_write(directory / "harness.json", config)
    private_write(directory / "SOUL.md", b"You are Agent.")
    private_write(directory / "skills.json", skills or {})
    return directory


def environment(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {**dict(os.environ), "HOME": str(home)}


def test_pi_prepare_writes_models_and_skills(tmp_path):
    m = managed(
        tmp_path,
        skills={"review": "---\nname: team-managed-review\n---\nReview"},
    )
    env = environment(tmp_path)
    state = tmp_path / "state"
    private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
    harness_config.prepare(m, env, state)
    home = tmp_path / "home"
    skill = home / ".pi/agent/skills/team-managed-review/SKILL.md"
    assert "Review" in skill.read_text()
    private_write(m / "skills.json", {})
    harness_config.prepare(m, env, state)
    assert not skill.exists()


def test_pi_api_key_literal_and_env_fallback(tmp_path):
    m = managed(tmp_path)
    env = environment(tmp_path)
    env["OPENROUTER_API_KEY"] = "sk-or-secret"
    state = tmp_path / "state"
    private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
    harness_config.prepare(m, env, state)
    models_path = tmp_path / "home/.pi/agent/models.json"
    models = json.loads(models_path.read_text())
    assert models["providers"]["openrouter"]["apiKey"] == "sk-or-secret"
    assert models_path.stat().st_mode & 0o777 == 0o600

    env.pop("OPENROUTER_API_KEY")
    harness_config.prepare(m, env, state)
    models = json.loads(models_path.read_text())
    assert models["providers"]["openrouter"]["apiKey"] == "$OPENROUTER_API_KEY"


def test_pi_custom_provider_and_mcp_rejection(tmp_path):
    m = managed(tmp_path, provider="custom", mcp={"x": {"url": "https://u"}})
    env = environment(tmp_path)
    state = tmp_path / "state"
    private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
    with pytest.raises(ValueError, match="do not support MCP"):
        harness_config.prepare(m, env, state)
    m2 = managed(tmp_path / "second", provider="custom")
    private_write(state / "SOUL.md", (m2 / "SOUL.md").read_bytes())
    harness_config.prepare(m2, env, state)
    models = json.loads((tmp_path / "home/.pi/agent/models.json").read_text())
    team = models["providers"]["team"]
    assert team["baseUrl"] == "https://llm.local/v1"
    assert team["models"][0]["id"] == "m1"
    m3 = managed(tmp_path / "third")
    private_write(state / "SOUL.md", (m3 / "SOUL.md").read_bytes())
    harness_config.prepare(m3, env, state)
    models = json.loads((tmp_path / "home/.pi/agent/models.json").read_text())
    assert (
        models["providers"]["openrouter"]["baseUrl"] == "https://openrouter.ai/api/v1"
    )


def test_codex_config(tmp_path):
    m = managed(
        tmp_path,
        harness="codex",
        provider="openrouter",
        mcp={
            "search": {"url": "https://example.com/mcp", "headers": {"A": "b"}},
            "local": {"command": "/bin/x", "args": ["-y"], "env": {"K": "v"}},
        },
        skills={"s1": "---\nname: team-managed-s1\n---\nSkill"},
    )
    env = environment(tmp_path)
    state = tmp_path / "state"
    private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
    harness_config.prepare(m, env, state)
    home = tmp_path / "home"
    config = (home / ".codex/config.toml").read_text()
    assert 'model_provider = "team"' in config
    assert 'name = "Team"' in config
    assert 'base_url = "https://openrouter.ai/api/v1"' in config
    assert 'env_key = "OPENROUTER_API_KEY"' in config
    assert 'wire_api = "responses"' in config
    assert 'web_search = "disabled"' in config
    assert (
        "[mcp_servers.search]" in config and 'url = "https://example.com/mcp"' in config
    )
    assert "[mcp_servers.search.http_headers]" in config
    assert "[mcp_servers.local]" in config and 'command = "/bin/x"' in config
    assert "[mcp_servers.local.env]" in config and 'K = "v"' in config
    instructions = (home / ".codex/AGENTS.md").read_text()
    assert instructions.startswith("You are Agent.")
    assert "non-interactively" in instructions
    assert (home / ".codex/skills/team-managed-s1/SKILL.md").exists()


def test_codex_openai_and_custom_config(tmp_path):
    for provider, expected in (
        ("openai", ['model_provider = "openai"']),
        (
            "custom",
            [
                'base_url = "https://llm.local/v1"',
                'env_key = "OPENAI_API_KEY"',
            ],
        ),
    ):
        directory = tmp_path / provider
        m = managed(directory, harness="codex", provider=provider)
        env = environment(directory)
        state = directory / "state"
        private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
        harness_config.prepare(m, env, state)
        config = (directory / "home/.codex/config.toml").read_text()
        for line in expected:
            assert line in config


def test_devin_prepare(tmp_path):
    m = managed(
        tmp_path,
        harness="devin",
        mcp={"svc": {"command": "/bin/x", "args": [], "env": {"K": "v"}}},
        skills={"s1": "---\nname: team-managed-s1\n---\nSkill"},
    )
    env = environment(tmp_path)
    state = tmp_path / "state"
    private_write(state / "SOUL.md", (m / "SOUL.md").read_bytes())
    harness_config.prepare(m, env, state)
    home = tmp_path / "home"
    assert (home / ".config/devin/AGENTS.md").read_text() == "You are Agent."
    servers = json.loads((home / ".config/devin/mcp_config.json").read_text())
    assert servers["mcpServers"]["svc"]["command"] == "/bin/x"
    assert (home / ".config/devin/skills/team-managed-s1/SKILL.md").exists()


def test_unknown_harness_rejected(tmp_path):
    m = managed(tmp_path)
    private_write(m / "harness.json", {"harness": "bogus"})
    env = environment(tmp_path)
    with pytest.raises(ValueError, match="Unknown harness"):
        harness_config.prepare(m, env, tmp_path / "state")
