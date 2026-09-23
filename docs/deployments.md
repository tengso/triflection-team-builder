# Deployment management service

Deployment management is a generic, installation-scoped component of the Team Builder toolkit. The owner or Chief of Agents (COA) can assign it to any selected managed agent. **Cody is an example assignee, not a dependency or a special agent role.** Application registrations, releases, credentials, and deployment history belong to the installation rather than an agent workspace.

## Architecture and lifecycle

The deployment service runs inside the Python management service, with its own persistent SQLite registry, background job worker, and observation loop. It is not a separate service container. Agents access a scoped MCP facade; they receive neither Docker access nor production application secrets through that facade.

Applications run as separate Docker containers on the Linux hosting VM, on networks separate from agent containers. Each application/environment owns a private network, labeled containers, and optional persistent data volumes. The manager is the component that controls these resources.

| Component | Responsibility |
| --- | --- |
| Local owner/operator | Register application specifications, provision secrets, register verified releases, assign agents, and operate deployments through the CLI. |
| COA | Assign or revoke deployment access through owner-authorized community-management operations. |
| Selected agent | Inspect assigned applications, propose deployments, and submit owner-authorized deploy/restart/rollback plans. |
| Deployment service | Enforce scope and authorization, persist jobs, control host containers, check readiness, and record outcomes. |
| Mission Control | Display service health, releases, operation history, and sanitized diagnostics. |

Stopping, archiving, renaming, or restarting an assigned agent does not stop its assigned production applications. Revoking access prevents future deployment tool requests; already queued, owner-authorized jobs continue independently. A manager restart temporarily interrupts management and observations, while application containers keep running and durable unfinished jobs resume when the manager returns. A VM shutdown stops both; application containers use Docker's `unless-stopped` restart policy.

Assignments are per **agent ID + application ID + environment**. One agent can manage multiple application/environment pairs, and multiple agents can be assigned the same pair. `staging` and `production` are separate scopes: a staging grant does not grant production access. No assignment automatically grants blanket approval for changes.

## Dashboard visibility

Mission Control → **Deployments** shows service health, pinned releases, published ports, persistent operation history, and timestamped diagnostic logs. **Activity** also shows deployment outcomes. Logs are bounded diagnostic summaries; arbitrary application output, request bodies, and credentials are excluded. All timestamps use the browser's timezone. Observations become stale after 15 seconds and continue independently of deployment work.

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

The service saves the assignment, adds the managed `deployments` MCP connection and deployment instructions to that agent, and reloads a running agent's gateway. Its container and detached application processes are retained; an active agent response may be interrupted. Stopped agents load the configuration on their next start and cannot use the deployment tools while stopped. No manual token copying or MCP catalog entry is needed.

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

Every tool takes an `application`; `environment` defaults to `production`. Only assigned scopes are accessible.

| MCP tool | Purpose and additional parameters |
| --- | --- |
| `inspect_application` | Read current service health, release, and recent jobs. |
| `list_releases` | List operator-registered immutable releases. |
| `get_service_logs` | Read bounded sanitized diagnostics for `service`. |
| `plan_deployment` | Freeze a plan with `operation` (`deploy`, `restart`, or `rollback`), `release` for deploy, and optional `service` for restart. Does not execute changes. |
| `propose_deployment` | Publish the frozen `plan_id` for approval in the thread identified by `source_event_id`. |
| `execute_deployment` | Queue `plan_id` using a direct, specific signed owner instruction identified by `source_event_id`. |
| `approve_deployment` | Execute the exact frozen proposal referenced by the owner's `approval_event_id`. |
| `get_deployment_operation` | Read progress and the terminal outcome of `operation_id`. |

The public `plan_deployment` MCP tool and the local CLI plan request both use `operation`. The MCP facade translates this to `action_type` internally; agents do not need to construct internal HTTP requests.

Assigned agents cannot register applications, inject executable specifications or volumes, provision production credentials, or register releases. Those remain local-operator actions. Deployment access does not give an ordinary agent COA's general community-management tools.

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
- Deployment controls and agent deployment-access assignment are not exposed in Mission Control yet. Its deployment views are read-only.

## CI-backed releases (no COA required)

Cody develops and opens changes; a trusted GitHub Actions workflow on `main` tests and builds the application. The host polls GitHub over outbound HTTPS and imports successful releases. Cody may propose staging deployments; Oppo may propose production deployments. The owner approves in their respective channels. Importing a release **never deploys it**, performs migrations, or changes agent grants.

Deployment proposals are published under the assigned agent's identity. That agent must belong to the proposal channel in both the registry and authoritative Buzz state. COA does not join engineering or operations channels. An approval must be a signed owner reply to that agent's exact frozen proposal, in the same channel and assigned application/environment. Retrying publication reuses the same event.

### One-time host setup

Register the application separately for `staging` and `production`, including its host ports, runtime secrets, users file, and database network, using the registration instructions above. Grant Cody only staging and Oppo only production. These privileged specifications and credentials remain operator-managed; subsequent verified image releases are registered automatically.

Provision a named GitHub credential with access to the application repository and **Actions: read** (a classic PAT requires `repo` for private repositories). The importer uses the existing Team Builder credential store; do not put a token in this JSON or grant it to Oppo. Prefer a dedicated read-only credential for the importer where available.

Create `/home/ubuntu/release-sync.json`:

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

The same image is registered in both environments as `ci-<run_id>-<run_attempt>`. Repeated polls and interrupted registration are idempotent. The newest 20 successful runs are considered; expired or missing artifacts are skipped. Keep artifact retention long enough for outages and dispatch a fresh run if a missed artifact has expired. Downloads/unpacked bundles are capped at 4 GiB; allow space for the ZIP, image archive and Docker layers. This initial importer supports Linux amd64 only.

### Daily operation and visibility

1. Cody merges the reviewed application change to `main` (or an owner triggers the Release workflow).
2. CI tests and publishes; within a few minutes the host registers the immutable release.
3. Ask Cody in engineering: “List staging releases and propose deploying release `ci-…` to `hti-research-admin/staging`.” Reply `approve` directly to the published proposal.
4. After UAT passes, ask Oppo in Production Operations to propose the **same release** for production, and reply `approve` to that proposal.
5. Confirm the job reaches `succeeded` and validate application/database behavior. Acceptance of a job alone is not success.

Mission Control → Deployments → application details shows registered releases and image IDs, importer status/last check, running services, operation history and sanitized service diagnostics. Importer details live in the systemd journal and installation `release-sync/<application>/status.json`; failures record sanitized error classes and HTTP status, never credentials or signed download URLs. A successful check older than ten minutes is stale. History survives manager restarts. Existing UAT and production services are not restarted by import or manager-only upgrades.
