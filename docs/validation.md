# Validation

The implementation was exercised on 11–12 September 2026 in a separate Ubuntu
`x86_64` deployment on the `ubuntu` OrbStack VM. Docker Engine was 29.8.0 and
Compose was 5.5.1. The existing HTI Research deployment and the old builder were
left unchanged.

## Results

- All 31 automated tests passed, along with Ruff lint and formatting checks.
- The distributable wheel built successfully with its runtime resources included.
- Fresh infrastructure setup and recovery from an interrupted bootstrap passed.
- Re-running `init --non-interactive --state-dir ...` after registration succeeded
  without an owner-key argument. Owner and agent identities were retained.
- COA connected to Buzz and exposed the management tools through Hermes MCP.
- A signed test-owner request made COA post a frozen proposal. A signed owner
  reply approved it, and COA created the requested channel and agent.
- The created agent published a valid ownership profile and complete managed-agent
  policy, joined its assigned channel as a bot, and was absent from the office
  until invited. When mentioned in Buzz, it replied `COA_SMOKE_OK` through the
  actual configured model provider.
- Office invitation, agent update, stop, start, membership removal, and archive
  passed against the real services. Repeating each authorized operation returned
  the stored result. Archive was checked against the relay-signed kind 13535 list;
  the workspace directory remained present.
- Local tests cover forged signatures, canonical HTTP host signing, reply
  ancestry, direct and proposed authorization, wrong-thread approvals, changed
  proposals, resource protection, retry persistence, secret exclusion, network
  preflight, provider mapping, and the HTTP authentication boundary.
- The final runtime rebuilt and resumed successfully without the owner key. The
  archived agent remained stopped after the manager restarted. A scan of the
  installation state found no persisted owner private key.
- The Buzz desktop displayed COA's ownership attribution and offered **Mention
  Chief of Agents** in the office composer. Selecting it inserted the correct
  mention. The draft was cleared without sending a message.
- Public NIP-11 metadata returned the configured name, `COA Smoke Test`.

The conversation test used OpenRouter with `moonshotai/kimi-k3`. Other provider
configuration paths have unit coverage; they were not tested with live credentials.
Signed discovery records, bot membership, and addressed-message delivery were
verified against the live relay. The visual picker check used COA; the lifecycle
test had already archived the child agent.

The desktop could not connect using the VM hostname, although a direct host HTTP
request succeeded. For the visual check only, the isolated community's host map
was temporarily switched to OrbStack's localhost forwarding. Its original host
was restored afterward. The existing desktop identity received temporary test
membership, which was revoked afterward; its original community and profile were
restored on screen. A saved localhost test connection remains in the desktop app.

## Reproduce on Linux

Install the project and development dependencies. Create an **isolated test**
community whose name contains `Smoke Test`; use a generated owner key, never a
community with real conversations. Bootstrap it using the normal `team-builder
init` command and a separate state directory and port.

```sh
pytest -q
ruff check src tests
ruff format --check src tests
sudo .venv/bin/python tests/integration/smoke.py \
  --state-dir /absolute/path/to/test-state \
  --owner-key-file /absolute/path/to/test-owner.key
```

The harness requires access to the Docker socket and UID-10000 workspace paths.
It posts test messages, uses the model provider, creates `smoke-dev` and
`smoke-engineer`, and archives that agent on success. Use a fresh test installation
for a second complete run after successful archival. An interrupted proposal can
be resumed. Run only one harness against an installation at a time.

## Published image validation — 2026-09-12

