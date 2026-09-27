"""Deployment CLI for agents whose harness has no MCP support (pi).

Shipped into the read-only managed bundle as /run/team/deployment_cli.py for
deployment-authorized agents, so this file must depend only on the standard
library and httpx. Every command is one call to the manager's /deployments
endpoint, which performs all authorization; this CLI grants nothing itself.
Keep TOOLS in step with resources/deployment_mcp.py (a test enforces parity).
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

REQUIRED = object()
URL = "http://manager:8088"
ENVIRONMENT = ("environment", "production")

# MCP tool name -> (manager action, parameters, description, renamed wire keys)
TOOLS = {
    "inspect_application": (
        "inspect",
        [("application", REQUIRED), ENVIRONMENT],
        "Read service health, deployed release and recent durable jobs.",
        {},
    ),
    "list_releases": (
        "releases",
        [("application", REQUIRED), ENVIRONMENT],
        "List operator-registered immutable releases.",
        {},
    ),
    "get_service_logs": (
        "logs",
        [("application", REQUIRED), ("service", REQUIRED), ENVIRONMENT],
        "Bounded sanitized service diagnostics.",
        {},
    ),
    "plan_deployment": (
        "plan",
        [
            ("application", REQUIRED),
            ("operation", "deploy"),
            ("release", None),
            ("service", None),
            ENVIRONMENT,
            ("profile", None),
        ],
        "Freeze a deploy, restart, or rollback plan. Executes nothing.",
        {"operation": "action_type"},
    ),
    "propose_deployment": (
        "propose",
        [
            ("application", REQUIRED),
            ("plan_id", REQUIRED),
            ("source_event_id", REQUIRED),
            ENVIRONMENT,
        ],
        "Publish a frozen plan in the source Buzz thread for owner approval.",
        {},
    ),
    "execute_deployment": (
        "execute",
        [
            ("application", REQUIRED),
            ("plan_id", REQUIRED),
            ("source_event_id", REQUIRED),
            ENVIRONMENT,
        ],
        "Queue a frozen plan authorized by a direct signed owner instruction.",
        {},
    ),
    "approve_deployment": (
        "approve",
        [("application", REQUIRED), ("approval_event_id", REQUIRED), ENVIRONMENT],
        "Queue exactly the frozen proposal the owner replied approve to.",
        {},
    ),
    "get_deployment_operation": (
        "operation",
        [("application", REQUIRED), ("operation_id", REQUIRED), ENVIRONMENT],
        "Read a persistent deployment job and its progress. Queued is not success.",
        {},
    ),
    "list_environment_profiles": (
        "profiles",
        [("application", REQUIRED), ENVIRONMENT],
        "List operator-approved profiles, secret reference names and last preflight. Never returns values.",
        {},
    ),
    "check_deployment_preflight": (
        "preflight",
        [("application", REQUIRED), ENVIRONMENT, ("profile", None)],
        "Check required credentials, files and TCP dependencies before deployment. No writes or restarts.",
        {},
    ),
    "plan_environment_configuration": (
        "configure",
        [
            ("application", REQUIRED),
            ("profile", REQUIRED),
            ENVIRONMENT,
            ("release", None),
        ],
        "Freeze profile and credential versions for owner approval; add a release for one combined deployment. Propose and approve it like a deployment plan.",
        {},
    ),
    "inspect_release_automation": (
        "automation-status",
        [("application", REQUIRED), ENVIRONMENT],
        "Read the automatic release policy, verified UAT handoff, check outcomes and blockers. Never claim UAT acceptance yourself.",
        {},
    ),
    "retry_automatic_release": (
        "automation-retry",
        [("application", REQUIRED), ENVIRONMENT],
        "After fixing a technical blocker, retry your assigned blocked stage under the existing policy (maximum two retries).",
        {},
    ),
}


COMMANDS = {
    "inspect_application": "inspect",
    "list_releases": "releases",
    "get_service_logs": "logs",
    "plan_deployment": "plan",
    "propose_deployment": "propose",
    "execute_deployment": "execute",
    "approve_deployment": "approve",
    "get_deployment_operation": "get-operation",
    "list_environment_profiles": "profiles",
    "check_deployment_preflight": "preflight",
    "plan_environment_configuration": "plan-configuration",
    "inspect_release_automation": "automation-status",
    "retry_automatic_release": "automation-retry",
}


def command_name(tool):
    return COMMANDS[tool]


def option(name):
    return "--" + name.replace("_", "-")


def parser():
    root = argparse.ArgumentParser(
        prog="python /run/team/deployment_cli.py",
        description="Operate assigned host applications through the Team Builder manager.",
    )
    sub = root.add_subparsers(dest="tool", required=True)
    for tool, (_, params, description, _) in TOOLS.items():
        command = sub.add_parser(command_name(tool), help=description)
        command.set_defaults(tool=tool)
        for name, default in params:
            if default is REQUIRED:
                command.add_argument(option(name), dest=name, required=True)
            else:
                command.add_argument(option(name), dest=name, default=default)
    return root


def request(args):
    action, params, _, renamed = TOOLS[args.tool]
    body = {"agent": os.environ["DEPLOYMENT_AGENT"], "action": action}
    for name, _ in params:
        body[renamed.get(name, name)] = getattr(args, name)
    return body


def main(argv=None, transport=None):
    args = parser().parse_args(argv)
    token = Path(os.environ["DEPLOYMENT_TOKEN_FILE"]).read_text().strip()
    try:
        with httpx.Client(
            base_url=URL,
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            trust_env=False,
            transport=transport,
        ) as client:
            result = client.post("/deployments", json=request(args))
    except httpx.HTTPError as error:
        print(
            f"Deployment service unreachable: {type(error).__name__}", file=sys.stderr
        )
        return 2
    try:
        payload = result.json()
    except ValueError:
        payload = {}
    if result.status_code != 200:
        print(payload.get("error", "Deployment request unavailable"), file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2))
    return 0


def skill(assignments):
    """Managed SKILL.md documenting this CLI for one agent's assignments."""
    lines = [
        "---",
        "name: team-managed-team-deployments",
        "description: Operate assigned host applications (inspect, plan, propose, approve, deploy) with the deployment CLI",
        "---",
        "# Deployment CLI",
        "",
        "Assigned application/environment pairs: " + ", ".join(assignments) + ".",
        "Run every command as `python /run/team/deployment_cli.py <command> ...`.",
        "Output is JSON on stdout; a rejection exits 1 with the reason on stderr.",
        "",
        "## Commands",
        "",
    ]
    for tool, (_, params, description, _) in TOOLS.items():
        usage = " ".join(
            f"{option(name)} {name.upper()}"
            if default is REQUIRED
            else f"[{option(name)} {name.upper()}]"
            for name, default in params
        )
        lines.append(
            f"- `{command_name(tool)} {usage}` (tool `{tool}`) — {description}"
        )
    lines += [
        "",
        "`--environment` defaults to production; pass it for staging.",
        "",
        "## Rules",
        "",
        "- Deployment instructions may name tools such as `list_environment_profiles`; each maps to the command listed above.",
        "- Run `automation-status` first. If an operator release policy is enabled, the manager deploys and promotes automatically: monitor it, fix technical failures, and use `automation-retry` only after fixing the cause.",
        "- Inspect, list releases, read logs, check profiles and preflight, and plan freely.",
        "- `--source-event-id` is the `Event ID` of the owner's message that triggered this turn (from the buzz-event block). Never use your own or another agent's message.",
        "- Use `execute` only for a direct, specific owner instruction to deploy that exact plan. Otherwise `propose` it and stop: the owner replies `approve` to the published proposal.",
        "- When the owner's approve reply wakes you, run `approve --approval-event-id <Event ID of that reply>`.",
        "- A queued job is not success. Poll `get-operation` until it reaches a terminal state and report the outcome.",
        "- Releases are registered by the operator; you cannot add images. Never deploy, touch databases, or seek Docker access from your terminal.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
