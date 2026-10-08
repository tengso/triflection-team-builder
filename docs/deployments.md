# Deployment management service

For a step-by-step walkthrough, read [Deploy applications to UAT and production](application-deployment-guide.md). This page is the architecture and tool reference.

Deployment management is a generic, installation-scoped component of the Team Builder toolkit. The owner or Chief of Agents (COA) can assign it to any selected managed agent. **Cody is an example assignee, not a dependency or a special agent role.** Application registrations, releases, credentials, and deployment history belong to the installation rather than an agent workspace.

## Architecture and lifecycle

The deployment service runs inside the Python management service, with its own persistent SQLite registry, background job worker, and observation loop. It is not a separate service container. Agents access a scoped MCP facade; they receive neither Docker access nor production application secrets through that facade.

Applications run as separate Docker containers on the Linux hosting VM, on networks separate from agent containers. Each application/environment owns a private network, labeled containers, and optional persistent data volumes. The manager is the component that controls these resources.

| Component | Responsibility |
| --- | --- |
| Local owner/operator | Install Team Builder and supply secret values (database passwords, login files, GitHub tokens) on the host. Every other step can be proposed by a release agent; the CLI remains available for all of them. |
| Owner | Reply `approve` to release agents' frozen proposals (setup changes, and deployments when no release policy is enabled). |
| COA | Make an agent a release agent and assign or revoke deployment access through owner-authorized community-management operations. |
| Release agents (e.g. Cody for UAT, Oppo for production) | Propose application registrations, profiles, generated credentials, dependency attachments, CI import settings, access grants and release policies; investigate, verify, retry and roll back under an enabled policy; propose deployments otherwise. |
| Deployment service | Enforce scope and authorization, persist jobs, control host containers, check readiness, and record outcomes. |
| Mission Control | Display service health, releases, operation history, and sanitized diagnostics. |

Stopping, archiving, renaming, or restarting an assigned agent does not stop its assigned production applications. Revoking access prevents future deployment tool requests; already queued, owner-authorized jobs continue independently. A manager restart temporarily interrupts management and observations, while application containers keep running and durable unfinished jobs resume when the manager returns. A VM shutdown stops both; application containers use Docker's `unless-stopped` restart policy.

Assignments are per **agent ID + application ID + environment**. One agent can manage multiple application/environment pairs, and multiple agents can be assigned the same pair. `staging` and `production` are separate scopes: a staging grant does not grant production access. No assignment automatically grants blanket approval for changes.

## Dashboard visibility

Mission Control → **Deployments** shows service health, pinned releases, published ports, persistent operation history, and timestamped diagnostic logs. **Activity** also shows deployment outcomes. Logs are bounded diagnostic summaries; arbitrary application output, request bodies, and credentials are excluded. All timestamps use the browser's timezone. Observations become stale after 15 seconds and continue independently of deployment work.

## Agent-led setup (recommended)

Release agents do the technical setup; the owner approves it in Buzz and the
operator only supplies secret values. An agent may propose changes for
applications it holds a grant for; a **release agent** (set by the owner through
COA: "Make Cody a release agent", operation `configure_release_agent`) may also
introduce new applications, and then everything for them in the same proposal.

The agent calls `propose_configuration_change` with a list of operations. The
manager validates every payload and the agent's scope, then publishes a frozen
proposal signed by the agent in its channel. After the owner replies `approve` to
that proposal, the agent calls `approve_configuration_change`; the manager
executes the operations in order and records each result, so a retry resumes
after a partial failure.

| Operation | Effect |
| --- | --- |
| `register_application` `{spec}` | Registers the immutable service specification for one environment (same schema as the CLI `register` request, without secrets). |
| `register_profile` `{application, environment, profile}` | Registers an immutable profile: values, credential references, file references, required variables, dependency checks. |
| `generate_credential` `{application, environment, id, rotate}` | Creates a generated secret such as an API token. Supplied secrets stay operator-only. |
| `attach_dependency` `{application, environment, container, alias}` | Connects an existing host container (for example a database) to the application network under a DNS alias. Team Builder containers, privileged containers and containers with the Docker socket are refused. Recorded attachments are restored before preflight and deployment, e.g. after a database container is recreated. |
| `configure_release_sync` `{application, repository, credential, workflow, branch, environments, services, enabled}` | Lets the manager import verified CI releases every two minutes (no host timer). `credential` names a stored GitHub credential. |
| `configure_release_policy` `{policy, expected_version}` | Registers, changes, enables or pauses the automatic release policy. The policy is replaced as a whole; agent proposals must name the live policy version they were written against (`""` if none). If the policy changes before approval, the proposal is rejected and nothing in it is applied. |
| `configure_deployment_access` `{agent, application, environment, allowed}` | Grants or revokes deployment access, including for another agent. |

