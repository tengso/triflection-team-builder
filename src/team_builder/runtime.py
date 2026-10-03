import fcntl
import hashlib
import ipaddress
import json
import os
from importlib.resources import files
from urllib.parse import urlsplit

import yaml

from .docker import generation
from .nostr import wire
from .storage import private_write

HARNESSES = {
    "hermes": {
        "python": "/opt/hermes/.venv/bin/python",
        "worker": "/opt/hermes/.venv/bin/team-builder-worker",
        "home": "/home/hermes",
        "state": ".team-builder",
        "acp": ["/opt/hermes/.venv/bin/hermes-acp"],
    },
    "pi": {
        "python": "/opt/team-builder/.venv/bin/python",
        "worker": "/opt/team-builder/.venv/bin/team-builder-worker",
        "home": "/home/agent",
        "state": ".team-builder",
        "acp": ["pi-acp"],
    },
    "codex": {
        "python": "/opt/team-builder/.venv/bin/python",
        "worker": "/opt/team-builder/.venv/bin/team-builder-worker",
        "home": "/home/agent",
        "state": ".team-builder",
        "acp": ["codex-acp"],
    },
    "devin": {
        "python": "/opt/team-builder/.venv/bin/python",
        "worker": "/opt/team-builder/.venv/bin/team-builder-worker",
        "home": "/home/agent",
        "state": ".team-builder",
        "acp": ["devin", "acp"],
    },
}


DEPLOYMENT_SKILL = "team-deployments"
RUNBOOK_SKILL = "team-release-runbook"
RESERVED_SKILLS = {DEPLOYMENT_SKILL, RUNBOOK_SKILL}


def release_agent(agent):
    """Agents with deployment grants, or trusted to propose new applications."""
    return bool(agent.get("deployments") or agent.get("release_agent"))


def harness_of(agent):
    return agent.get("harness", "hermes")


def python_for(agent):
    return HARNESSES[harness_of(agent)]["python"]


def home_dir(root, agent):
    return (
        root / "agents" / agent["id"] / "home" / HARNESSES[harness_of(agent)]["state"]
    )


def image_for(config, agent):
    harness = harness_of(agent)
    if harness == "hermes":
        return config["runtime_image"]
    image = config.get("images", {}).get(harness)
    if not image:
        raise ValueError(
            f"{harness} runtime image is not installed; run team-builder upgrade --harness {harness} --image IMAGE"
        )
    return image


def render(config, secrets, agent):
    harness = harness_of(agent)
    coa = agent["id"] == "coa"
    key_env = (
        "OPENROUTER_API_KEY" if config["provider"] == "openrouter" else "OPENAI_API_KEY"
    )
    document = {"mcp_servers": {}}
    if harness == "hermes":
        document.update(
            {
                "model": {
                    "provider": "openai-api"
                    if config["provider"] == "openai"
                    else config["provider"],
                    "default": agent.get("model") or config["model"],
                },
                "agent": {"max_turns": 30},
                "terminal": {"backend": "local", "cwd": "/work"},
                "security": {"redact_secrets": True},
                "platform_toolsets": {
                    "acp": list(
                        agent.get(
                            "tools",
                            [
                                "terminal",
                                "file",
                                "skills",
                                "memory",
                                "todo",
                                "session_search",
                            ],
                        )
                    )
                    + (
                        ["mcp"]
                        if coa or agent.get("mcp") or release_agent(agent)
                        else ["no_mcp"]
                    )
                },
            }
        )
        if release_agent(agent):
            # Keep typed deployment schemas visible instead of routing through the
            # generic tool_call(name, arguments) bridge, which can lose arguments.
            document["tools"] = {"tool_search": {"enabled": "off"}}
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
    harness_document = {
        "harness": harness,
        "agent_id": agent["id"],
        "agent_name": agent["name"],
        "model": agent.get("model") or config["model"],
        "provider": config["provider"],
        "base_url": config.get("base_url"),
        "key_env": key_env,
        "relay_url": config.get("internal_url") or config["advertised_url"],
        "channels": agent["channel_ids"],
        "owner": config["owner"],
        "mcp_servers": {},
    }
    env = {
        "TEAM_BUILDER_OWNER": config["owner"],
        "BUZZ_PRIVATE_KEY": agent["secret"],
        "BUZZ_AUTH_TAG": wire(agent["auth_tag"]).decode(),
        "BUZZ_RELAY_URL": config.get("internal_url") or config["advertised_url"],
        "BUZZ_ACP_RESPOND_TO": "anyone",
        "BUZZ_ACP_PERMISSION_MODE": "bypass-permissions",
        "BUZZ_ACP_SESSION_POLICY": "channel",
        **({key_env: secrets["provider_key"]} if harness != "devin" else {}),
    }
    if harness == "codex":
        env["CODEX_API_KEY"] = secrets["provider_key"]
        env["DEFAULT_AUTH_REQUEST"] = '{"methodId": "api-key"}'
        env["NO_BROWSER"] = "1"
        env["INITIAL_AGENT_MODE"] = "agent-full-access"
    if harness == "hermes":
        env["HERMES_ACP_SKIP_CONFIGURED_MCP"] = "0"
    if coa:
        env["TEAM_BUILDER_OFFICE"] = config["office"]
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
        soul = _worker_soul(agent)
    if harness != "hermes":
        document = {}
    return _finish(config, secrets, agent, document, harness_document, env, soul)


