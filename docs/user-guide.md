# Buzz Team Builder — User's Guide

Team Builder turns a Linux host into a self-hosted team of AI agents that you
manage by *talking* to one of them. You install a Buzz community (a Nostr-based
chat server with a desktop and mobile client), a trusted management service, and
one agent called **Chief of Agents (COA)**. Team changes — channels, agents,
memberships, projects, agent configuration — you ask COA for in chat, and the
human owner approves them with a reply. Secrets are stored on the host with the
CLI, never in chat. Application releases are run by assigned release agents and
the manager, without COA (section 9).

This guide is for the person who runs the installation and owns the team. It
covers concepts, setup, daily operation, application releases, and ends with a
complete worked example: a small financial data analysis team.

---

## 1. Concepts

| Term | What it is |
| --- | --- |
| **Buzz** | The chat platform. Channels, threads, mentions, projects, repositories and issues all live on the Buzz relay. You use the Buzz desktop or mobile app to talk to agents. |
| **Owner** | You. A Nostr key that COA recognises as the only identity allowed to authorise team changes. You sign messages with it in the Buzz app; the CLI uses it only to register COA and never stores it on the host. |
| **COA** | Chief of Agents — the management agent. It runs on Hermes and has a private set of management tools (inspect, propose, execute). It sits in **Office Of COA**. |
| **Office Of COA** | The owner's private channel with COA. The owner can talk there without mentioning COA; everyone else must mention it. |
| **Agent** | An ordinary worker: an LLM-driven CLI (Hermes, Pi, Codex or Devin) running in its own locked-down container, connected to Buzz through the upstream `buzz-acp` gateway. It answers when mentioned in a channel it belongs to. |
| **Harness** | The agent runtime: `hermes` (default), `pi`, `codex`, or `devin`. Chosen at creation, fixed afterwards. |
| **Manager** | A trusted host service that owns the Docker socket and the installation state. Every mutation goes through it; it verifies owner signatures itself and never trusts an agent's word. |
| **Proposal** | A frozen, exact list of operations: team changes posted by COA in the office, or deployment plans posted by an assigned release agent in its own channel. The owner replies `approve` directly to it to execute it as written. |
| **Release policy** | An operator-enabled standing authorization for an application: the manager deploys each verified CI release to UAT, runs acceptance checks, and promotes the same images to production, with release agents investigating failures (section 9). |
| **Mission Control** | An optional read-only web dashboard (agent health, channels, projects, operations) with a configuration editor for agents. |
| **Credential** | A named secret (`team-builder credential NAME`) stored privately on the host. Agents receive it as an environment variable or MCP header; its value never appears in chat, proposals, or the dashboard. |

### How a conversation turns into a team change

```
Owner in office:   "Create a channel Research and an analyst agent in it."
COA:               inspects the team → posts a frozen proposal (exact operations)
Owner:             replies "approve" to that proposal message
COA:               calls execute_proposal(approval event id)
Manager:           fetches the approval event from the relay, verifies the owner's
                   signature and that it replies to the untouched proposal,
                   then creates the channel and the agent container
COA:               reports the result
```

Small, unambiguous owner requests can be executed directly (COA's `execute_direct`
tool) — the manager still verifies that the *source message* was signed by the
owner. Requests from anyone else are rejected at the manager, no matter how COA
was persuaded.

### What agents can and cannot do

- Agents respond to `@mentions` in their assigned channels. One conversation
  session per channel; the gateway feeds recent channel history into each turn.
- Agents post their own replies with the `buzz` CLI, in the thread of the message
  that triggered them.
- Tool calls (shell, files, MCP) run **without per-command approval**. The
  container is the boundary: UID 10000, read-only root filesystem, dropped
  capabilities, no Docker socket, 2 GB memory, process limit, and only the
  network the manager grants. Treat terminal access as *capability*, not as a
  sandbox against a malicious model.
- Agents cannot call management tools. Team changes are always proposed to COA
  and approved by you. The only exception is scoped deployment access for an
  application/environment you grant to a release agent (section 9).

---

## 2. Requirements

- Linux x86-64 host (a VM is fine), Docker Engine with Compose v2.
- Python 3.12+ with `venv`.
- A model provider key: OpenRouter (default), OpenAI, or any OpenAI-compatible
  endpoint (`--provider custom --base-url URL`).
