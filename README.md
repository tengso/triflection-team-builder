# Buzz Team Builder

A small Linux bootstrapper for a conversational Buzz team. It creates the Buzz
services, **Chief of Agents (COA)**, and **Office Of COA**. Tell COA what team you
need; there is no team blueprint to maintain.

## Install and initialize

Requirements: Linux x86-64, Python 3.12+ with pip and venv support, and Docker Engine
with Compose v2+. The default Buzz and Hermes images are published in GitHub
Container Registry under `ghcr.io/tengso/triflection-team-builder/`. Installation
pulls the prebuilt runtime, including the management service, and records immutable
image IDs. It does not need Rust, a Hermes checkout, or a local Docker build.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install 'https://github.com/tengso/triflection-team-builder/releases/download/v0.5.0/buzz_team_builder-0.5.0-py3-none-any.whl'
team-builder init \
  --bind 0.0.0.0 \
  --port 3100
```

Answer the prompts for community name, advertised URL (for example
`http://ubuntu.orb.local:3100`), default model, owner key, and provider key. The
default provider is OpenRouter; `--provider openai` and
`--provider custom --base-url URL` select OpenAI-compatible alternatives.

Use the advertised URL to connect your Buzz app, then open **Office Of COA**.
The owner can speak to COA there without mentioning it. Invited agents must
mention COA or reply to it. Ordinary agents respond to mentions and replies in
their assigned channels.

For automation, use `--non-interactive` and supply `--name`, `--advertised-url`,
`--model`, `--owner-key-file`, and `--provider-key-file`, in addition to image and
network settings if overriding the defaults. Each value flag also supports the matching environment variable,
such as `TEAM_BUILDER_STATE_DIR` or `TEAM_BUILDER_MODEL`. The provider credential
also supports `TEAM_BUILDER_PROVIDER_KEY`. Private keys are never command-line
values. Protect input files; the CLI does not delete files you supply.

The owner key is used only in the CLI process to authorize and register COA. It
is never written into installation state, the registry, logs, or containers.
Subsequent agents are owned by COA, not directly by the human owner. The provider
credential is stored privately and shared with each agent runtime.

To use your own images, pass `--buzz-image` and `--runtime-image`. For an unbundled
Hermes base with the native Buzz adapter and `buzz` CLI, use `--hermes-image`
instead; this explicitly enables the local compatibility build. Existing
installations keep their saved images when resumed. Image source pins, publishing,
and release validation are documented in [packaging/README.md](packaging/README.md).

### Client tunnels and internal agent connections

`--advertised-url` is the client-facing community URL, including the address used
in discovery and media links. `--internal-url` optionally supplies a separate
agent connection URL; for this Compose installation use `http://relay:3000`.
`--bind` and `--port` control the server's published socket independently.

The v0.5.0 installer above includes these options and selects compatible images.
For a client that can reach the server only through an SSH tunnel, initialize on
the Linux server:

```sh
team-builder init \
  --advertised-url http://127.0.0.1:3400 \
  --internal-url http://relay:3000 \
  --bind 127.0.0.1 --port 3100
```

