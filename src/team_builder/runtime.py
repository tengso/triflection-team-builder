import ipaddress
import os
from importlib.resources import files
from urllib.parse import urlsplit

import yaml

from .docker import generation
from .nostr import wire
from .storage import private_write


def render(config, secrets, agent):
    coa = agent["id"] == "coa"
    channel_ids = agent["channel_ids"]
    extra = {
        "relay_url": config["advertised_url"],
        "channels": channel_ids,
        "home_channel": channel_ids[0] if channel_ids else "",
        "cli_path": "/usr/local/bin/buzz",
        "require_mention": True,
        "allow_all_users": True,
        "reply_in_thread": True,
        "allow_admin_from": [config["owner"]],
        "group_allow_admin_from": [config["owner"]],
    }
    document = {
        "model": {
            "provider": "openai-api"
            if config["provider"] == "openai"
            else config["provider"],
            "default": agent.get("model") or config["model"],
        },
        "agent": {"max_turns": 30},
        "terminal": {"backend": "local", "cwd": "/work"},
        "gateway": {
            "max_concurrent_sessions": 1,
            "platforms": {"buzz": {"enabled": True, "extra": extra}},
        },
        "security": {"redact_secrets": True},
        "display": {
            "platforms": {
                "buzz": {"interim_assistant_messages": False, "tool_progress": "off"}
            }
        },
        "mcp_servers": {},
        "platform_toolsets": {
            "buzz": ["terminal", "file", "skills", "memory", "todo", "session_search"]
            + (["mcp"] if coa else ["no_mcp"])
        },
    }
    if config.get("base_url"):
        document["model"]["provider"] = "custom:team"
        document["providers"] = {
            "team": {
                "enabled": True,
                "api": config["base_url"],
                "key_env": "OPENAI_API_KEY",
                "transport": "chat_completions",
                "default_model": config["model"],
            }
        }
    env = {
        "TEAM_BUILDER_OWNER": config["owner"],
        "BUZZ_PRIVATE_KEY": agent["secret"],
        "BUZZ_AUTH_TAG": wire(agent["auth_tag"]).decode(),
        "BUZZ_RELAY_URL": config["advertised_url"],
        "BUZZ_ALLOW_ALL_USERS": "true",
        "OPENROUTER_API_KEY"
        if config["provider"] == "openrouter"
        else "OPENAI_API_KEY": secrets["provider_key"],
    }
    if coa:
        env.update(
            TEAM_BUILDER_OWNER=config["owner"], TEAM_BUILDER_OFFICE=config["office"]
        )
        document["mcp_servers"] = {
            "team": {
                "command": "/opt/hermes/.venv/bin/team-builder-mcp",
                "env": {
                    "TEAM_BUILDER_URL": "http://manager:8088",
                    "TEAM_BUILDER_TOKEN": secrets["token"],
                },
            }
        }
        soul = (
            files("team_builder")
            .joinpath("resources/COA.md")
            .read_text()
            .format(owner=config["owner"])
        )
        if agent.get("instructions"):
            soul += "\n\nOwner-configured instructions:\n" + agent["instructions"]
    else:
        soul = f"You are {agent['name']}, an agent in a Buzz community.\n\n{agent['instructions']}\n\nWork in your assigned channels and reply in the request thread. Team changes must be proposed to Chief of Agents and approved by the human owner."
    if agent.get("github_credential"):
        env.update(
            GITHUB_TOKEN_FILE="/run/team/github-token",
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="credential.https://github.com.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="credential.https://github.com.helper",
            GIT_CONFIG_VALUE_1="!/opt/hermes/.venv/bin/python -m team_builder.github_access",
        )
        soul += (
            "\n\nGitHub access is provisioned as named credential "
            + agent["github_credential"]
            + ". HTTPS git clone/fetch/push authenticate automatically through a GitHub-only "
            "credential helper. Use plain https://github.com/owner/repo.git URLs. "
            "For GitHub API calls, read the token from GITHUB_TOKEN_FILE inside your program "
            "and send it only to https://api.github.com. Never print the token, include it "
            "in URLs/command arguments, save it in repositories, or paste it into chat. "
            "GitHub CLI is not required for git access. A repository announcement does not "
            "grant GitHub permissions; report authentication failures without revealing secrets."
        )
    soul += (
        "\n\nFor persistent app servers, launch detached from the terminal with stdin from "
        "/dev/null and stdout/stderr redirected to a log in /work (for example: "
        "nohup command > /work/app.log 2>&1 < /dev/null &). Gateway-only restart "
        "preserves detached processes, but attached pipes/PTYs and active turns may "
        "be interrupted. Check existing listeners before starting another instance. "
        "Container restart, stop, or upgrade still stops all processes."
    )
    return document, env, soul


def write_github_token(root, agent):
    from .github_access import token_for

    managed = root / "agents" / agent["id"] / "managed"
    managed.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = managed / "github-token"
    if agent.get("github_credential") and agent["state"] != "archived":
        private_write(path, token_for(root, agent["github_credential"]).encode())
        os.chown(path, 10000, 10000)
    else:
        path.unlink(missing_ok=True)


def start_agent(root, config, secrets, agent, docker):
    directory = root / "agents" / agent["id"]
    managed = directory / "managed"
    home = directory / "home"
    work = directory / "work"
    document, env, soul = render(config, secrets, agent)
    for path in (directory, managed, home, work):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, 10000, 10000)
    private_write(managed / "config.yaml", yaml.safe_dump(document).encode())
    private_write(managed / "env.json", env)
    private_write(managed / "SOUL.md", soul.encode())
    write_github_token(root, agent)
    for path in managed.iterdir():
        os.chown(path, 10000, 10000)
    host = config["host_root"] + "/agents/" + agent["id"]
    spec = {
        "Image": config["runtime_image"],
        "User": "10000:10000",
        "WorkingDir": "/work",
        "Entrypoint": ["/opt/hermes/.venv/bin/team-builder-worker"],
        "Env": [
            "HOME=/home/hermes",
            "HERMES_HOME=/home/hermes/.hermes",
            "PYTHONDONTWRITEBYTECODE=1",
        ],
        "Healthcheck": {
            "Test": [
                "CMD",
                "/opt/hermes/.venv/bin/python",
                "-m",
                "team_builder.worker",
                "health",
            ],
            "Interval": 5_000_000_000,
            "Timeout": 3_000_000_000,
            "Retries": 30,
        },
        "HostConfig": {
            "Binds": [
                host + "/managed:/run/team:ro",
                host + "/home:/home/hermes",
                host + "/work:/work",
            ],
            "NetworkMode": config["project"] + "_community",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Init": True,
            "Tmpfs": {"/tmp": "rw,nosuid,size=512m"},
            "RestartPolicy": {"Name": "unless-stopped"},
            "Memory": 2147483648,
            "PidsLimit": 256,
        },
    }
    hostname = urlsplit(config["advertised_url"]).hostname
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        bind = config.get("bind", "0.0.0.0")
        spec["HostConfig"]["ExtraHosts"] = [
            hostname + ":" + ("host-gateway" if bind == "0.0.0.0" else bind)
        ]
    docker.ensure(
        config["project"] + "-agent-" + agent["id"],
        spec,
        generation([config["runtime_image"], document, env, soul]),
    )