What remains for the operator: `team-builder github-credential NAME` for the
importer's GitHub token (or reuse one already stored), and supplied application
credentials. When one is missing, the release agent — and a blocked automatic run
— tells the owner exactly which credential it is and the host command:

```bash
echo '{"action":"credential","application":"APP","environment":"ENV","id":"ID"}' > ID.json
team-builder deployment ID.json --secret-file /private/path/ID
```

## Operator setup

`team-builder deployment REQUEST.json` sends a structured request through the local Docker operator boundary. Its credential is distinct from COA's community-management credential. Add `--state-dir PATH` for another installation. Request files below contain no secret values.

Register an application with a fixed service specification. The following Streamlit UI/API application is an example; replace the application ID, repository, commands, ports, and health paths for your workload:

```json
{"action":"register","application":{"id":"hti-research-admin","environment":"production","repository":"tengso/hti-research-admin","services":[{"id":"ui","command":["ui"],"port":8502,"host_port":38502,"health_path":"/_stcore/health","data_path":"/app/data"},{"id":"api","command":["python","-m","uvicorn","modules.research.crm.data_api_main:app","--host","0.0.0.0","--port","8002"],"port":8002,"health_path":"/health","health_token_env":"CRM_REST_TOKEN"}]}}
```

Supply private application settings with `--secrets-file /private/app-env.json` (a JSON mapping of environment names to strings) and optionally `--users-file /private/users.yaml`. Never paste credentials in Buzz or put them into the image/build context. The local operator can replace credentials by repeating registration with the same specification; this invalidates pending plans, rejects updates during active deployment jobs, and requires a new deployment to apply the settings. Image rollback does not restore credentials or database contents.

Register a release built from a known Git commit. Every service must use an immutable image reference, either a registry digest (`ghcr.io/owner/app@sha256:…`) or a fully qualified local Docker image ID (`sha256:…`). Mutable image tags are rejected.

```json
{"action":"release","release":{"id":"main-9104be4","application":"hti-research-admin","environment":"production","commit":"9104be4c00f7c0e364fd86fd2e612c62518898c4","images":{"ui":"sha256:REPLACE_WITH_64_HEX_IMAGE_ID","api":"sha256:REPLACE_WITH_64_HEX_IMAGE_ID"}}}
```

The first deployment may use a clean local build of `git archive <commit>`. For subsequent releases, prefer CI-produced registry digests and register only verified artifacts. Private registry images must be loaded/pulled by the host operator first; agent GitHub tokens are never reused for registry pulls. This version does not trigger CI or check out/build arbitrary code from a chat request.

## Assign a selected agent

First register the application/environment and create the selected agent. Use the agent's stable ID (for example, `devops`), not its display name. The selected agent can be a developer, a dedicated operations agent, or COA itself. An archived agent cannot receive a grant.

### Owner assignment through the CLI

Save this as `grant-deployment.json` on the Linux host:

```json
{
  "action": "grant",
  "agent": "devops",
  "application": "hti-research-admin",
  "environment": "production",
  "allowed": true
}
```

Apply it to the installation:

```sh
team-builder deployment grant-deployment.json \
  --state-dir ~/.local/state/team-builder/default
```

