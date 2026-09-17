import fcntl
import hashlib
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
    document["platform_toolsets"]["buzz"] = list(
        agent.get(
            "tools", ["terminal", "file", "skills", "memory", "todo", "session_search"]
        )
    ) + (["mcp"] if coa or agent.get("mcp") or agent.get("deployments") else ["no_mcp"])
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
                "command": "/opt/hermes/.venv/bin/python",
                "args": ["/run/team/management_mcp.py"],
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
    if agent.get("deployments"):
        from .deployments import token

        document["mcp_servers"]["deployments"] = {
            "command": "/opt/hermes/.venv/bin/python",
            "args": ["/run/team/deployment_mcp.py"],
            "env": {
                "DEPLOYMENT_AGENT": agent["id"],
                "DEPLOYMENT_TOKEN": token(secrets, agent["id"]),
            },
        }
        soul += (
            "\n\nProduction operations: use the deployments MCP tools for these application/environment assignments: "
            + ", ".join(agent["deployments"])
            + ". Inspect and plan freely; execute only a specific signed owner instruction or owner-approved frozen proposal. Queueing is not success: poll the operation until terminal. Production runs on the host separately from this workspace. Never use your terminal to deploy production, change databases, or acquire Docker access. Releases must first be registered by the local operator."
        )
    if agent.get("soul"):
        soul += "\n\nPersonality and communication:\n" + agent["soul"]
    document["mcp_servers"].update(agent.get("resolved_mcp", {}))
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


def write_agent_files(root, config, secrets, agent):
    managed = root / "agents" / agent["id"] / "managed"
    managed.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(managed.parent, 10000, 10000)
    os.chown(managed, 10000, 10000)
    with (managed / ".config.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        os.chown(lock.name, 10000, 10000)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _write_agent_files(root, config, secrets, agent)


def _write_agent_files(root, config, secrets, agent):
    directory = root / "agents" / agent["id"]
    managed = directory / "managed"
    home = directory / "home"
    work = directory / "work"
    from .storage import Registry

    registry = Registry(root / "registry.sqlite3")
    resolved = {}
    skills = {}
    try:
        for identifier in agent.get("mcp", []):
            entry = registry.get("catalog/" + identifier)
            if not entry or entry["kind"] != "mcp":
                raise ValueError("Unknown MCP assignment")
            server = {"url": entry["url"], "tools": {"include": entry["tools"]}}
            if entry.get("credential"):
                import json

                key = json.loads(
                    (root / "credentials" / (entry["credential"] + ".json")).read_text()
                )["api_key"]
                server["headers"] = {"Authorization": "Bearer " + key}
            resolved["assigned-" + identifier] = server
        for identifier in agent.get("skills", []):
            entry = registry.get("catalog/" + identifier)
            if not entry or entry["kind"] != "skill":
                raise ValueError("Unknown skill assignment")
            skills[identifier] = (
                "---\n"
                + yaml.safe_dump(
                    {
                        "name": "team-managed-" + identifier,
                        "description": entry["description"] or entry["name"],
                    }
                )
                + "---\n"
                + entry["content"]
            )
    finally:
        registry.db.close()
    document, env, soul = render(config, secrets, {**agent, "resolved_mcp": resolved})
    for path in (directory, managed, home, work):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, 10000, 10000)
    if agent.get("deployments"):
        private_write(
            managed / "deployment_mcp.py",
            files("team_builder").joinpath("resources/deployment_mcp.py").read_bytes(),
        )
    else:
        (managed / "deployment_mcp.py").unlink(missing_ok=True)
    if agent["id"] == "coa":
        # Ship the current typed MCP facade in the read-only managed bundle so
        # manager-only upgrades can expose new tools without replacing workers.
        models_source = (
            files("team_builder")
            .joinpath("models.py")
            .read_text()
            .replace("from .agent_config", "from team_builder.agent_config")
            .replace("from .repositories", "from team_builder.repositories")
        )
        mcp_source = (
            files("team_builder")
            .joinpath("mcp.py")
            .read_text()
            .replace("from .models", "from management_models")
        )
        private_write(managed / "management_models.py", models_source.encode())
        private_write(
            managed / "management_mcp.py",
            (mcp_source + "\nif __name__ == '__main__':\n    main()\n").encode(),
        )
    private_write(managed / "config.yaml", yaml.safe_dump(document).encode())
    private_write(managed / "env.json", env)
    private_write(managed / "SOUL.md", soul.encode())
    private_write(managed / "skills.json", skills)
    private_write(
        managed / "revision.json", {"revision": agent.get("config_revision", 0)}
    )
    write_github_token(root, agent)
    private_write(
        managed / "manifest.json",
        {
            name: hashlib.sha256((managed / name).read_bytes()).hexdigest()
            for name in (
                "config.yaml",
                "env.json",
                "SOUL.md",
                "skills.json",
                "revision.json",
            )
        },
    )
    for path in managed.iterdir():
        os.chown(path, 10000, 10000)
    return document, env, soul


def start_agent(root, config, secrets, agent, docker):
    name = config["project"] + "-agent-" + agent["id"]
    observed = docker.inspect(name)
    managed = root / "agents" / agent["id"] / "managed"

    def fingerprint():
        return generation(
            {
                p.name: p.read_bytes().hex()
                for p in managed.glob("*")
                if p.is_file() and not p.name.startswith(".")
            }
        )

    before = fingerprint()
    write_agent_files(root, config, secrets, agent)
    changed = before != fingerprint()
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
        generation([config["runtime_image"], spec]),
    )
    current = docker.inspect(name)
    if (
        changed
        and observed
        and current
        and observed["Id"] == current["Id"]
        and observed.get("State", {}).get("Running")
    ):
        docker.restart(name)