[Release workflow run 34666426450](https://github.com/tengso/triflection-team-builder/actions/runs/34666426450)
passed on a fresh Linux amd64 GitHub runner. It ran all 35 unit tests and lint
checks, built both images from pinned upstream sources, and verified Buzz CLI
startup and Hermes imports in a read-only container. It then bootstrapped an
isolated Compose installation, checked registration, office membership and COA
gateway readiness, and repeated setup without replacing identities or configuration.
Only after those checks passed did it publish the `0.1.0` tags.

An independent temporary Ubuntu VM installation also passed fresh bootstrap and
identity-preserving setup recovery using the published candidate runtime. Its
containers, volumes and generated keys were cleaned up afterward. The existing VM
deployment stayed healthy. Publication checks use a dummy provider credential;
they make no model calls. The live conversational validation recorded above was a
separate test.

The final installer wheel was also tested on Ubuntu with no image override flags.
Its pinned default images passed fresh bootstrap and setup recovery. Anonymous
Docker pulls succeeded for both released digests, and the `0.1.0` registry tags
were independently checked against the CI manifest. The wheel's packaged source
and resource files were compared byte-for-byte with the release checkout.

The installer pins the released Buzz and Hermes digests. The supporting MinIO
server and client use digest-pinned official Quay images because their Docker Hub
references rejected anonymous pulls; both Quay digests match the earlier VM inputs.

## Earlier local image inputs

These immutable image IDs were used for the original VM validation below. They
are local artifacts, not registry addresses. New installations use the published
images documented in [../packaging/README.md](../packaging/README.md).

| Input | Local image ID |
| --- | --- |
| Buzz relay | `sha256:649e9791b13929f38126d438111e9380dd8f68c6beb336bc93ddecac062f819a` |
| Hermes base | `sha256:bf6e766cddea3aad18c1190ec19b08e1fc4e064783cf8731910cd78e00dd9854` |

The Hermes base contains revision `d0df324862cb2244a40789a5ca45392165f1b950`
and the native Buzz adapter. The adapter patch is checked while building; an
incompatible source fails rather than silently losing message-author context.

Test source copy: `/home/ubuntu/agent_team/minimal-team-builder`.
Test state: `/home/ubuntu/.local/state/team-builder-smoke`.
Test Compose project: `tb-335d9adf-6ea` (port 3310).
Final built runtime: `sha256:b3f364bb64ec48e20e8d80ae5a3d936e19c9d499d360a8e6a03843be69d7626c`.
The isolated test containers are stopped; their state, workspaces, and volumes
remain available. The original deployment's containers were still healthy after
cleanup. Run the normal `init --non-interactive --state-dir ...` command to resume
the test installation.
The owner used for testing is a generated identity, separate from the user's Buzz
identity. No key or provider credential is included in this repository.

## 0.2.0 community management expansion

The local regression suite passes 45 tests, including native project CRUD and
repository references, human memberships/invite replay, credential secrecy and
provider persistence, cross-channel approval rejection, COA/office lifecycle,
and a state-preserving runtime upgrade.

A disposable Linux Compose deployment on the Ubuntu VM passed fresh bootstrap,
identity-preserving resume, and real relay validation of project create/update/
delete, invitation minting, human membership changes, public/private channel
changes, channel deletion, COA stop/start/archive, manager restart, and repeated
signed requests. Its generated owner identity and installation were deleted after
testing. Neither existing deployment was used for destructive test operations.

Run the same expanded protocol test with `EXERCISE_MANAGEMENT=1` when invoking
`tests/integration/bootstrap_images.py`. It only accepts the generated
`Image Smoke Test` community. This verifies relay/gateway behavior; it does not
make a paid model request or claim to validate new project workflows through a
live language-model conversation.

## Hosted deployments (2026-09-17)

- Builder: 120 tests passed; Ruff checks and formatting passed.
- Application main `9104be4c00f7c0e364fd86fd2e612c62518898c4`: 238 tests passed in a disposable image with no operational database credentials.
- Isolated Linux deployment: durable queue recovery, duplicate requests, actual service restart, image replacement, rollback, retained volume witness, snapshots and sanitized logs passed.
- Ubuntu production: UI and API healthy, UI reachable from workstation on 38502. Dashboard showed succeeded operation and timestamped service diagnostics; filter/keyboard clearing and desktop/mobile detail views checked.
- Live Cody deployment MCP and COA management schema verified. Agent container IDs, identities and channel memberships preserved; UAT health on 18502 still HTTP 200.
- This deployment uses a locally built pinned application image and local manager image; no new public image release was published.
- Production authentication/data configuration remains pending: the UAT checkout did not contain database credentials or users.yaml. Production dev-mode bypass is disabled. Container health is not evidence of working database access or user login.