The service saves the assignment, adds the managed `deployments` MCP connection and deployment instructions to that agent, and reloads a running agent's gateway. pi agents have no MCP support; they instead receive `/run/team/deployment_cli.py` (every deployments MCP tool as a subcommand — `inspect`, `releases`, `logs`, `plan`, `propose`, `execute`, `approve`, `get-operation`, `profiles`, `preflight`, `plan-configuration`, `automation-status`, `automation-retry`), their token as the private file `/run/team/deployment-token`, and a managed `team-deployments` skill documenting the commands and approval rules. The CLI calls the same manager endpoint, so authorization, scope and owner approval are identical. Revoking access removes all three. Its container and detached application processes are retained; an active agent response may be interrupted. Stopped agents load the configuration on their next start and cannot use the deployment tools while stopped. No manual token copying or MCP catalog entry is needed.

### COA assignment through Buzz

The owner can ask COA, for example:

> Give the DevOps agent access to manage the production environment of hti-research-admin.

COA resolves the selected agent's stable ID and submits this typed operation through its existing `execute_direct` or proposal/approval flow:

```json
{
  "action": "configure_deployment_access",
  "agent": "devops",
  "application": "hti-research-admin",
  "environment": "production",
  "allowed": true
}
```

The CLI action is `grant`; the COA management operation is `configure_deployment_access`. Both configure the same service. COA must have a verified owner instruction or an approved frozen proposal to change the assignment. The owner communicates subsequent deployment requests in a Buzz channel assigned to the selected agent.

### Revoke or transfer responsibility

Set `allowed` to `false` in either example to revoke that exact application/environment grant. Scope and running-agent state are checked on every request, so an old token cannot retain revoked access. Production containers, data, and history are retained.

To transfer responsibility, grant the same application/environment to the new agent, verify its deployment tools, then revoke the previous agent. The application does not need to be recreated or redeployed. Repeat grants independently for other applications or environments.

Assignment through the dashboard configuration editor is not implemented. Use the owner CLI or COA's typed management operation; the dashboard provides deployment visibility.

## Tools available to assigned agents

Every tool takes an `application`; `environment` defaults to `production`, except tools that reference a plan, operation or approval (`propose_deployment`, `execute_deployment`, `approve_deployment`, `get_deployment_operation`), which default to that item's environment. Only assigned scopes are accessible.

| MCP tool | Purpose and additional parameters |
| --- | --- |
| `inspect_application` | Read current service health, release, and recent jobs. |
| `inspect_release_automation` | Read the standing policy, automatic runs, acceptance results and agent handoff. |
| `retry_automatic_release` | Retry the assigned blocked stage after correcting its cause, within the standing policy and retry limit. |
| `list_releases` | List operator-registered immutable releases. |
| `get_service_logs` | Read diagnostics for `service`: `detail="summary"` (recognized entries) or `detail="redacted"` (last 200 lines, installation and environment secrets masked; staging, and production when the release policy allows). |
| `verify_release_checks` | Run the release policy's acceptance checks now, at most once a minute. Does not deploy or spend a retry; failed checks include masked output. |
| `rollback_production` | Production agent only: restore the previous production release after a blocked automatic run when the policy's `production_rollback` is `agent` (or `automatic`). Images only. |
| `propose_configuration_change` | Publish a frozen release-setup proposal (`source_event_id`, `operations`); see Agent-led setup. No `application` parameter. |
| `approve_configuration_change` | Execute the configuration proposal referenced by the owner's `approval_event_id`. |
| `plan_deployment` | Freeze a plan with `operation` (`deploy`, `restart`, or `rollback`), `release` and optional `profile` for deploy, and optional `service` for restart. Does not execute changes. |
| `list_environment_profiles` | List profiles with their non-secret `values` (hosts, ports, names, flags) and connection checks, secret reference names, and the last preflight result. Credential values are never returned. Profiles are immutable: copy one completely when registering a new profile ID. |
| `check_deployment_preflight` | Check credentials, required settings/files and TCP dependencies for an optional `profile`. |
| `plan_environment_configuration` | Freeze a `profile`; include `release` to apply configuration and deploy in one approved operation. |
| `propose_deployment` | Publish the frozen `plan_id` for approval in the thread identified by `source_event_id`. |
| `execute_deployment` | Queue `plan_id` using a direct, specific signed owner instruction identified by `source_event_id`. |
| `approve_deployment` | Execute the exact frozen proposal referenced by the owner's `approval_event_id`. |
| `get_deployment_operation` | Read progress and the terminal outcome of `operation_id`. |