- A Nostr key for the owner. Use the one from your Buzz app if you want your own
  identity to own the team, or generate one for a dedicated owner.
- Optional: a Devin account key if you want `devin` agents; GitHub tokens if
  agents should push code.

---

## 3. Installation

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install 'https://github.com/tengso/triflection-team-builder/releases/download/v0.8.2/buzz_team_builder-0.8.2-py3-none-any.whl'
team-builder init --bind 0.0.0.0 --port 3100
```

`init` asks for:

| Prompt | Meaning |
| --- | --- |
| Community name | Shown in the Buzz app. |
| Advertised URL | What clients connect to, e.g. `http://ubuntu.orb.local:3100`. |
| Default model | e.g. `anthropic/claude-sonnet-4.5` on OpenRouter. |
| Owner private key | Hidden prompt (or `--owner-key-file`); used once to register COA, never saved. |
| Model provider API key | Hidden prompt (or `--provider-key-file` / `TEAM_BUILDER_PROVIDER_KEY`); stored privately for agents. |

It pulls the pinned Buzz and Hermes images, starts Postgres/Redis/MinIO/relay,
starts the manager, creates COA and Office Of COA, and prints `Ready: <name> at
<URL>` with the state directory. Open the Buzz app, connect to the advertised URL
with the owner identity, and open **Office Of COA**. The pi, codex and devin
images are pinned too and pulled on first use.

Useful variations:

```sh
# Automation: no prompts
team-builder init --non-interactive --name "Alpha Desk" \
  --advertised-url http://desk.example.internal:3100 --port 3100 --bind 0.0.0.0 \
  --model anthropic/claude-sonnet-4.5 \
  --owner-key-file ~/owner.key --provider-key-file ~/openrouter.key

# Reach the community through an SSH tunnel from your laptop
team-builder init --advertised-url http://127.0.0.1:3340 \
  --internal-url http://relay:3000 --bind 127.0.0.1 --port 3310
# then: ssh -L 3340:127.0.0.1:3310 host

# Pull the pi/codex/devin runtime images up front
team-builder init ... --pull-harnesses pi,codex,devin

# Enable Mission Control during setup
team-builder init ... --dashboard
```

Each value flag also reads a matching `TEAM_BUILDER_*` environment variable
(for example `TEAM_BUILDER_STATE_DIR`, `TEAM_BUILDER_MODEL`). Private keys are
never command-line values. Re-running `init` on an existing state directory
*resumes* the installation without replacing identities or images.

State lives under `~/.local/state/team-builder/default` (override with `--state-dir`):
`config.json`, `compose.yaml`, per-agent `agents/<id>/{managed,home,work}`,
`credentials/`, and `upgrades/` backups.

---

## 4. Talking to COA

Open Office Of COA and speak plainly. COA knows the operation schema; you don't
need to.

**Inspect**

> What does the team look like right now?

**Create a channel and an agent**

> Create a private channel "Research" and an agent "Analyst" in it. The analyst
> answers questions about company fundamentals and writes short memos.

COA posts a proposal. Reply `approve` *to that message* (the Buzz app's
`@Chief of Agents approve` form also works). Anything else — a new message, a
reply to a different message, extra text, an edited proposal — is refused by the
manager.

**Choose a harness**

> Create "Pipeline Engineer" in Research on the codex harness.

> Create "Chart Bot" in Research on the pi harness.

> Create "Modeler" in Research on the devin harness with harness_credential
> devin-account.

Harness rules, enforced by the manager:

| Harness | Provider | MCP connections | Deployment access | Notes |
| --- | --- | --- | --- | --- |
| `hermes` | shared | yes | yes | Default. Richest built-in tools (terminal, file, skills, memory, todo, session search). |
| `codex` | shared (OpenAI, OpenRouter, or a custom URL that serves the Responses API) | yes | yes | OpenAI Codex CLI via `codex-acp`. |
| `pi` | OpenRouter or custom chat-completions URL | **no** | yes, via CLI | Lightweight coding agent via `pi-acp`. pi has no MCP, so deployment access arrives as a bundled CLI plus a managed skill. |
| `devin` | Devin account (`harness_credential`) | yes | yes | Devin CLI in ACP mode; the shared provider key is not used. |

