# Deploy applications to UAT and production

Use Team Builder to run applications in their own Docker containers, with
release agents doing the technical work: **Cody** (development, UAT) sets the
application up and fixes it, **Oppo** (operations, production) runs production.
The owner approves their proposals with one reply; the operator only supplies
secret values on the host. An enabled release policy automates routine releases;
without it, the owner approves each rollout in Buzz. Application containers keep
running when an agent restarts.

This guide uses **HTI Research Admin** as its example. Replace its repository,
service commands, settings, and dependencies with those of your application.
COA does not need to join release channels or operate deployments.

> **Version requirement:** agent-led setup, manager-run CI imports, masked
> diagnostics, on-demand verification and policy rollback need Team Builder
> v0.8.0 or newer for both the CLI and the manager/agent runtime images;
> the manual steps work from v0.6.0. Install the wheel linked in the
> [README](../README.md), then follow the
> [upgrade instructions](../README.md#upgrade-an-existing-installation).
> Existing installations must enable a standing release policy explicitly;
> upgrading alone does not authorize automatic production releases.

## Start here

- **New application?** Let the agents do it: [Agent-led setup](#agent-led-setup-recommended).
- **Want minimal owner work?** Enable [automatic releases](#automatic-releases-recommended) once.
- **Using manual approvals?** Go to [Deploy to UAT](#deploy-to-uat), then
  [Promote to production](#promote-to-production).
- **Doing setup by hand?** Use [Manual operator setup](#manual-operator-setup-alternative).
- **Deployment failed?** Use [Troubleshooting](#troubleshooting).

For the HTI installation on `myresearch` prepared on 26 September 2026, both environments have
an application registration, release access, and a `standard-v1` profile. Do not
repeat setup just to deploy another release. To move its host-timer importer into
the manager, ask Cody to propose `configure_release_sync` with the same settings,
then disable the systemd timer.

## Understand the four pieces

| Piece | What it means | HTI example |
| --- | --- | --- |
| Application | The services to run, their commands, ports and health checks | `hti-research-admin`, with `ui` and `api` services |
| Environment | An independent deployment and permission scope | `staging` for UAT; `production` for live use |
| Release | A known source commit and immutable container images | `ci-35807996920-1` |
| Profile | Configuration and references to privately stored credentials/files | `standard-v1`, registered separately in each environment |

**Always use `staging` in commands and tool arguments for UAT.** The supported
environment names are `staging` and `production`; `uat` is not a valid API value.
A release ID is not a branch name. Deploy a registered release, not `main` or a
mutable image tag such as `latest`.

| Responsibility | Who handles it |
| --- | --- |
| Service specification, profiles, generated credentials, database attachment, CI import, access grants and release policy | Cody proposes, the owner replies `approve`, Cody executes |
| Supplied secrets (database passwords, login files, GitHub token) | Operator, on the host, when an agent names the missing credential |
| CI release workflow, smoke test and acceptance checks in the repository | Cody, through reviewed pull requests |
| UAT investigation and fixes | Cody |
| Production monitoring, investigation and rollback | Oppo |
| Routine deployment authorization and acceptance | Standing release policy and automated checks; owner approval only when that policy is disabled or a change is outside its scope |
| Container deployment, health checks and operation history | Team Builder manager |
| Visibility into releases, services, checks and failures | Mission Control → Deployments |

Cody and Oppo are ordinary agent names. You can select different agents or grant
one agent both environments. This example keeps their responsibilities separate.

## Automatic releases (recommended)

After setup, Cody proposes a standing release policy and the owner approves it
once (the operator can also register it directly). The owner then needs no
technical deployment instructions or routine approval replies. Trusted CI registers a release; the manager runs UAT, records acceptance
results, and promotes the exact same images to production. Cody investigates UAT
failures and Oppo investigates production failures. COA stays out of the workflow.

Policy fields are described in [automatic release setup](deployments.md#automatic-uat-acceptance-and-production-promotion).
Prefer acceptance checks that live in the repository (e.g. `python -m acceptance
--environment {environment}`), maintained by Cody through reviewed pull requests.
For HTI, the included example policy checks the UI, authenticated API, database read access
and login configuration in both environments. These checks can be extended for
other applications. They do not replace all possible human product acceptance.

Watch **Mission Control → Deployments → Automatic releases**. A successful run
means its configured UAT and production checks passed. When a stage blocks, the
responsible agent is notified in the notification channel and investigates with
the masked output of the failed checks and masked service logs. Cody fixes
defects in the repository (the fix arrives as a new CI release), confirms with an
on-demand check run and retries at most twice per run; Oppo can restore the
previous production release when the policy allows. The owner is asked only for
a missing supplied credential — with the exact host command — or an actual
decision outside the agreed policy. Credentials go through private host setup,
never a chat message.

The manual UAT and production instructions later in this guide remain available
for installations that have not enabled automation. They are not required for
routine releases under an enabled policy.

## Agent-led setup (recommended)

1. **Owner, in Office Of COA (once):** create the agents if needed, then
   > Make Cody a release agent. Create a channel "Production Operations" with
   > Cody, Oppo and me.

   A release agent may propose new applications; Oppo gets its grant through
   Cody's proposal.
2. **Operator, on the host (once):** store a GitHub token that can read the
   repository and its Actions artifacts, unless one is already stored:
   `team-builder github-credential github-release-reader`.
3. **Owner, in Cody's channel:**
   > @Cody Set up `hti-research-admin` from `tengso/hti-research-admin` for UAT
   > and production. You operate staging, Oppo operates production. The MySQL
   > containers are `db-uat-mysql-1` and `db-prod-mysql-1`. Use the stored
   > GitHub credential `github-release-reader`.

   Cody reads the repository and publishes **one frozen proposal**: both
   application registrations, generated API tokens, a profile per environment,
   database attachments, grants for Cody (staging) and Oppo (production), and
   the CI import settings. Review it and reply `approve` directly to it; Cody
   then executes it.
4. **Operator, on the host:** Cody lists the supplied secrets that are still
   missing (for HTI: the database passwords and the login files), each with its
   exact `team-builder deployment … --secret-file …` command. Run those; nothing
   else is needed on the host.
5. **Cody, in the repository:** adds the CI release workflow, the image smoke
   test and an `acceptance` entrypoint through a pull request. After merge, CI
   publishes the first release and the manager imports it within minutes.
6. **Cody:** when preflight passes in both environments, proposes the release
   policy (checks calling the acceptance entrypoint, both agents, the
   notification channel, rollback and diagnostics settings). Reply `approve`.

From then on every merge to `main` flows to UAT and production automatically
(next section). Everything Cody proposes is validated by the manager and visible
in Mission Control; the manual steps below produce the same state.

## Manual operator setup (alternative)

Run host commands on the **Linux machine running Team Builder and Docker**, using
the installation owner's account. For the example:

```bash
ssh myresearch
source ~/projects/triflection_team_builder/.venv/bin/activate
mkdir -p ~/deployments/hti-research-admin
cd ~/deployments/hti-research-admin
umask 077
```

The examples use the default installation at
`~/.local/state/team-builder/default`. For another installation, add
`--state-dir /absolute/installation/path` to Team Builder commands and adjust the
state path in the networking example.

### 1. Prepare the application and dependencies

Before registering it, confirm:

- Its container image starts each service with a known command.
- HTTP services listen on `0.0.0.0` inside their containers, not just localhost.
- Each service has a health endpoint. The current health checker requires a
  `python` executable in the application image.
- Databases and other external dependencies already exist, with appropriate
  schemas and runtime-user permissions.
- You have separate environment settings and any application-specific login files.

The current service supports up to eight HTTP-checked services per application,
on the same Linux Docker host. The CI importer supports Linux amd64 and imports
one image used by the selected services. Manual release registration can pin a
separate image for each service. Applications that need a different runtime or
health-check mechanism need additional toolkit support.

For HTI Research Admin:

| Setting | UAT (`staging`) | Production |
| --- | --- | --- |
| UI port on host | `18502` | `38502` |
| UI port inside container | `8502` | `8502` |
| API port inside container | `8002` | `8002` |
| MySQL DNS name on application network | `uat-mysql` | `prod-mysql` |
| Assigned agent ID | `cody` | `oppo` |
| Login file | UAT accounts | Separate production accounts |

The API needs no published host port when only the UI uses it. A browser connects
to the UI's host port; the UI connects to `http://api:8002` inside Docker.
Applications with browser-side API requests may require a separately reachable
API URL and host port.

### 2. Register both environments

The following generates request files for the sample app. For another app, edit
the ID, repository, service commands, ports and health paths before running it.
`["ui"]` is an entrypoint command specific to the HTI application image.

```bash
python - <<'PY'
import json
from pathlib import Path
for environment, port in [('staging', 18502), ('production', 38502)]:
    request = {
        'action': 'register',
        'application': {
            'id': 'hti-research-admin',
            'environment': environment,
            'repository': 'tengso/hti-research-admin',
            'services': [
                {
                    'id': 'ui', 'command': ['ui'],
                    'port': 8502, 'host_port': port,
                    'health_path': '/_stcore/health',
                    'data_path': '/app/data',
                },
                {
                    'id': 'api',
                    'command': ['python', '-m', 'uvicorn',
                                'modules.research.crm.data_api_main:app',
                                '--host', '0.0.0.0', '--port', '8002'],
                    'port': 8002, 'health_path': '/health',
                    'health_token_env': 'CRM_REST_TOKEN',
                },
            ],
        },
    }
    Path(f'register-{environment}.json').write_text(json.dumps(request, indent=2))
PY

team-builder deployment register-staging.json
team-builder deployment register-production.json
```

Registration creates records, **not running application containers**. Linking a
repository to a Buzz project does not register an application or a release.
Service specifications are immutable. Repeating the identical registration is
safe; changing commands, service ports or volume paths requires a new application
ID. Profiles handle environment-variable and credential changes separately.

### 3. Provision credentials and register profiles

Prepare these private files on the host; use paths appropriate to your setup:

| File | Contents |
| --- | --- |
| `/private/staging/mysql-password` | UAT runtime database password, plain text |
| `/private/production/mysql-password` | Production runtime database password, plain text |
| `/private/staging/users.yaml` | Application-compatible UAT login configuration |
| `/private/production/users.yaml` | Separate application-compatible production login configuration |

These are placeholders, not files created by Team Builder. Replace the paths in
the commands below. Login-file structure, password hashing and role assignment
are application-specific. For HTI, the login file must follow the app's
`config/users.example.yaml`; a generic YAML file or plain password is insufficient.

Generate the non-secret request files:

```bash
python - <<'PY'
import json
from pathlib import Path
for env, host in [('staging', 'uat-mysql'), ('production', 'prod-mysql')]:
    base = {'application': 'hti-research-admin', 'environment': env}
    requests = {
        'db-credential': dict(base, action='credential', id='mysql-password'),
        'login-credential': dict(base, action='credential', id='login-file'),
        'api-credential': dict(base, action='credential', id='api-token', generate=True),
        'profile': dict(base, action='profile', profile={
            'id': 'standard-v1',
            'values': {
                'MYSQL_HOST': host, 'MYSQL_PORT': '3306', 'MYSQL_USER': 'app_runtime',
                'CRM_DATA_BACKEND': 'rest', 'CRM_REST_BASE_URL': 'http://api:8002',
                'CRM_REST_TRUST_ENV': 'false', 'AIME_USER_DB_PATH': '/app/config/users.yaml',
            },
            'secrets': {'MYSQL_PASSWORD': 'mysql-password', 'CRM_REST_TOKEN': 'api-token'},
            'files': {'/app/config/users.yaml': 'login-file'},
            'required_env': ['MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD',
                             'CRM_REST_TOKEN', 'AIME_USER_DB_PATH'],
            'connections': [{'id': 'mysql', 'host': host, 'port': 3306}],
        }),
        'preflight': dict(base, action='preflight', profile='standard-v1'),
    }
    for name, request in requests.items():
        Path(f'{name}-{env}.json').write_text(json.dumps(request, indent=2))
PY

team-builder deployment db-credential-staging.json --secret-file /private/staging/mysql-password
team-builder deployment login-credential-staging.json --secret-file /private/staging/users.yaml
team-builder deployment api-credential-staging.json
team-builder deployment profile-staging.json

team-builder deployment db-credential-production.json --secret-file /private/production/mysql-password
team-builder deployment login-credential-production.json --secret-file /private/production/users.yaml
team-builder deployment api-credential-production.json
team-builder deployment profile-production.json
```

For a different application, replace the HTI variable names and remove login-file
or database checks if they do not apply. Profile values must be strings. Put
secrets in credential files, not the profile's `values` map. All services in an
environment receive its profile variables and mounts.

Generated API tokens stay private; the manager gives the UI and API the same
value within each environment. Repeating `generate: true` preserves an existing
token unless rotation is explicitly requested. In the existing HTI installation,
production intentionally uses the UAT API token at the owner's request; these
setup commands will not replace it. Other applications can use separate tokens.

Profiles and references are scoped to the application/environment. Having
`api-token` in staging does not make it available in production. Profile IDs are
immutable: use `standard-v2` when changing the profile definition.

### 4. Connect dependencies and run preflight

Run the checks:

```bash
team-builder deployment preflight-staging.json
team-builder deployment preflight-production.json
```

If credentials and files pass, the TCP probe creates the labeled application
network if needed. For databases outside that network, the first TCP check can
fail until you attach the dependency. For existing MySQL Docker containers on the
same host, attach each to its matching application network:

```bash
TB_PROJECT=$(python - <<'PY'
import json
from pathlib import Path
print(json.loads((Path.home()/'.local/state/team-builder/default/config.json').read_text())['project'])
PY
)

docker network connect --alias uat-mysql \
  "${TB_PROJECT}-app-hti-research-admin-staging" db-uat-mysql-1

docker network connect --alias prod-mysql \
  "${TB_PROJECT}-app-hti-research-admin-production" db-prod-mysql-1
```

Replace database container names with yours. If Docker reports an existing
endpoint, inspect that network and confirm the alias rather than disconnecting a
live database. Preserve these attachments in the database stack's own deployment
configuration if its containers may be recreated.

For a remote database, use its reachable hostname in the profile and allow the
application host/network to connect. Do not use `localhost` for a database in a
different container or on the host.

Rerun both preflight commands. Proceed when `ready` is `true`. **TCP reachability
does not prove the database password, grants, schema, login roles or migrations
are correct.** Those require application-specific verification. Team Builder
never runs database migrations as part of this flow.

### 5. Assign the agents

The agents must already exist. The owner and each agent must belong to the Buzz
channel where that agent will publish proposals. Use the agents' stable IDs:

```bash
cat > grant-cody.json <<'JSON'
{"action":"grant","agent":"cody","application":"hti-research-admin","environment":"staging","allowed":true}
JSON
cat > grant-oppo.json <<'JSON'
{"action":"grant","agent":"oppo","application":"hti-research-admin","environment":"production","allowed":true}
JSON
team-builder deployment grant-cody.json
team-builder deployment grant-oppo.json
```

This installs the scoped deployment tools and reloads a running agent's gateway.
Its container remains running, although an active response can be interrupted.
Oppo needs no GitHub token or Docker access to operate registered releases.
COA needs no deployment grant or release-channel membership.

### 6. Make releases available

**Recommended: CI-backed releases.** The application repository's workflow tests,
builds and smoke-tests the image. A host importer registers the successful release
in both environments. Importing alone does not deploy it; an enabled standing policy automatically selects matching registered releases for UAT and production.

1. Adapt the [sample workflow](../examples/ci-release/release.yml) and
   [image smoke test](../examples/ci-release/image_smoke.sh) to your application.
   Put the workflow in the application repository at `.github/workflows/release.yml`
   and the smoke script at the path it invokes.
   The importer requires the documented release artifact format; an ordinary
   image-push workflow alone is insufficient.
2. Provision the host importer's GitHub credential:

   ```bash
   team-builder github-credential github-release-reader
   ```

   Enter the token at the hidden prompt. It needs access to the repository and
   its Actions artifacts. This stores the credential without granting it to an agent.
3. Create and run the importer configuration:

   ```bash
   cat > release-sync.json <<'JSON'
   {
     "repository": "tengso/hti-research-admin",
     "application": "hti-research-admin",
     "credential": "github-release-reader",
     "workflow": "release.yml",
     "branch": "main",
     "environments": ["staging", "production"],
     "services": ["ui", "api"]
   }
   JSON
   team-builder release-sync "$PWD/release-sync.json"
   ```

4. Let the manager poll for new releases: submit the same settings as
   `{"action": "release-sync-config", ...}` with `team-builder deployment`, or
   have Cody propose `configure_release_sync`. A [host timer](deployments.md#one-time-setup)
   remains an alternative.
5. Check Mission Control for a registered `ci-<run_id>-<attempt>` release in each
   environment. It appears a few minutes after CI finishes; while the importer
   reports `importing`, wait. If no release is registered, do not ask an agent to
   deploy it yet.

**Alternative: manually register a verified image.** Build/pull it on the host,
record the full Git commit and immutable image ID/digest, then follow
[manual release registration](deployments.md#operator-setup). Register the same
release separately for staging and production. Merely having an image on disk
or registering the application is not enough.

## Deploy to UAT

In Cody's engineering channel, send this request, replacing the example release
with one actually listed in Mission Control:

> @Cody Prepare a UAT deployment for application `hti-research-admin`, environment
> `staging`, release `ci-35807996920-1`, using profile `standard-v1`. Run preflight.
> If it passes, publish a combined configuration-and-release proposal for my
> approval. After approval, monitor the operation and report service health.

Cody should inspect the release and profile, check prerequisites, freeze a plan,
and publish a proposal. Review its environment, release, services, profile and
credential references. Then **reply `approve` directly to the published proposal**.

Do not reply to a later summary, an older proposal or an unrelated thread. Reply
with plain `approve` (or the Buzz app's `@Cody approve`); since v0.7.0 there is no
separate `/approve` command.

Follow Mission Control → **Deployments → hti-research-admin / staging** until:

- The operation is `succeeded`.
- The current release is the one approved.
- Both sample services are healthy.

Then test login and representative application/database actions. A queued job,
running container, or healthy HTTP endpoint alone is not UAT acceptance.

## Promote to production

After UAT acceptance, ask Oppo to deploy **the same release**, using production's
profile and credentials. Do not rebuild `main` and assume the result is identical.

> @Oppo UAT has passed for `hti-research-admin` release `ci-35807996920-1`. Prepare
> its deployment to `production` using profile `standard-v1`. Run preflight and
> publish the combined configuration-and-release proposal. After my approval,
> monitor it to completion and report the deployed release and service health.

Review and reply `approve` directly to Oppo's new proposal. UAT approval does not
authorize production. Confirm `succeeded`, the correct current release, healthy
services, and production login/application behavior.

**Include both profile and release when applying new configuration.** A
configuration-only plan records settings for a future deployment but does not
change running containers. Restarting a container also retains its old environment.

## Open the application from your workstation

If the Linux host is directly reachable, use its address and the UI's published
port, subject to the configured bind address and network rules.

For the sample host reached over SSH, run this on your workstation and keep it open:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 18502:127.0.0.1:18502 \
  -L 38502:127.0.0.1:38502 myresearch
```

Open UAT at `http://127.0.0.1:18502` and production at
`http://127.0.0.1:38502`. Use a different local port if either is occupied.
The SSH destination must be the machine where Docker publishes these ports. If
SSH ends on an outer LXD host, use the container's reachable address as the
forward target, or tunnel through SSH into the container itself.

These application ports are separate from the Buzz relay and Mission Control
ports. The community's advertised URL does not set the application's address.

## Change configuration or credentials later

| Change | Operator action | Agent action |
| --- | --- | --- |
| New release, unchanged configuration | Let CI/importer register it | Preflight, propose release with current profile, obtain approval, monitor |
| Different ordinary settings or file references | Register a new profile ID | Propose that profile and a release together |
| Replace a password, API token or login file | Import replacement under its reference with `rotate: true` | Create a fresh profile-backed plan and obtain fresh approval |
| Change command, service port or data-volume path | Register under a new application ID; plan data/network handling separately | Operate the newly granted application scope |

Example rotation request, saved as `rotate-api-token.json`:

```json
{"action":"credential","application":"hti-research-admin","environment":"production","id":"api-token","rotate":true}
```

```bash
team-builder deployment rotate-api-token.json --secret-file /private/new-api-token
```

To deliberately share a token, import the same private token file into both
environments' references. Keep token values out of Buzz. Coordinate changes with
other consumers. The existing HTI production reference already matches UAT.

Rotation invalidates pending plans and is refused while a deployment is queued
or running. It does not update running application containers. Obtain a fresh
proposal using the profile, even if its name has not changed.

## Troubleshooting

| What you see | What it means / next action |
| --- | --- |
| No registered releases | Check the CI workflow and importer. Repository linking and application registration do not register releases. |
| Missing `CRM_REST_TOKEN` | For HTI, verify `api-token` is provisioned in that environment and mapped by the selected profile. Deploy the profile and release together; a restart will not add the variable. |
| Failed credential or file preflight | The agent names the missing supplied credential with the exact host command for the operator, or proposes a corrected profile; generated credentials it proposes itself. Then it reruns preflight. |
| Failed MySQL TCP check | The agent checks the recorded attachment, DNS alias and port and proposes `attach_dependency` if needed. No application containers have been replaced by the failed preflight. |
| Database access denied or missing table | Investigate database credentials, grants or schema with the database operator. TCP success does not validate them. |
| Login succeeds but access is denied | Verify the application's user-role mapping; a valid login file does not grant database-backed roles. |
| Approval rejected | Reply to the exact current proposal as the owner. Changed credentials/configuration or an intervening operation require a new plan. |
| Proposal cannot be published | Verify the agent and owner are members of the proposal channel and the agent has the exact application/environment grant (or is a release agent, for a new application). COA does not need to join. |
| Automatic run blocked, agent silent | Check the policy has a `notification_channel` containing both agents, and that the agents run v0.8.0 or newer (0.7.0 notices did not wake buzz-acp agents). Mention the agent to prompt it. |
| Production checks failed after promotion | Oppo inspects the masked check output; with `production_rollback: agent` it restores the previous release, with `automatic` the manager already did. |
| Agent says required tool arguments are missing | Start a fresh thread and verify its current deployment tools. Do not repeatedly approve or retry an empty tool call. |
| Running but unhealthy | Inspect the dashboard readiness reason and logs. Running is not ready; the application may still lack valid configuration or dependencies. |
| Failed first deployment, no previous release | There is no previous image to restore. Failed containers may remain. Correct the cause and approve a new plan. |
| Application works in Docker but not from workstation | Check host port, bind address, SSH destination and tunnel. A service with no host port is intentionally internal. |

A failed update attempts automatic recovery to the previous images when one
exists; inspect the recorded outcome rather than assuming recovery succeeded.
For an intentional restart or rollback, ask the assigned agent to propose that
specific action. Rollback restores images, not database contents; it does not
undo migrations or provide a general credential rollback.

For manual visibility on the host:

```bash
printf '%s\n' '{"action":"inspect"}' > inspect.json
team-builder deployment inspect.json
```

The operator CLI returns all application scopes for this installation. In Buzz,
the assigned agent's inspection is restricted to its grants. Mission Control
shows timestamps, profiles, preflight results, releases, service health and
operation history. Assigned agents can read the last 200 log lines with secrets
masked (`detail="redacted"`) in staging, and in production when the release
policy's `production_diagnostics` is `redacted`. Masked logs can still contain
application data. Do not paste credentials or raw configuration into a channel.

## A release is complete when

1. The intended immutable release is deployed in the intended environment.
2. The operation reports `succeeded` and required services are healthy.
3. Login and representative application/database checks pass.
4. The assigned agent reports the outcome, including any remaining limitations.

For the full tool/API reference, see [Deployment management service](deployments.md).