The public `plan_deployment` MCP tool and the local CLI plan request both use `operation`. The MCP facade translates this to `action_type` internally; agents do not need to construct internal HTTP requests.

Agents cannot change anything directly: application specifications, profiles, generated credentials, dependency attachments, CI import settings, grants and policies take effect only through an owner-approved frozen proposal. Agents never handle supplied secret values and cannot register releases by hand; releases come from the verified CI importer or the operator. Deployment access does not give an ordinary agent COA's general community-management tools.

## Deployment and recovery

From the CLI, plan then execute using the returned plan ID:

```json
{"action":"plan","application":"hti-research-admin","environment":"production","release":"main-9104be4"}
```

```json
{"action":"execute","plan_id":"REPLACE_WITH_PLAN_ID"}
```

A successful request queues a durable job; it does not mean production is ready. Inspect with `{"action":"inspect"}` or follow Mission Control until **succeeded**. Reusing the same plan returns its existing job. A failed job requires a fresh plan against the new environment revision. Only one job runs per environment; this initial worker processes jobs serially across environments.

For restart and rollback, use `operation` in the CLI plan request:

```json
{"action":"plan","application":"hti-research-admin","environment":"production","operation":"restart","service":"ui"}
```

```json
{"action":"plan","application":"hti-research-admin","environment":"production","operation":"rollback"}
```

Inspection and planning are non-mutating. A direct specific signed owner instruction can authorize a plan. Otherwise the assigned agent proposes the exact frozen plan and the owner replies `approve` to that proposal. Other agents, unrelated approvals, changed plans and unassigned environments cannot authorize deployment. The local CLI is an owner/operator boundary and can execute a plan without a Buzz message; an agent cannot use its deployment token to access that boundary.

Container readiness must pass before success. Failed updates attempt to restore the previous images; failed recovery is reported explicitly. Named data volumes are retained. No database migrations, imports, destructive cleanup or database rollback are performed. Restart may repeat after an interrupted manager operation; persistent history records the recovered outcome.

The manager state directory contains `deployments/registry.sqlite3`, private application configuration, and credential files. Back up this directory together with the community state and Docker volumes. Use `team-builder upgrade --manager-only --runtime-image IMAGE` to update management and dashboard code without replacing worker containers. Gateway configuration reloads may interrupt active agent responses but keep detached UAT processes running.

## Current scope and limitations

- Linux host and Docker Engine only; application containers run on the same host as this Team Builder installation.
- Application specifications and release IDs are immutable. Configuration changes to service commands, ports, or volumes currently require a new application/environment registration under a new ID. Credential replacement is supported separately as described above.
- The current HTTP health probe invokes Python inside the application image. Images must provide `python`, and a successful container probe confirms the configured endpoint, not database readiness or a working user login.
- The deployment service does not build images, trigger CI, check out GitHub branches, manage DNS/TLS, or perform database migrations. The optional CI importer below registers verified releases automatically before an agent proposes deployment.
- Agent deployment-access assignment is not exposed in Mission Control. Profiles and secrets are (see below).

### Managing profiles and secrets in Mission Control

Open **Deployments → an application environment → Manage profiles and secrets**. A signed-in owner can:

- **View** every profile: values, secret and file references, required variables and connection checks, plus which profile is deployed and which one the release policy uses.
- **Copy & edit** a profile. Profiles stay immutable, so the edited copy is saved under a new ID (the next `-vN` is suggested) and validated like any other profile.
- **Run preflight** for a profile.
- **Use for releases**: point the release policy's profile for that environment at another profile. The change is refused if the policy changed since the page was loaded.
- **Redeploy** the environment's current release with a chosen profile, e.g. after rotating a secret. Repeating the request never queues a second deployment.
- **Store, replace or generate** secrets, including file credentials such as `users.yaml` (upload). Secrets referenced by a profile but not stored are listed as missing.
- **Reveal** a stored secret after re-entering the Mission Control access key. Key checks share the login throttle (five attempts per minute).

Mission Control serves plain HTTP. Because a reveal sends the secret to your browser, reach it only through an SSH tunnel to a loopback-bound port (as in the user guide) or a TLS proxy, never directly over a shared network.

