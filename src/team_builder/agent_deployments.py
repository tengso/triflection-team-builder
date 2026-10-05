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
# Plan-, job- and proposal-based commands default to the referenced item's environment.
FROM_PLAN = ("environment", None)

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
        "List registered immutable releases and the CI importer state (importing, current, stale, failed).",
        {},
    ),
    "get_service_logs": (
        "logs",
        [
            ("application", REQUIRED),
            ("service", REQUIRED),
            ENVIRONMENT,
            ("detail", "summary"),
        ],
        "Service diagnostics: --detail summary (recognized entries) or redacted (last 200 lines, secrets masked).",
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
            FROM_PLAN,
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
            FROM_PLAN,
        ],
        "Queue a frozen plan authorized by a direct signed owner instruction.",
        {},
    ),
    "approve_deployment": (
        "approve",
        [("application", REQUIRED), ("approval_event_id", REQUIRED), FROM_PLAN],
        "Queue exactly the frozen proposal the owner replied approve to.",
        {},
    ),
    "get_deployment_operation": (
        "operation",
        [("application", REQUIRED), ("operation_id", REQUIRED), FROM_PLAN],
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
    "verify_release_checks": (
        "automation-verify",
        [("application", REQUIRED), ENVIRONMENT],
        "Run the policy's acceptance checks now (once a minute); no deploy, no retry spent. Failed checks include masked output.",
        {},
    ),
    "rollback_production": (
        "automation-rollback",
        [("application", REQUIRED), ENVIRONMENT],
        "Production agent: restore the previous release after a blocked run, if the policy allows. Images only.",
        {},
    ),
    "propose_configuration_change": (
        "propose-change",
        [("source_event_id", REQUIRED), ("operations", REQUIRED)],
        "Publish a frozen release-setup proposal (JSON list of operations) for owner approval. Never include secret values.",
        {},
    ),
    "approve_configuration_change": (
        "approve-change",
        [("approval_event_id", REQUIRED)],
        "Execute exactly the frozen configuration proposal the owner replied approve to.",
        {},
    ),
}
JSON_PARAMS = {"operations"}


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
    "verify_release_checks": "verify",
    "rollback_production": "rollback",
    "propose_configuration_change": "propose-change",
    "approve_configuration_change": "approve-change",
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
            kind = json.loads if name in JSON_PARAMS else str
            if default is REQUIRED:
                command.add_argument(option(name), dest=name, required=True, type=kind)
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
        "Assigned application/environment pairs: "
        + (", ".join(assignments) or "none yet (release agent)")
        + ".",
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
        "`--environment` defaults to production; pass it for staging. Commands that take a plan, operation or approval default to that item's environment.",
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


EXAMPLE_OPERATIONS = [
    {
        "action": "register_application",
        "spec": {
            "id": "my-app",
            "environment": "staging",
            "repository": "owner/my-app",
            "services": [
                {
                    "id": "ui",
                    "command": ["ui"],
                    "port": 8502,
                    "host_port": 18502,
                    "health_path": "/healthz",
                    "data_path": "/app/data",
                    "environment": {"API_URL": "http://api:8002"},
                },
                {
                    "id": "api",
                    "command": ["api"],
                    "port": 8002,
                    "health_path": "/health",
                    "health_token_env": "API_TOKEN",
                },
            ],
        },
    },
    {
        "action": "generate_credential",
        "application": "my-app",
        "environment": "staging",
        "id": "api-token",
    },
    {
        "action": "register_profile",
        "application": "my-app",
        "environment": "staging",
        "profile": {
            "id": "uat-v1",
            "values": {"MYSQL_HOST": "db.internal", "MYSQL_PORT": "3306"},
            "secrets": {"MYSQL_PASSWORD": "mysql-password", "API_TOKEN": "api-token"},
            "files": {"/app/config/users.yaml": "users-yaml"},
            "required_env": ["MYSQL_HOST", "MYSQL_PASSWORD", "API_TOKEN"],
            "connections": [{"id": "mysql", "host": "db.internal", "port": 3306}],
        },
    },
    {
        "action": "attach_dependency",
        "application": "my-app",
        "environment": "staging",
        "container": "uat-mysql-1",
        "alias": "uat-mysql",
    },
    {
        "action": "configure_deployment_access",
        "agent": "cody",
        "application": "my-app",
        "environment": "staging",
        "allowed": True,
    },
    {
        "action": "configure_release_sync",
        "application": "my-app",
        "repository": "owner/my-app",
        "credential": "github",
        "workflow": "release.yml",
        "branch": "main",
        "environments": ["staging"],
        "services": ["ui", "api"],
    },
]


