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
pip install 'https://github.com/tengso/triflection-team-builder/releases/download/v0.1.0/buzz_team_builder-0.1.0-py3-none-any.whl'
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
GitHub provisioning remain outside this version.

Retries reuse recorded operation outcomes. If invite creation is interrupted
before its result is saved, the upstream API cannot resolve that ambiguity by
request ID: management reports an unknown outcome rather than minting duplicates.
Inspect Buzz before authorizing another invite ID.

## Upgrade an existing installation

Install the new wheel in the VM's virtual environment, then run `team-builder upgrade`.
The command updates the manager and running agents, preserving identities, workspaces,
channels, and infrastructure volumes. It saves the previous Compose/config files under
`STATE/upgrades/`. Use `--runtime-image` for an explicit runtime image; `init` continues
to resume with saved image pins. An interrupted upgrade can be retried with the same
image. If readiness fails, inspect manager logs before retrying.

## Operation and recovery

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