Every change and reveal, including refused ones, is appended to `deployments/dashboard-audit.jsonl` in the state directory and shown as the environment's change log. Entries name the action, environment and profile or secret, never values. Secret changes reach the running application on its next deployment. Agents keep their own approval-based path; Mission Control actions are owner actions and need no Buzz approval.


## CI-backed releases (no COA required)

Cody develops and opens changes; a trusted GitHub Actions workflow on `main` tests and builds the application. The host polls GitHub over outbound HTTPS and imports successful releases. Cody may propose staging deployments; Oppo may propose production deployments. The owner approves in their respective channels. Importing a release **never deploys it**, performs migrations, or changes agent grants.

Deployment proposals are published under the assigned agent's identity. That agent must belong to the proposal channel in both the registry and authoritative Buzz state. COA does not join engineering or operations channels. An approval must be a signed owner reply to that agent's exact frozen proposal, in the same channel and assigned application/environment. Retrying publication reuses the same event.

### One-time setup

Register the application separately for `staging` and `production` and grant
Cody only staging and Oppo only production — normally as one agent proposal (see
Agent-led setup), otherwise with the CLI requests above.

Provision a named GitHub credential with access to the application repository and **Actions: read** (a classic PAT requires `repo` for private repositories). The importer uses the existing Team Builder credential store; never put a token in the configuration. Prefer a dedicated read-only credential for the importer where available.

**Manager-run importer (recommended).** Cody proposes `configure_release_sync`
with the repository, credential name, workflow, branch, environments and
services; after approval the manager polls GitHub every two minutes and records
status for Mission Control. No systemd unit or host Docker access is needed. The
operator can apply the same settings directly:

```json
{"action":"release-sync-config","application":"hti-research-admin","repository":"tengso/hti-research-admin","credential":"github-platform","services":["ui","api"]}
```

**Alternative: host timer.** The importer can still run on the host. Create `/home/ubuntu/release-sync.json`:

```json
{
  "repository": "tengso/hti-research-admin",
  "application": "hti-research-admin",
  "credential": "github-platform",
  "workflow": "release.yml",
  "branch": "main",
  "environments": ["staging", "production"],
  "services": ["ui", "api"]
}
```

Run once as the installation owner, who must have Docker access:

```bash
team-builder release-sync /home/ubuntu/release-sync.json
```

Install `/etc/systemd/system/team-builder-release-sync.service` (adjust paths/user for your installation):

```ini
[Unit]
Description=Import verified Team Builder application releases
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
UMask=0077
ExecStart=/home/ubuntu/projects/triflection_team_builder/.venv/bin/team-builder release-sync /home/ubuntu/release-sync.json
TimeoutStartSec=30min
```

And `/etc/systemd/system/team-builder-release-sync.timer`:

```ini
[Unit]
Description=Poll GitHub for verified releases
[Timer]
OnBootSec=1min
OnUnitInactiveSec=2min
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now team-builder-release-sync.timer
sudo systemctl start team-builder-release-sync.service
journalctl -u team-builder-release-sync.service --no-pager -n 30
```

If outbound HTTPS needs a proxy, configure `HTTPS_PROXY` and an appropriate `NO_PROXY` in a systemd service drop-in. Docker image import uses the artifact, so private GHCR pull credentials are not needed on the host. No GitHub runner, inbound webhook, or SSH access from CI is required.

### Workflow contract and trust

The application workflow must test first, build Linux amd64, smoke-test UI/API without operational databases, and publish an immutable GHCR image. See `examples/ci-release/` for the working HTI Research Admin workflow and image smoke script (adapt tests, service commands and ports for other applications).

The workflow uploads exactly `release.json` and `image.tar` as `team-builder-release-<run_attempt>` using `actions/upload-artifact@v4`. `image.tar` is `docker save` of a single image. The JSON contains `repository`, full `commit`, numeric `run_id` and `run_attempt`, `image` (local sha256 image ID), `registry` (`ghcr.io/<owner>/<repo>`), `digest` (registry manifest sha256), and `archive_sha256`. The image has the `org.opencontainers.image.revision` label set to the commit.