On the workstation, keep this tunnel open and join `http://127.0.0.1:3400` in Buzz:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:3400:127.0.0.1:3100 ubuntu@ubuntu.orb.local
```

Replace the SSH destination for another server. Each client needs its own tunnel
using the advertised local port. Agents connect directly over Docker networking
and keep working when a workstation's tunnel closes. The server can publish only
on loopback because agents no longer depend on its published port. Mission Control
uses a separate listener and needs its own tunnel if enabled.

Automation also supports `TEAM_BUILDER_INTERNAL_URL`. Omitting `--internal-url`
preserves the original shared-URL behavior and validation. Existing installations
resume with their stored addresses; `init` does not change an existing community's
address or identities. When supplying custom images, both images must carry the
`io.team-builder.internal-relay=1` capability label; incompatible images are
rejected before starting services. The default v0.5.0 image pins include support.
See [image build instructions](packaging/README.md) for custom builds.

The relay maps only the explicitly configured internal host and port to the same
community. Unknown hosts cannot access community data, and signed HTTP/WebSocket
requests must match the host they actually use. The internal URL is an
authenticated connection address, not an additional management endpoint.

### Agent HTTP proxy

Starting with v0.5.1, configure an unauthenticated HTTP forward proxy on the Linux
host where the team runs (inside the LXD container, if applicable):

```bash
team-builder proxy set --url http://PROXY_SERVER:3128
team-builder proxy status
# Return to direct connections:
team-builder proxy disable
```

Upgrade the CLI and management runtime to v0.5.1 first. This setting persists in
installation state and applies to existing and future agents. It supplies
`HTTP_PROXY`/`HTTPS_PROXY` and lowercase equivalents to agent gateways; tools
inheriting that environment may also use the proxy. HTTPS providers use HTTP
CONNECT through the proxy, retaining TLS certificate verification. Proxy
authentication and SOCKS proxies are not supported by this command.

Internal Buzz and management addresses, localhost, and infrastructure service
names are excluded through `NO_PROXY`. The proxy must be reachable from the agent
Docker network; localhost would refer to the agent container and is rejected.
Changing the setting restarts the manager and refreshes running agent gateways;
active conversations may be interrupted, but agent containers and detached apps
are retained. Existing detached processes retain their previous environment.
Stopped/archived agents stay stopped. If application fails, the setting remains
saved; fix the reported problem and retry the same command. This does not configure
Docker image pulls or the Ubuntu/LXD host's own network proxy.

## Build a team through conversation

Ask COA: “I need a software team to build a small internal application.” It will
discuss roles and channels, then post a proposal. Reply **approve** directly to
that proposal to authorize its exact operations. COA performs the agreed batch
without requesting approval for each step.

Specific owner instructions such as “Stop the engineer” can execute immediately.
Requests from other agents always require owner approval. Only agents explicitly
invited join the office; creating an agent does not invite it automatically.

COA can manage agents, channels, human memberships and invitation links, shared
model-provider settings, and native Buzz projects. Its MCP tools are `inspect_team`,
`inspect_projects`, `propose_changes`, `execute_direct`, `execute_proposal`, and
`store_provider_credential`. Mutations use the same signed owner authorization
and frozen-proposal checks, including project operations.

- Channels: create, rename, change descriptions/visibility, manage members, delete.
- Agents: create, update instructions/model, start, stop, archive, change channels.
- Humans: create expiring, use-limited community invite links; add/remove channel
  members by public key. Invite links grant community access, not private-channel access.
- Projects: create/update/delete native NIP-MP records, link a channel, and attach
  or detach repository announcement coordinates. Use `link_github_repository(project,url)`
  to link an existing GitHub URL directly: Buzz announcement creation is automatic,
  and existing links are preserved. HTTPS repository URLs with optional `.git`
  suffix/trailing slash are accepted; branch/file URLs and embedded credentials
  are rejected. Registration does not check GitHub existence or grant clone/push
  access; GitHub credentials remain separate. Listed/unlisted controls
  discovery, not confidentiality; project metadata is community-visible. Project
  records are owned by the management identity and survive COA removal. Deleting
  a project preserves its repositories and channel.
- Providers: store named credentials, change the shared provider/default model,
  and restart running workers. Existing per-agent model overrides remain in effect.

To add a credential without putting the key into chat history, run on Ubuntu:

```sh
team-builder credential research-provider
```

Then tell COA the provider, model, and credential name. Custom OpenAI-compatible
providers also require a base URL. COA can store a key supplied directly by its
owner through `store_provider_credential`; keys pasted into Buzz remain in chat
history. The manager excludes keys from approval proposals, operation records,
and inspection responses. Named credentials are immutable: rotation uses a new
name. Provider changes persist atomically in the private `provider.json` file.

COA and its office can now be renamed, reconfigured, stopped/archived or deleted
by owner instruction. Archiving retains agent workspaces. Stopping/archiving COA
ends its replies; deleting its last channel stops it. Move COA and the owner to
another channel first to keep conversation-based management available there
(address COA by mention/reply). The manager accepts signed owner instructions in
any managed channel. Restarting the manager does not resurrect removed resources.
Automatic agent unarchive, integration installation, and repository hosting or
GitHub repository creation remain outside this version.

## GitHub access for agents

On the Linux installation host, provision a named token without sending it to chat:

```sh
team-builder github-credential github-platform --env-file /path/to/.env
team-builder github-access software-engineer --credential github-platform
```

The env-file reader loads only `GITHUB_TOKEN`, without executing shell code. Use
`--key-file` for a file containing only the token, or omit both flags for a hidden
prompt. The manager stores it privately; only assigned agents receive a read-only
token file. Tokens are absent from Docker environment metadata, Git URLs, Buzz
records, proposals, and inspection output. An assigned agent can read its token;
use a fine-grained PAT restricted on GitHub to the intended repositories and
permissions. Grant Contents read for cloning, read/write for pushes, and Pull
requests write if the agent should manage PRs. Agent assignment does not narrow
the permissions of a broad token.

For future team building, tell COA: “Create a software engineer in Platform and
assign GitHub credential github-platform.” COA can include `github_credential`
when creating an agent, or use `configure_github_access(agent, credential)` after
creation. These changes require the same verified owner authorization as other
management operations. COA receives the credential name, not its value, unless
you explicitly grant GitHub access to COA itself.

HTTPS `git clone`, `fetch`, and `push` authenticate automatically, with a credential
helper limited to `https://github.com`. GitHub CLI is not required. API clients can
read the assigned `GITHUB_TOKEN_FILE` privately. GitHub repository announcements
alone do not grant clone or API access.