def _worker_soul(agent):
    return f"You are {agent['name']}, an agent in a Buzz community.\n\n{agent['instructions']}\n\nWork in your assigned channels and reply in the request thread. Team changes must be proposed to Chief of Agents and approved by the human owner."


def _finish(config, secrets, agent, document, harness_document, env, soul):
    harness = harness_of(agent)
    mcp_servers = (
        document["mcp_servers"]
        if harness == "hermes"
        else harness_document["mcp_servers"]
    )
    if release_agent(agent):
        from .deployments import token

        if harness == "pi":
            # pi has no MCP; the bundled CLI calls the same manager endpoint.
            env["DEPLOYMENT_AGENT"] = agent["id"]
            env["DEPLOYMENT_TOKEN_FILE"] = "/run/team/deployment-token"
            tools = "the deployment CLI (python /run/team/deployment_cli.py, documented in the team-managed-team-deployments skill; the tool names below map to its commands)"
        else:
            mcp_servers["deployments"] = {
                "command": python_for(agent),
                "args": ["/run/team/deployment_mcp.py"],
                "env": {
                    "DEPLOYMENT_AGENT": agent["id"],
                    "DEPLOYMENT_TOKEN": token(secrets, agent["id"]),
                },
            }
            tools = "the deployments MCP tools"
        scope = ", ".join(agent.get("deployments", [])) or (
            "none yet; you may propose new applications"
        )
        soul += (
            f"\n\nRelease operations: you are a release agent. Use {tools}; your application/environment scope: "
            + scope
            + ". Follow the team-managed-team-release-runbook skill. You do the technical release work: propose application registration, profiles, generated credentials, dependency attachments, access grants, CI import settings and release policies with propose_configuration_change, and after the owner replies approve, execute them with approve_configuration_change. Under an enabled release policy the manager deploys, verifies UAT and promotes automatically: investigate blocked runs with inspect_release_automation, masked check output and get_service_logs(detail=redacted), fix causes (code fixes arrive as new CI releases), confirm with verify_release_checks, then retry_automatic_release; the production agent may rollback_production when the policy allows. Without a policy, execute only an owner-approved frozen proposal or a specific signed owner instruction. Queueing is not success: poll until terminal. Never request or accept secret values in chat: tell the owner which credential is missing and the exact host command. Never use your terminal to deploy, change databases or reach Docker; no migrations. TCP preflight proves reachability only, not database authorization or schema correctness."
        )
    if agent.get("soul"):
        soul += "\n\nPersonality and communication:\n" + agent["soul"]
    mcp_servers.update(agent.get("resolved_mcp", {}))
    if agent.get("github_credential"):
        env.update(
            GITHUB_TOKEN_FILE="/run/team/github-token",
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="credential.https://github.com.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="credential.https://github.com.helper",
            GIT_CONFIG_VALUE_1="!"
            + python_for(agent)
            + " -m team_builder.github_access",
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
    soul += (
        "\n\nDelivery: your final assistant text is NOT shown to anyone. Every reply "
        "must be posted with the buzz CLI, replying to the triggering message. Write "
        "the reply to a file with your file tool (for example /work/.buzz-reply.md), "
        "then run: buzz messages send --channel <channel uuid> --reply-to <Event ID "
        "from the buzz-event block> --content - < /work/.buzz-reply.md. --content takes "
        "message text or - for stdin, never a file path; never put a multi-line or "
        "markdown reply inside shell quotes. Post exactly "
        "one reply per request unless asked for more; if a task fails, post the "
        "failure the same way. Tool calls are auto-approved inside your isolated "
        "container, so act carefully and never run destructive commands without an "
        "explicit owner instruction."
    )
    return document, harness_document, env, soul


def _coa_rules(config):
    return (
        '[[rules]]\nname = "mentions"\nchannels = "all"\nkinds = [9]\n'
        "require_mention = true\n\n"
        '[[rules]]\nname = "office-owner"\n'
        f'channels = ["{config["office"]}"]\nkinds = [9]\n'
        "require_mention = false\n"
        f"filter = 'author == \"{config['owner']}\"'\n"
    )


APPROVALS = ("approve", "Approve", "APPROVE")


def _release_rules(config):
    """Wake release agents for the owner's bare approve reply, which clients may send untagged."""
    content = " || ".join(f'content == "{word}"' for word in APPROVALS)
    return (
        '[[rules]]\nname = "mentions"\nchannels = "all"\nkinds = [9]\n'
        "require_mention = true\n\n"
        '[[rules]]\nname = "owner-approvals"\nchannels = "all"\nkinds = [9]\n'
        "require_mention = false\n"
        f"filter = 'author == \"{config['owner']}\" && ({content})'\n"
    )


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
    document, harness_document, env, soul = render(
        config, secrets, {**agent, "resolved_mcp": resolved}
    )
    for path in (directory, managed, home, work):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, 10000, 10000)
    cli_agent = release_agent(agent) and harness_of(agent) == "pi"
    if release_agent(agent) and not cli_agent:
        private_write(
            managed / "deployment_mcp.py",
            files("team_builder").joinpath("resources/deployment_mcp.py").read_bytes(),
        )
    else:
        (managed / "deployment_mcp.py").unlink(missing_ok=True)
    if cli_agent:
        from .agent_deployments import skill
        from .deployments import token

        private_write(
            managed / "deployment_cli.py",
            files("team_builder").joinpath("agent_deployments.py").read_bytes(),
        )
        private_write(
            managed / "deployment-token", token(secrets, agent["id"]).encode()
        )
        skills[DEPLOYMENT_SKILL] = skill(agent.get("deployments", []))
    else:
        (managed / "deployment_cli.py").unlink(missing_ok=True)
        (managed / "deployment-token").unlink(missing_ok=True)
    if release_agent(agent):
        from .agent_deployments import runbook

        skills[RUNBOOK_SKILL] = runbook(agent.get("deployments", []), cli=cli_agent)
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
    from .proxy import environment as proxy_environment

    env.update(proxy_environment(root, config))
    harness = harness_of(agent)
    names = [
        "config.yaml",
        "env.json",
        "SOUL.md",
        "skills.json",
        "revision.json",
        "harness.json",
    ]
    if harness == "devin":
        credential = json.loads(
            (root / "credentials" / (agent["harness_credential"] + ".json")).read_text()
        )
        env["WINDSURF_API_KEY"] = credential["api_key"]
    if agent["id"] == "coa" or release_agent(agent):
        rules = _coa_rules(config) if agent["id"] == "coa" else _release_rules(config)
        private_write(managed / "rules.toml", rules.encode())
        names.append("rules.toml")
    else:
        (managed / "rules.toml").unlink(missing_ok=True)
    private_write(managed / "harness.json", harness_document)
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
            for name in names
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
    harness = HARNESSES[harness_of(agent)]
    image = image_for(config, agent)
    spec = {
        "Image": image,
        "User": "10000:10000",
        "WorkingDir": "/work",
        "Entrypoint": [harness["worker"]],
        "Env": [
            "HOME=" + harness["home"],
            *(
                ["HERMES_HOME=/home/hermes/.hermes"]
                if harness["home"] == "/home/hermes"
                else ["TEAM_BUILDER_HARNESS=" + harness_of(agent)]
            ),
            "PYTHONDONTWRITEBYTECODE=1",
        ],
        "Healthcheck": {
            "Test": [
                "CMD",
                harness["python"],
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
                host + "/home:" + harness["home"],
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
        if not config.get("internal_url"):
            spec["HostConfig"]["ExtraHosts"] = [
                hostname + ":" + ("host-gateway" if bind == "0.0.0.0" else bind)
            ]
    docker.ensure(
        config["project"] + "-agent-" + agent["id"],
        spec,
        generation([image, spec]),
    )
    current = docker.inspect(name)
    if (
        changed
        and observed
        and current
        and observed["Id"] == current["Id"]
        and observed.get("State", {}).get("Running")
    ):
        docker.restart(name, python_for(agent))