The importer verifies the repository, selected workflow, successful push/manual run on the configured branch, GitHub artifact checksum, bundle checksum, image configuration digest, platform and revision before loading and registering it. GitHub credentials are never forwarded to artifact storage. Protect `main` and changes to the release workflow: code merged there is trusted to produce executable releases. Owner deployment approval remains separate.

The same image is registered in both environments as `ci-<run_id>-<run_attempt>`. A release appears a few minutes after its workflow finishes: the importer polls every two minutes, then downloads and verifies the artifact. Meanwhile Mission Control and the agents' `list_releases` (`ci_import`) report the importer state `importing` with the release ID; `current` means up to date, and only `stale` (no successful poll for ten minutes) or `failed` needs attention. Repeated polls and interrupted registration are idempotent. The newest 20 successful runs are considered; expired or missing artifacts are skipped. Keep artifact retention long enough for outages and dispatch a fresh run if a missed artifact has expired. Downloads/unpacked bundles are capped at 4 GiB; allow space for the ZIP, image archive and Docker layers. This initial importer supports Linux amd64 only.

### Daily operation and visibility

1. Cody merges the reviewed application change to `main` (or an owner triggers the Release workflow).
2. CI tests and publishes; within a few minutes the host registers the immutable release.
3. Ask Cody in engineering: “List staging releases and propose deploying release `ci-…` to `hti-research-admin/staging`.” Reply `approve` directly to the published proposal.
4. After UAT passes, ask Oppo in Production Operations to propose the **same release** for production, and reply `approve` to that proposal.
5. Confirm the job reaches `succeeded` and validate application/database behavior. Acceptance of a job alone is not success.

Mission Control → Deployments → application details shows registered releases and image IDs, importer status/last check, running services, operation history and sanitized service diagnostics. Importer details live in the systemd journal and installation `release-sync/<application>/status.json`; failures record sanitized error classes and HTTP status, never credentials or signed download URLs. A successful check older than ten minutes is stale. History survives manager restarts. Existing UAT and production services are not restarted by import or manager-only upgrades.

### Approval replies and tool-call troubleshooting

Reply directly to the frozen proposal message published by `propose_deployment`, not to an agent's subsequent summary. Bare `approve` is accepted. Buzz's `@Agent Name approve` is also accepted when the signed mention identifies the proposal's author; arbitrary mentions, added instructions, wrong reply targets and agent-authored approvals are rejected. Known approval rejections return a specific safe explanation.

Deployment-enabled agents expose their typed tools directly (`tools.tool_search.enabled: off`). This avoids Hermes's generic `tool_call` discovery wrapper producing name-only calls with no nested arguments. It does not change model selection or deployment permissions. A missing-argument failure does not prove provider corruption; inspect the recorded call first. After changing tool exposure, use a fresh Buzz thread if the previous conversation keeps repeating stale wrapper calls. Release synchronization itself does not increment the application's deployment revision or invalidate a frozen plan.

## Environment profiles and automated preflight

Provision an application's executable specification and named credentials once as
its local operator. The assigned release agent can then select a profile, check
prerequisites, and propose one combined configuration-and-release plan. This is
identical for staging and production and does not require COA. Credentials,
login files and database accounts remain separate per environment.

For example, create a private credential request and import its value from a file:

```json
{"action":"credential","application":"my-app","environment":"production","id":"database-password"}
```

```bash
team-builder deployment credential.json --secret-file /private/path/db-password
```

Use `"generate":true` instead of `--secret-file` for a generated API token.
Import login/configuration files the same way with a different credential ID.
Existing values are preserved on retry; changing a value requires `"rotate":true`.
Values are never returned by the API or made available to agents. Version files
are readable inside the application container through individually mounted,
read-only files, with private parent directories on the host.

Register a profile using a request such as:

```json
{
  "action": "profile",
  "application": "my-app",
  "environment": "production",
  "profile": {
    "id": "standard-v1",
    "values": {"DATABASE_HOST": "production-db", "LOGIN_FILE": "/app/config/users.yaml"},
    "secrets": {"DATABASE_PASSWORD": "database-password", "API_TOKEN": "api-token"},
    "files": {"/app/config/users.yaml": "login-file"},
    "required_env": ["DATABASE_HOST", "DATABASE_PASSWORD", "API_TOKEN", "LOGIN_FILE"],
    "connections": [{"id": "database", "host": "production-db", "port": 3306}]
  }
}
```

