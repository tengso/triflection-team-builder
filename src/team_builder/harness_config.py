"""Per-harness CLI configuration written inside the worker container.

buzz-acp owns the Buzz connection, sessions, and replies; this module only
prepares each harness CLI's config files and managed skills.
"""

import json
from pathlib import Path

from .worker_config import install_skills


def _load(managed):
    return json.loads((managed / "harness.json").read_text())


class Pi:
    name = "pi"
    config_path = Path(".pi/agent/models.json")
    skills_root = Path(".pi/agent/skills")
    instructions_path = None

    def write_config(self, config, home):
        if config.get("mcp_servers"):
            raise ValueError("pi agents do not support MCP connections")
        if config["provider"] not in ("openrouter", "custom"):
            raise ValueError(
                "pi requires the shared OpenRouter or custom provider; "
                "a direct OpenAI key cannot be used"
            )
        model = {
            "id": config["model"],
            "name": config["model"],
            "input": ["text"],
        }
        # pi treats a bare apiKey string as a literal secret, not an env
        # reference ("$VAR" is the env syntax), so write the resolved key.
        key = config.get("key_value") or "$" + config["key_env"]
        path = home / self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if config["provider"] == "openrouter":
            providers = {
                "openrouter": {
                    "baseUrl": "https://openrouter.ai/api/v1",
                    "api": "openai-completions",
                    "apiKey": key,
                    "models": [model],
                }
            }
        else:
            providers = {
                "team": {
                    "baseUrl": config["base_url"],
                    "api": "openai-completions",
                    "apiKey": key,
                    "models": [model],
                }
            }
        path.write_text(json.dumps({"providers": providers}, indent=2) + "\n")
        path.chmod(0o600)
        settings = home / ".pi/agent/settings.json"
        provider = "openrouter" if config["provider"] == "openrouter" else "team"
        settings.write_text(
            json.dumps(
                {
                    "defaultProvider": provider,
                    "defaultModel": config["model"],
                },
                indent=2,
            )
            + "\n"
        )


class Codex:
    name = "codex"
    config_path = Path(".codex/config.toml")
    skills_root = Path(".codex/skills")
    instructions_path = Path(".codex/AGENTS.md")

    def write_config(self, config, home):
        lines = [
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            f'model = "{config["model"]}"',
        ]
        provider = config["provider"]
        if provider == "openai":
            lines.append('model_provider = "openai"')
        else:
            lines.append('model_provider = "team"')
            if provider == "openrouter":
                base_url = "https://openrouter.ai/api/v1"
                key_env = "OPENROUTER_API_KEY"
            else:
                base_url = config["base_url"]
                key_env = "OPENAI_API_KEY"
            lines.append("[model_providers.team]")
            lines.append('name = "Team"')
            lines.append(f'base_url = "{base_url}"')
            lines.append(f'env_key = "{key_env}"')
            lines.append('wire_api = "responses"')
            lines.append('web_search = "disabled"')
        lines.append("")
        for name, server in config.get("mcp_servers", {}).items():
            table = f"[mcp_servers.{name}]"
            if "url" in server:
                lines += [table, f'url = "{server["url"]}"']
                if server.get("headers"):
                    lines.append(f"{table[:-1]}.http_headers]")
                    for key, value in server["headers"].items():
                        lines.append(f'{key} = "{value}"')
                if server.get("tools"):
                    quoted = ", ".join(f'"{t}"' for t in server["tools"]["include"])
                    lines.append(f"enabled_tools = [{quoted}]")
            else:
                lines += [table, f'command = "{server["command"]}"']
                if server.get("args"):
                    quoted = ", ".join(f'"{a}"' for a in server["args"])
                    lines.append(f"args = [{quoted}]")
                if server.get("env"):
                    lines.append(f"{table[:-1]}.env]")
                    for key, value in server["env"].items():
                        lines.append(f'{key} = "{value}"')
            lines.append("")
        path = home / self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))


class Devin:
    name = "devin"
    config_path = Path(".config/devin/config.json")
    skills_root = Path(".config/devin/skills")
    instructions_path = Path(".config/devin/AGENTS.md")

    def write_config(self, config, home):
        path = home / self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model": {"name": config["model"]},
                },
                indent=2,
            )
            + "\n"
        )
        servers = {}
        for name, server in config.get("mcp_servers", {}).items():
            if "url" in server:
                entry = {"url": server["url"]}
                if server.get("headers"):
                    entry["headers"] = dict(server["headers"])
            else:
                entry = {
                    "command": server["command"],
                    "args": list(server.get("args", [])),
                }
                if server.get("env"):
                    entry["env"] = dict(server["env"])
            servers[name] = entry
        if servers:
            (home / ".config/devin/mcp_config.json").write_text(
                json.dumps({"mcpServers": servers}, indent=2) + "\n"
            )


DRIVERS = {"pi": Pi(), "codex": Codex(), "devin": Devin()}


def prepare(managed, env, state):
    """Write the harness CLI's config files and managed skills."""
    config = _load(managed)
    harness = config["harness"]
    if harness not in DRIVERS:
        raise ValueError(f"Unknown harness: {harness}")
    home = Path(env["HOME"])
    driver = DRIVERS[harness]
    key_env = config.get("key_env")
    if key_env and env.get(key_env):
        config = {**config, "key_value": env[key_env]}
    driver.write_config(config, home)
    if driver.instructions_path:
        soul = (state / "SOUL.md").read_text()
        if harness == "codex":
            soul += (
                "\n\nYou run non-interactively inside a chat bridge: answer "
                "directly in your final message. Do not ask the user questions "
                "and do not call tools such as request_user_input; no user can "
                "respond.\n"
            )
        target = home / driver.instructions_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(soul)
    if driver.skills_root:
        install_skills(state, managed, root=home / driver.skills_root)