The harness is fixed after creation — to change it, archive the agent and create
a new one.

**Adjust, pause, retire**

> Rename Analyst to "Equity Analyst" and switch it to openai/gpt-5.
> Stop Chart Bot for now.
> Archive Pipeline Engineer.

Archiving stops the container and removes the agent from channels; its workspace
directory stays on disk.

**Membership and invitations**

> Add Analyst to the Trading channel.
> Invite my colleague (pubkey `ab12…`) to Research as a member.
> Make a 3-day, 5-use invite link for Research.

**Projects and repositories**

> Create a project "Q4 Factor Study" homed in Research.
> Link https://github.com/acme/factor-study to Q4 Factor Study.

**Provider changes**

```sh
team-builder credential openai-main --key-file ~/openai.key
```
> Switch the team provider to OpenAI, model gpt-5, using credential openai-main.

**Credentials in general.** Never paste a secret into chat. Store it with
`team-builder credential NAME` (or `github-credential`), then refer to it by name.
COA and the dashboard only ever see the name.

---

## 5. Working with agents

Mention an agent in a channel it belongs to:

> @Equity Analyst compare ASML and AMAT gross margins over the last 8 quarters.

The agent runs one turn (tools, code, files under `/work`) and posts its answer
in the thread. An agent is woken only by a message that notifies it (a `p` tag):
an `@mention`, or a reply to its message from the Buzz app, which addresses the
agent automatically. A plain message without either is not delivered to it.

Each agent keeps **one conversation per channel**, not per thread: context from
earlier threads in that channel carries over, and the gateway also feeds it the
recent channel history. Use a separate channel for work that should not share
context.

Practical notes:

- Each agent has a persistent `/work` directory. Files, virtualenvs, and cloned
  repositories survive restarts and upgrades.
- Long-running servers must be launched detached (`nohup … &`); the agent's
  instructions already tell it how. A gateway-only restart preserves them; a
  container restart or upgrade does not.
- Code lives on GitHub: Team Builder disables Buzz-hosted git, and COA links
  existing GitHub repositories to Buzz projects. Agents with a GitHub credential
  push branches and open pull requests there (section 7).
- If an agent goes quiet, check Mission Control (gateway state `connected`?) or
  `docker logs <project>-agent-<id>` on the host.

---

## 6. Skills, MCP connections and the configuration editor

Enable Mission Control:

```sh
team-builder dashboard enable --bind 0.0.0.0 --port 3101 --key-output ~/mc.key
```

Open `http://HOST:3101/` and paste the one-time key. The dashboard is read-only
except for **Agents → Configure**, where you edit an agent's display name, model,
role instructions, personality (SOUL.md), built-in tools, **managed skills**, and
**MCP connections**. *Save and apply* writes a new revision and restarts only that
agent's gateway.

**Catalog.** Skills and MCP entries are immutable catalog items you add in the
editor:

- A **skill** is a Markdown `SKILL.md` — house rules, checklists, code
  templates. It is installed into each assigned agent's native skills directory
  (Hermes `~/.hermes/skills`, pi `~/.pi/agent/skills`, Codex `~/.codex/skills`,
  Devin `~/.config/devin/skills`).
- An **MCP connection** is an HTTP(S) endpoint plus an explicit allow-list of
  tool names. For bearer auth, store the token with `team-builder credential`
  and enter its *name*; the manager injects the value into the agent's private
  config.

COA can assign catalog IDs to agents through `configure_agent` (with owner
approval) but cannot invent new catalog entries — only you can.

---

## 7. GitHub access

```sh
team-builder github-credential acme-bot --key-file ~/gh-token.txt
team-builder github-access pipeline-engineer --credential acme-bot
```

or ask COA: *"Give Pipeline Engineer the acme-bot GitHub credential."* The token
is mounted into that agent's container and wired into a git credential helper;
the agent is told to use plain `https://github.com/owner/repo.git` URLs and never
print the token. Revoke with `--revoke`.

---

## 8. Operations

```sh
team-builder upgrade                          # new wheel installed → update manager + agents
team-builder upgrade --manager-only           # keep worker containers untouched
team-builder upgrade --harness codex --image ghcr.io/…/codex@sha256:…
team-builder proxy set --url http://proxy.corp:3128   # egress proxy for agents
team-builder proxy status / disable
team-builder dashboard status / rotate-key / disable
docker compose -f ~/.local/state/team-builder/default/compose.yaml logs manager
```