```bash
team-builder deployment profile.json
```

Profiles are immutable: register a new ID to change configuration. Secret and
file references resolve only within the specified application and environment.
All services in that environment receive the profile's variables and files;
register separate applications if they require different trust boundaries.
File targets are restricted to single filenames under `/app/config/` or
`/run/application-config/`. The operator must attach external dependencies to
the application's Docker network with the configured DNS aliases.

The assigned agent's workflow is:

1. `list_environment_profiles` and `list_releases` for its application/environment.
2. `check_deployment_preflight` with the selected `profile`.
3. `plan_environment_configuration` with `profile` and `release` for a combined
   deployment, or `plan_deployment` with both fields.
4. `propose_deployment`, then consume the owner's direct reply using
   `approve_deployment` and monitor `get_deployment_operation` to completion.
5. Report actual health and diagnostic results, including incomplete checks.

A configuration-only plan omits `release`; approval records the profile for the
next deployment and does **not** reconfigure running containers. A restart also
keeps existing container configuration. To apply new values to containers,
approve a deployment with the desired profile and pinned release.

Preflight checks credential availability, required environment variables, mounted
file existence, and optional TCP reachability from an isolated helper on the
application network. It also runs before execution, before replacing application
containers. TCP success does not validate database passwords, grants, schemas,
login roles or migrations. Application health remains the post-deploy gate.
A failed preflight leaves existing application containers intact. Agents cannot
supply arbitrary probe commands or provision credentials through these tools.

Plans pin profile and credential versions. Credential rotation invalidates pending
plans; create a new plan and obtain fresh approval. Old versions remain available
for existing mounts and recovery. No database migrations or role grants are
performed automatically. Back up the whole deployment state directory securely.

Mission Control's deployment detail view shows the active profile, available
profile/reference names, last preflight time, individual check results, and
operation outcomes. Values and file contents are excluded. Checks are performed
on request and execution, not continuously; their timestamps indicate their age.

## Automatic UAT acceptance and production promotion