```sh
team-builder github-access software-engineer --revoke
```

Revocation removes the mounted token and refreshes running workers; stopped workers
stay stopped and workspaces remain intact. Rotate by storing a new credential name
and assigning it. Also revoke the old token on GitHub if it must become unusable
outside this installation. Interrupted local grants can be retried using the
printed `--request-id`; the manager records operator authorization separately from
signed Buzz messages.

Retries reuse recorded operation outcomes. If invite creation is interrupted
before its result is saved, the upstream API cannot resolve that ambiguity by
request ID: management reports an unknown outcome rather than minting duplicates.
Inspect Buzz before authorizing another invite ID.

## Upgrade an existing installation

Install the new wheel in the VM's virtual environment, then run `team-builder upgrade`.
When managed instructions, configuration, or operation schemas change, workers refresh
Hermes' saved system prompts and tool lists for continuing sessions. Conversation
history is preserved; unchanged restarts retain the cached prompts.
The command updates the manager and running agents, preserving identities, workspaces,
channels, and infrastructure volumes. It saves the previous Compose/config files under
`STATE/upgrades/`. Use `--runtime-image` for an explicit runtime image; `init` continues
to resume with saved image pins. An interrupted upgrade can be retried with the same
image. If readiness fails, inspect manager logs before retrying.

`upgrade` does not replace the Buzz relay image or change community addresses.
The new split-URL bootstrap is available for fresh installations; automatic
migration of an existing community to a different client URL is not implemented.

## Operation and recovery

When an agent requests command approval in Buzz, the community owner can reply
directly to that prompt with `/approve` or `/deny`. If the app attaches replies to
the thread root, use the prompt's explicit `/approve REQUEST_ID` command in the
same channel. `/approve REQUEST_ID session` or `always` are accepted only when the
security check permits that scope. Expired, already answered, ambiguous, and
pre-restart prompts cannot authorize a newer command; ask the agent to retry for
a fresh prompt. No command-security checks are disabled by this routing support.

State defaults to `~/.local/state/team-builder/default`. Use a different
`--state-dir` and port for each community. Run the same `init` command again to
resume interrupted setup. If interruption happened before owner registration,
setup needs the owner key again; after registration, it does not.

Only the relay port is published. The default bind address is `0.0.0.0`; a specific
private-network IPv4 address is also supported. The advertised URL must use the
same port and a hostname/address reachable from the Buzz app. Loopback-only setup
and IPv6 are outside this version. Databases and the management HTTP service stay
inside the Compose network. This version uses private-network HTTP; domain and
certificate management are outside its scope.