def runbook(assignments, cli=False):
    """Managed SKILL.md: the release agent's operating procedure (all harnesses)."""
    scope = ", ".join(assignments) or "no application yet (release agent)"
    tools = (
        "Tool names below map to deployment CLI commands (see the "
        "team-managed-team-deployments skill)."
        if cli
        else "Tool names below are your deployments MCP tools."
    )
    text = f"""---
name: team-managed-team-release-runbook
description: Set up, release, investigate and recover assigned applications as a release agent
---
# Release runbook

Your deployment scope: {scope}. UAT is environment `staging`. {tools}

## Division of work

Release agents do the technical release work. The owner only replies `approve`
to your frozen proposals and supplies secret values (database passwords, login
files, GitHub tokens) on the host. Never ask for or accept secret values in chat.
The manager validates and executes everything; queued is not success.

## Set up a new application

Until your first setup proposal is approved you have no application scope, so
inspect, list and preflight tools answer "no access"; that is expected. Start with
`propose_configuration_change`.

1. Read the application repository: service commands, ports, health endpoints
   (the image must contain `python` for health checks), data paths and required
   environment variables.
2. Call `propose_configuration_change` once with every operation needed:
   - `register_application` for `staging` and `production`
     (`spec`: id, environment, repository `owner/name`, services with id,
     command, port, optional host_port, health_path, optional health_token_env,
     data_path, memory_mb and non-secret environment).
   - `generate_credential` for generated secrets such as API tokens.
   - `register_profile` per environment: `values`, `secrets` (env var ->
     credential id), `files` (target under /app/config -> credential id),
     `required_env`, `connections` (dependency host/port for preflight).
   - `attach_dependency` for existing database containers, using the DNS alias
     your profile connects to.
   - `configure_deployment_access` for the UAT agent (staging) and the
     production agent (production).
   - `configure_release_sync`: repository, the stored GitHub credential name,
     workflow file, branch and services built from the one CI image.
   Exact shape (every operation has an `action` key; `command` is a list;
   `data_path` belongs to a service; `profile` is an object with its `id`; file
   targets are absolute paths under /app/config; connections use `id`, `host`,
   `port` with the real DNS name or container alias, never a variable name):

```json
{{EXAMPLE}}
```

   Ask the owner for dependency host names and ports you cannot find (they are
   not secrets). Validation errors name the failing field; fix and resubmit.
3. Tell the owner exactly which supplied credentials are still missing, with the
   host command: `echo '{{"action":"credential","application":"APP","environment":"ENV","id":"ID"}}' > ID.json && team-builder deployment ID.json --secret-file /path/to/ID`.
4. In the repository (development agent): add the CI release workflow and image
   smoke test, and an `acceptance` entrypoint with read-only, repeatable checks
   (`{{environment}}` in the policy command is replaced with staging/production).
   Merged code is the trust boundary: open a pull request and ask for review.
5. When `check_deployment_preflight` passes in both environments, propose
   `configure_release_policy` with `enabled: true`, both agents, the profiles,
   checks calling the acceptance entrypoint, the shared notification channel,
   `production_rollback` and the diagnostics settings.
6. When the owner replies `approve` to your own proposal, call
   `approve_configuration_change` (deployment plans: `approve_deployment`) with
   that reply's Event ID. You are woken for every owner `approve` in your
   channels; if it replies to someone else's proposal, do nothing.

## Waiting for a CI release

A merged change appears in `list_releases` a few minutes after its CI workflow
finishes: the manager polls every two minutes, then downloads and verifies the
release artifact. Check `ci_import` in the `list_releases` result: `importing`
names the release being imported (wait), `current` means up to date, and only
`stale` (no successful poll for ten minutes) or `failed` needs attention.

## When a notice mentions you

1. `inspect_release_automation`: blocked stage, reason, failed checks and their
   masked output.
2. `get_service_logs` with `detail="redacted"` and `check_deployment_preflight`.
3. Decide:
   - Code or test defect (UAT agent): fix it in the repository and open a pull
     request. After merge, CI publishes a new release that the policy picks up;
     do not retry the broken release.
   - Wrong configuration: propose a new profile ID and the policy change.
   - Dependency unreachable: check attachments; propose `attach_dependency`.
   - Transient failure: `verify_release_checks`; if it passes,
     `retry_automatic_release` (at most two retries per run).
   - Missing supplied secret: tell the owner which one and the command above.
4. Production checks failed (production agent): if the previous release was
   healthy, `rollback_production` first, then investigate with the UAT agent.
5. Report in the notification channel: what failed, the evidence, the action
   taken and the next step.

## Rules

- Never deploy, change databases or use Docker from your terminal; no migrations.
- Masked logs can still contain application data: summarize, never repost it.
- Owner approval covers exactly the frozen proposal; propose again for changes.
"""
    return text.replace("{EXAMPLE}", json.dumps(EXAMPLE_OPERATIONS, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