The owner (by approving a release agent's proposal) or the operator can authorize
routine releases once, through an **enabled release policy**. This is a separate authorization mode from per-release Buzz approval.
Existing applications remain manual unless a policy is explicitly enabled.

The manager selects the highest registered `ci-<run_id>-<attempt>` release, checks
that staging and production registrations have identical commits and images,
applies the configured staging profile, runs acceptance checks, then hands the
same release to the production role. It records exact UAT configuration,
revision, container/image identities and check outcomes; changed or unhealthy UAT
invalidates production execution. Already healthy deployments with the exact
release and profile are checked without unnecessary container replacement.

The durable workflow belongs to the manager. Cody and Oppo are accountable agents,
not processes that must stay in a long chat loop. Agent scope/state is checked at
queue/execution time. Their tokens cannot change policy or checks without owner approval,
handle supplied credentials or manufacture acceptance evidence. Stopping/revoking an assigned
agent prevents its automatic stage from executing. COA is not involved.

### Enable a policy once

Register both applications, profiles, credentials, CI releases and agent grants
first. Acceptance checks are commands executed inside registered application containers;
they come from the operator or an owner-approved agent proposal. Use bounded **read-only, repeatable** checks. Raw output is
discarded; only named pass/fail results are recorded. Do not embed credentials in
commands. Tests use credentials already available to the application.

For the HTI example, generate a policy using the sample file from this checkout:

```bash
python examples/release-automation/hti-policy.py --enable > automatic-releases.json
team-builder deployment automatic-releases.json
```

Review/adapt the file before enabling on a different application. Enabling it can
immediately process the newest registered CI release, including one that already
existed before policy registration. Checks are privileged input (from the operator or an owner-approved
proposal); application code and CI must already be trusted to run in the application container.
No command supplied by an agent or a Buzz message becomes a check without the owner approving the frozen policy that contains it.

The [HTI example generator](../examples/release-automation/hti-policy.py) checks UI
HTTP readiness, authenticated API readiness, a read of the existing CRM override
table, and login-file structure with hashed passwords. These are automated
acceptance checks, not proof that every UI workflow or user role works. Add your
application's read-only functional test entrypoints for broader coverage.

For other applications the policy fields are:

| Field | Meaning |
| --- | --- |
| `application` | Registered application ID |
| `enabled` | Explicit standing authorization for staging and production |
| `staging_agent`, `production_agent` | Agents with those exact deployment grants |
| `staging_profile`, `production_profile` | Operator-provisioned profiles |
| `checks` | One to eight `{id, service, command, timeout}` checks; timeout 1–300 seconds, run in both environments. `{environment}` in a command is replaced with `staging` or `production`, so one repository-owned entrypoint such as `["python", "-m", "acceptance", "--environment", "{environment}"]` can serve both. |
| `notification_channel` | Optional managed channel ID shared by both release agents (and the owner, for owner requests) |
| `production_rollback` | `agent` (default): the production agent may call `rollback_production` after failed production checks; `automatic`: the manager restores the previous release immediately; `off`: rollback needs an owner-approved plan |
| `staging_diagnostics`, `production_diagnostics` | `redacted` (default): assigned agents see masked output of failed checks and masked log tails; `summary`: pass/fail and recognized log entries only. Policies registered before these fields existed keep production at `summary` |

Prefer checks that live in the application repository: the development agent
maintains the acceptance entrypoint through reviewed pull requests, and the
policy only names the command. `examples/release-automation/acceptance.py` is a
template for such an entrypoint. Release agents can author and propose the whole
policy with `configure_release_policy`; enabling it still requires the owner's
approval.

Use a shared notification channel for two distinct agents. Handoff, completion and
failure messages mention the responsible agent and are retried using the same
signed event. Delivery failure does not stop deployment; dashboard records remain
available. Omit this field for one agent responsible for both scopes.

### Routine operation

Once trusted CI imports a release, no human needs to supply release IDs, profile
names, deployment commands or routine approval replies. The worker performs UAT,
acceptance, handoff, production deployment and production checks automatically.
Handoff, completion and failure notices mention the responsible agent with a `p`
tag, which wakes it through its buzz-acp gateway; a run blocked on a missing
supplied credential also sends the owner a notice with the exact host command.
The assigned agents follow their release runbook skill: `inspect_release_automation`
(including masked output of failed checks), `get_service_logs` with
`detail="redacted"`, a fix (code fixes arrive as a new CI release), then
`verify_release_checks` and `retry_automatic_release`. A blocked run permits a
maximum of two agent-requested retries. Repeated polling does not retry it. New
releases, credential versions or policy versions create a new run.

Failed UAT blocks promotion. A failed deployment uses existing image recovery
behavior. Failed production checks stop the workflow and notify the production
agent, which may restore the previous release (`production_rollback: agent`), or
the manager restores it immediately (`automatic`). Rollback restores images only;
it does not repair databases or roll back application data changes. Technical failures require investigation by
the assigned agent, not an owner approval loop. Missing credentials produce a
plain-language owner request. Database migrations and broader privileges remain
outside this policy and are never inferred from a failure.

Mission Control shows policy, responsible roles, release stage, status, reason and
owner-input requests. Exact check results and UAT evidence are available through
the automation status tool. Operator inspection:

```json
{"action":"automation-status","application":"hti-research-admin"}
```

To pause, submit the same policy with `enabled: false`. Queued automatic jobs
recheck policy before touching containers. A job already applying a release may
finish; pausing does not stop application containers. Changing checks or profile
selection creates a new policy version and invalidates older queued authority.
Manual frozen proposals remain available when automatic policy is disabled.


Connection checks start a short-lived helper container on the application network. Each Docker step of that probe may take up to two minutes, so checks also work on hosts with slow disks (where starting a container can take most of a minute); a reachable dependency still answers within three seconds. After installing a release, a deployment waits up to five minutes for every service to report healthy. Agent deployment tools allow four minutes per request for the same reason.

If the manager refuses a host-side `team-builder deployment` request, the command prints the manager's reason (for example `Error: Credential exists; explicitly rotate it`) and exits with status 1; these messages never contain submitted values. A Docker or connectivity problem is reported separately.