```sh
docker compose -f ~/.local/state/team-builder/default/compose.yaml ps
docker compose -f ~/.local/state/team-builder/default/compose.yaml logs manager
docker logs CONTAINER_NAME_FOR_COA
```

Compose manages the infrastructure and manager. The manager creates agent
containers directly through Docker using installation-scoped labels; they are
not static Compose services. Use COA to stop ordinary agents. For whole-host
maintenance, stop the manager first, then stop containers selected by the
installation's `io.team-builder.project` label. Compose `down` alone does not stop
agent containers. Never use `down --volumes` when preserving community data.

Back up the installation directory and the installation's Postgres, Redis, MinIO,
and relay volumes together while services are stopped. Keep the original absolute
state path on restore. Docker restart policies recover containers after a host
restart; explicitly stopped and archived agents remain stopped. Partial operations
report their completed steps and can be retried using the original message.

The management service is a trusted host-level component because it holds the
Docker socket. Its API only accepts typed community operations. COA has the MCP
credential; ordinary agents have neither it nor Docker access. The service checks
signed Buzz authors and reply relationships independently of model claims. It
binds each authorized message to one operation batch. Interpreting a direct
owner instruction remains COA's responsibility; exact approval replies are checked
by the service for proposed batches.

## Development

```sh
pip install -e '.[dev]'
pytest -q
ruff check src tests
ruff format --check src tests
```

Unit tests cover signatures, access boundaries, proposals, retries, registration,
memberships, and lifecycle operations. Linux integration instructions and recorded
validation results are in `docs/validation.md`.

Local `vm-repair/` artifacts are retained as historical recovery material and
excluded from publication. The new package does not import or run the old builder.

## Mission Control

Mission Control shows one community's agents, services, channels, projects,
repository announcements, operation outcomes, and recent activity metadata.
Owners can configure agents and restart their gateways. Community structure and
command approvals stay in Buzz; host maintenance stays in Docker Compose.

On the Linux host, after installing/upgrading to v0.5.0:

```bash
team-builder dashboard enable
team-builder dashboard status
```

The enable command prints a separate owner access key once. Open the printed
URL on your private network (normally the community hostname on port `3101`)
and sign in. Only the key verifier is stored in installation state. Existing
installations keep the dashboard disabled until explicitly enabled. Fresh setup
also supports `team-builder init --dashboard`.

```bash
# Optional address/port and private one-time key delivery file:
team-builder dashboard enable --bind 0.0.0.0 --port 3101 --key-output ~/mission-control.key
team-builder dashboard rotate-key --key-output ~/mission-control-new.key
team-builder dashboard disable
```

All commands accept `--state-dir`. Rotation immediately invalidates dashboard
sessions; sessions also expire after eight hours or a manager restart. Re-running
enable preserves an existing key. Use rotation if it was lost. Enabling/disabling
recreates only the manager when its published port changes; identities and
workspaces are preserved. The internal management/MCP port is never published.

Observations refresh every five seconds and are marked stale after fifteen
seconds without a new snapshot. Container state, Docker health, the gateway's
live event-loop probe, and Buzz connection state are separate observations.
Hermes' state-change timestamp can legitimately be old for an idle gateway.
Unavailable data is shown as unknown; channel/project registry records are
labelled separately from signed Buzz readbacks. The community view covers
managed channels/projects plus the authoritative community membership roster.

Recent message/tool entries show metadata only. Logs contain recognized,
sanctioned diagnostic summaries from the last 100 container log lines; commands,
transcripts, credentials, and arbitrary output are omitted. Inspect full logs
locally with Compose when deeper troubleshooting is needed. Metric samples stay
in memory for at most an hour and reset with the manager. Operation times show the last recorded update; historical entries without a
recorded timestamp are labeled Not recorded. All dashboard times, including logs,
use the browser’s local timezone, identified in the footer.

This version supports private-network HTTP, a single owner access key, and one
installation. Keep the dashboard on a trusted private network. It has no terminal,
credential editor, transcript viewer, or automatic recovery.