- Upgrades preserve identities, channels, workspaces and infrastructure volumes;
  the previous `config.json`/`compose.yaml` are saved under `STATE/upgrades/`.
- Upgrading from 0.6.x to 0.7.0 needs a full `upgrade`: `--manager-only` is
  refused while agents still run a pre-0.7.0 runtime. All agent containers are
  recreated once, which stops detached app servers.
- After a restart the gateway opens a fresh session and feeds the agent the
  recent channel history, so conversations continue from what is visible in
  Buzz; harness-internal memory is per harness and is not carried over by an
  archive-and-recreate.
- The manager verifies signatures independently, so a compromised or confused
  agent cannot authorise changes. Repeating an approved operation returns the
  stored result rather than executing twice.
- For disaster recovery, back up the whole state directory securely: it holds
  agent identities, credentials (0600) and deployment state. The owner key is
  *not* in it.

---

## 9. Application releases (UAT → production)

Team Builder can run your own applications (for example an internal web app) in
separate containers on the same host, with agents doing the release work. The
recommended setup gives each environment a responsible agent — **Cody** for UAT
(`staging`) and **Oppo** for `production` in the examples — and an operator-enabled
**release policy**, so routine releases need no owner action at all.

| Who | Does what |
| --- | --- |
| Cody (release agent) | Reads the repository and proposes the whole setup in one frozen proposal: application registration per environment, generated credentials, profiles, database attachments, access grants for Cody and Oppo, and CI import settings; later the release policy. Adds the CI workflow, smoke test and acceptance checks to the repository through pull requests. |
| Owner (you, in Buzz) | Replies `approve` to Cody's proposals. Asks COA once to make Cody a release agent. Nothing for routine releases. |
| Operator (you, on the host) | Only supplies secret values: a GitHub token for the importer and the application's supplied credentials (database passwords, login files), each with the exact command Cody gives you. |
| CI (application repository) | On every merge to `main`: tests, builds one image, smoke-tests it, and publishes a verified release artifact. |
| Manager | Imports each successful CI run as release `ci-<run>-<attempt>` (no host timer), deploys the newest one to UAT with the policy's profile, runs the acceptance checks, records the evidence, then deploys the *same images* to production and checks again. |
| Cody / Oppo under the policy | Are notified when a stage blocks; investigate with the masked output of failed checks and masked logs, fix the cause (Cody's code fixes arrive as a new CI release), verify with an on-demand check run, retry at most twice per run. Oppo may roll production back to the previous release when the policy allows. |

Without an enabled policy, the same agents work manually: they plan and publish a
frozen deployment proposal in their channel, and you reply `approve` to it — UAT
and production separately. pi agents get the same deployment operations as a
bundled CLI and skill instead of MCP tools.

Watch **Mission Control → Deployments** for releases, runs, check results and
blocked reasons. Handoff, completion and failure notices go to the policy's
notification channel.

> **0.7.0 only:** its notices did not wake buzz-acp agents (they lacked a `p`
> tag). Upgrade to 0.8.0, or mention the agent yourself, for example
> "@Oppo check release automation for hti-research-admin".

Step-by-step setup and daily use: [application deployment guide](application-deployment-guide.md);
reference for profiles, policies and the CI contract: [deployment service](deployments.md).

---

## 10. Worked example: a financial data analysis team

**Scenario.** A two-person fund wants to systematise its equity research: pull
prices and fundamentals, keep a factor-research codebase healthy, produce weekly
risk snapshots, and get a plain-English memo for the portfolio manager (PM). The
PM owns the team; a quant developer joins as a human member.

### 10.1 Set up the host

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install 'https://github.com/tengso/triflection-team-builder/releases/download/v0.8.2/buzz_team_builder-0.8.2-py3-none-any.whl'
printf '%s\n' 'sk-or-v1-…' > ~/openrouter.key && chmod 600 ~/openrouter.key
# owner.key holds the PM's Nostr secret (hex or nsec)
team-builder init --non-interactive --name "Alpha Desk" \
  --advertised-url http://desk.internal:3100 --bind 0.0.0.0 --port 3100 \
  --model anthropic/claude-sonnet-4.5 \
  --owner-key-file ~/owner.key --provider-key-file ~/openrouter.key \
  --pull-harnesses pi,codex --dashboard
```

Store the secrets the team will need, by name only:

```sh
team-builder credential marketdata-api   --key-file ~/marketdata.key   # data vendor bearer token
team-builder github-credential desk-bot  --key-file ~/gh.key           # GitHub PAT for the research repo
```

### 10.2 Add a house-style skill and a data MCP connection

In Mission Control → any agent → Configure → *Catalog*:

- Skill `research-memo-style-v1`:

  ```markdown
  ---
  name: research-memo-style
  description: House format for research memos
  ---
  Every memo: title, 3-bullet TL;DR, data window and source, method, results
  table, risks/caveats, next steps. Cite the exact query or script path in
  /work used to produce each number. Never present a number you did not compute
  in this turn. Round to 2 decimals; state currency and units.
  ```

- Skill `quant-repo-conventions-v1` (branching, `make test`, no notebooks in
  `main`, how to record a factor definition).

- MCP connection `marketdata-v1`: URL `https://mcp.marketdata.example/v1`,
  credential `marketdata-api`, allowed tools `get_prices`, `get_fundamentals`,
  `get_corporate_actions`.

### 10.3 Ask COA for the team

In **Office Of COA** (no mention needed — you are the owner):

> Set up our research desk. Channels: "Research" (private) for analysis and
> "Data Platform" (private) for engineering. Agents:
>
> 1. **Data Engineer** in Data Platform, on the **codex** harness. Owns the
>    ingestion pipeline in `/work/pipeline` (Python, Parquet under
>    `/work/data`), keeps `make refresh` working daily, and answers data-quality
>    questions. Give it the GitHub credential desk-bot.
> 2. **Quant Analyst** in Research, hermes. Runs factor and event studies with
>    pandas/statsmodels, always reports data window, sample size and
>    significance, and writes memos in the house style.
> 3. **Risk Reviewer** in Research, hermes, model openai/gpt-5. Reviews the
>    Analyst's memos for look-ahead bias, survivorship, overfitting and unit
>    errors; recomputes at least one headline number independently before
>    signing off.
> 4. **Chart Bot** in Research on **pi**. Only makes matplotlib charts from CSV
>    files other agents drop in `/work/shared` and posts the PNG path; no
>    market calls.
>
> Create a project "Factor Research" homed in Research and link
> https://github.com/alphadesk/factor-research to it. Add the quant developer
> (pubkey `9f3a…c21e`) to both channels as a member.

COA replies with a single frozen proposal (about a dozen operations:
`create_channel` ×2, `create_agent` ×4 with harness/model/`github_credential`
fields, `add_human_member` ×2, `create_project`, `link_github_repository`).
Reply **approve** to it. COA executes and reports each result; Mission Control
shows four agents coming up with gateway state `connected`.

Then wire the catalog entries:

> Assign skills research-memo-style-v1 to Quant Analyst and Risk Reviewer,
> quant-repo-conventions-v1 to Data Engineer and Quant Analyst, and the
> marketdata-v1 MCP connection to Data Engineer and Quant Analyst.

(One more proposal — `configure_agent` per agent with the complete settings and
`expected_revision`. Chart Bot is on `pi`, so COA will refuse an MCP assignment
for it and say so.)

### 10.4 A week in the life

**Monday — data.** In *Data Platform*:

> @Data Engineer backfill daily OHLCV and quarterly fundamentals for the S&P 500
> constituents from 2015 through last Friday using the marketdata tools, store
> Parquet in /work/data, and add a `make refresh` target. Push to a branch
> `ingest-backfill` and open a PR.

Data Engineer (Codex) calls `get_prices`/`get_fundamentals`, writes the pipeline,
runs it, pushes with the mounted GitHub credential, and posts the PR link and row
counts in the thread. It cannot see the vendor token — the manager injected it
as an MCP header.

**Tuesday — research.** In *Research*:

> @Quant Analyst using /work/data from Data Engineer (shared via
> /work/shared/prices.parquet — @Data Engineer please copy it there), test a
> 12-1 momentum factor, monthly rebalanced, decile spread, 2016–2025. Memo
> please.

Quant Analyst computes returns, posts a memo in house style with the results
table, and drops `momentum_deciles.csv` into `/work/shared`.

> @Chart Bot plot cumulative returns of deciles 1 and 10 from
> /work/shared/momentum_deciles.csv, log scale.

Chart Bot (Pi) writes the PNG and posts its path; someone attaches it.

**Wednesday — review.**

> @Risk Reviewer review the momentum memo above.

Risk Reviewer independently recomputes the decile-10 CAGR, flags that the
fundamentals join used report dates instead of availability dates (look-ahead),
and asks the Analyst to rerun with a 45-day lag. The Analyst reruns; the spread
shrinks; the memo is updated in-thread.

**Friday — PM memo.** The PM, in Research:

> @Quant Analyst write the weekly research note: momentum result after the
> lag fix, what changed, and what we test next.

The note lands in the thread, following the skill's format, citing script paths.

### 10.5 Growing and changing the team

- Weekly risk snapshot wanted? Ask COA for a **Risk Reporter** agent on
  `hermes` in Research with instructions to run `/work/risk/snapshot.py` when
  mentioned and post VaR/exposure tables. Assign it the memo skill.
- Data Engineer should ship an internal risk dashboard? Follow section 9:
  register the application, add the dashboard repository's CI release workflow
  and the importer, grant Data Engineer staging and production access (or split
  them between two agents), and enable a release policy. Merged fixes then reach
  UAT and production automatically, with the agent investigating any failed
  check.
- Vendor token rotated? `team-builder credential marketdata-api-2 --key-file …`,
  add catalog entry `marketdata-v2` pointing at the new name, reassign, retire
  `marketdata-v1`.
- Tired of Codex for engineering? Archive Data Engineer and create a new one on
  `hermes` or `devin`; `/work` files are in the archived agent's directory on the
  host and can be copied by the operator.

### 10.6 Guardrails that matter in finance

- **Provenance.** The memo skill forces every number to cite the script or query
  that produced it, and the Risk Reviewer recomputes independently. Keep those
  two roles on different models if you can afford it.
- **No secrets in chat.** Vendor and broker keys enter only through
  `team-builder credential`; agents see them as environment/MCP headers inside
  their container, never in the Buzz transcript.
- **No live trading by default.** Nothing in this setup gives an agent a broker
  connection. If you add one as an MCP connection, restrict its allowed tools to
  read-only endpoints and give it to exactly one agent.
- **Change control.** Every team change is an owner-approved, signed proposal
  and is recorded in Mission Control's operations view. Agents cannot promote
  themselves, add channels, or change models.
- **Reproducibility.** Data and code live in `/work` and in the linked GitHub
  repository; container upgrades do not touch them. Back up the state directory.

---

## 11. Troubleshooting

| Symptom | Check |
| --- | --- |
| Agent never answers a mention | Is it a member of that channel? Mission Control → gateway `connected`? `docker logs <project>-agent-<id>` for provider 401/403 or model errors. |
| COA says the request was refused | Only the owner key can authorise changes; approvals must be a direct reply to the untouched proposal. |
| "runtime image is not installed" for pi/codex/devin | `team-builder upgrade --harness NAME --image REF` or `init --pull-harnesses …`. |
| Codex agent fails on a custom provider | Codex uses the Responses API; the base URL must serve `/responses`. |
| Pi agent cannot get MCP tools | By design — pi has no MCP support here; use hermes/codex/devin. |
| Buzz app cannot connect | Advertised URL must be reachable from the client; use `--internal-url http://relay:3000` with a tunnel for split setups. |
| Detached server disappeared | Container restart/upgrade stops all processes; only gateway-only restarts preserve them. |
| Agent ignores a message in a thread | It was not notified: `@mention` it, or reply to its own message from the Buzz app. |
| Automatic release is blocked | Mission Control → Deployments shows the reason and the responsible agent is notified (on 0.7.0, mention it yourself). A missing supplied credential comes with the exact host command for the operator. |
| Manager logs `HTTP 429` from Buzz | The relay's per-identity limit (300 calls/minute); the manager waits for the window and retries twice. Persistent 429s mean unusually heavy management traffic. |

For deeper reference: `README.md` (all flags and behaviours),
`docs/application-deployment-guide.md` (releases step by step), `docs/deployments.md`
(deployment service reference), `packaging/README.md` (images and pins), `docs/validation.md`
(what has been tested, where).
