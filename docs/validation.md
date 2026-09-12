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

## Compatible image inputs used

These immutable image IDs are already available on the tested VM. They are local
artifacts, not published registry addresses. Other hosts must supply their own
compatible Buzz and Hermes images; this repository does not publish those upstream
images.

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