In **Agents**, open an agent and choose **Restart agent…**. Owner confirmation
restarts only the Hermes gateway, keeping the container and detached app servers
running. Active agent turns, unsaved in-memory work, and attached terminal/PTY
sessions can be interrupted. Identity, configuration, history, and workspace files
are retained. Gateway restart bypasses Hermes' shutdown cleanup by terminating only
the gateway PID; it does not signal its process group or other app processes.

For app servers that must survive, launch them detached with their own log files:

```sh
nohup .venv/bin/streamlit run app.py --server.address 0.0.0.0 --server.port 18502 \
  > /work/streamlit.log 2>&1 < /dev/null &
```

Check existing listeners before relaunching apps after a gateway restart. Tool
process handles owned by the previous gateway are not restored. A full container
restart, stop, archive, image upgrade, or VM shutdown still stops all its processes.

Existing worker containers need a one-time runtime upgrade/container recreation to
install the stable supervisor. Schedule this when app interruption is acceptable.
The dashboard never falls back to restarting an old container; it reports that a
runtime upgrade is required. Only agents with running desired state can restart.
Watch gateway and Buzz health separately for reconnection; the result confirms the
new gateway process was launched, not that it is ready. Outcomes appear in Activity.


### Agent configuration

Open **Agents → an agent → Configure** to edit its display name, model, role
instructions, SOUL.md personality, built-in tools, managed skills, and MCP
connections. **Review changes** shows the before/after values; **Save and apply**
saves a new revision and restarts only the running agent's gateway. Active turns
may be interrupted; detached app servers remain in the container. Stopped agents
load the saved configuration on their next start.

The editor distinguishes the saved revision from the revision loaded by the last
witnessed gateway. Check gateway health for responsiveness. A pending apply retains
the saved revision and can be retried. Restore an old configuration by reviewing
and saving it as a new revision. Concurrent dashboard and COA edits fail with a
conflict instead of overwriting newer settings.

COA uses `inspect_agent_configuration`, then an owner-authorized `configure_agent`
operation with `expected_revision` and complete settings. `apply_agent_config`
retries activation. Existing `update_agent` operations also create revisions.
Ordinary agents cannot access these management tools.

Owners add immutable catalog entries in the configuration editor. Skills contain
Markdown instructions; new versions use new catalog IDs. MCP entries support
HTTP(S) endpoints with explicit allowed tool names (empty means none). For bearer
authentication, provision a named secret through `team-builder credential` first
(see `team-builder credential --help`), then enter only its name in the catalog.
Values are resolved into the assigned agent's private configuration and are never
returned by the editor. COA assigns approved catalog IDs; it cannot add arbitrary
MCP commands through these operations. COA retains its mandatory community
management connection, which ordinary agents never receive.

The prompt preview shows the managed SOUL.md layer, not Hermes' entire dynamic
system prompt. Managed skills supplement existing Hermes/project skills. Tool
selection controls exposure to the model; terminal access is not a security
sandbox. Remote MCP tools may themselves perform mutations.

Existing installations need a one-time worker image upgrade for the new loader.
This upgrade recreates containers and interrupts app servers. Afterward,
configuration-only changes reload gateways without recreating containers.

## Deployment management service

Team Builder includes a generic deployment service in its manager. The owner or
COA can assign any selected managed agent access to specific applications and
`staging` or `production` environments. Cody is one example assignee; the service
and production containers operate independently of agent identity and lifecycle.

The owner registers fixed application specifications, secrets, and immutable
releases through the CLI. Assign access with a `team-builder deployment` `grant`
request, or ask COA to use the owner-authorized `configure_deployment_access`
operation. The selected agent automatically receives scoped MCP tools for
inspection, deployment planning, owner-authorized deployment, restart, and
rollback. Access can be revoked or transferred without redeploying the application.

Mission Control → **Deployments** shows health, releases, persistent operations,
and sanitized diagnostic logs with local timestamps. Deployment actions and agent
access assignment currently use the CLI or Buzz rather than dashboard controls.

See the [deployment service guide](docs/deployments.md) for architecture,
[agent assignment and revocation](docs/deployments.md#assign-a-selected-agent),
the [MCP tool reference](docs/deployments.md#tools-available-to-assigned-agents),
and [deployment and recovery](docs/deployments.md#deployment-and-recovery).
