# Fresh Team Builder install on `research` (keep only the SQL databases)

Goal: remove every existing Team Builder team and hosted app on `research`, keep the
UAT and production databases (`db-stack`: MySQL and PostgreSQL, UAT and prod), and
install Team Builder **0.8.5** from scratch with the HTI Research team and the
`hti-research-admin` app (UAT + production), using DashScope `kimi-k3`.

How to read this: every step says **why** (intent), **run** (commands, with
comments) and **check** (what you should see). Run commands as `ubuntu` on
`research` unless a step says *workstation* or *Buzz*. Do not skip the checks.
Never paste secret values into Buzz or this document.

What exists today (checked 2026-10-06):

| Thing | Name on research | Fate |
| --- | --- | --- |
| Team Builder 0.5.1 installation | project `tb-c1b7e32b-6af`: 11 containers (COA, Cody, Windy, relay, manager, Postgres, Redis, MinIO, …), networks `tb-c1b7e32b-6af_community` and `…-app-hti-research-admin-production`, 4 volumes; state `~/.local/state/team-builder/default` (agents `coa`, `cody`, `windy`, upgrade backups) | **Remove** (3.1–3.2, 3.5) |
| Old Team Builder program | `~/triflection_team_builder` (0.5.1 venv **plus `.env` and `.rest_env` secret files**) | **Remove** after backup (1.5, 3.6) |
| Old hosted app | `tb-c1b7e32b-6af-app-hti-research-admin-production-{ui,api}`, its data volume, and the manually built image `hti-research-admin:main-9104be4…` | **Remove** (3.2, 3.10) |
| Old operator files | `~/production/hti-research-admin/*.json` (request files; `production-env.json` holds app secrets), `~/mission-control.key` (old dashboard key) | **Remove** after backup (1.5, 3.7) |
| Leftovers of an earlier installation | volumes `tb-c443071d-901_{minio,postgres,redis,relay}` | **Remove** (3.3) |
| Test containers | `laughing_cohen`, `clever_feynman` (hello-world) | **Remove** (3.4) |
| Old Team Builder images and build cache | `ghcr.io/tengso/triflection-team-builder/*`, old Redis/MinIO/Postgres images, buildx cache | **Remove** (3.10) |
| Host checkout of the app repo | `~/projects/hti-research-admin` (clean `main`, no secrets) | Optional remove (3.8) |
| Recovery container | `db-uat-migration-recovery` (+ volume of the same name) | Optional remove (3.9) |
| Old Buzz community in your Buzz app | `http://127.0.0.1:3100` (old relay) | **Remove from the app** (3.12) |
| Production MySQL | `db-prod-mysql-1` (127.0.0.1:3306), volume `db-prod-mysql-data` | **Keep** (detach from old app network) |
| UAT MySQL | `db-uat-mysql-1` (127.0.0.1:13306, currently stopped), volume `db-uat-mysql-data` | **Keep** |
| Production / UAT PostgreSQL | `db-prod-postgres-1` (5432), `db-uat-postgres-1` (15432), both stopped | **Keep** |
| `~/projects/db-stack` | compose files, secrets, `dc` wrapper | **Keep** (one small edit, step 2.2) |

Expected downtime: the old production app stops in phase 3 and comes back in
phase 9. (Its API container has been stopped since 21 Sept anyway.)

---

## Phase 0 — Prepare (before touching research)

### 0.1 Collect what you will need
**Why:** the install asks for these; having them ready avoids pausing mid-way.

- Your **owner Nostr private key** (the identity that owns the team in Buzz). You
  will type it at a hidden prompt; it is never stored on the host.
- The **DashScope** URL and key: `~/secrets/.env` on sandbox2 (`DASHSCOPE_API_BASE`,
  `DASHSCOPE_API_KEY`).
- A **GitHub token** for Cody (push branches / open PRs on
  `tengso/hti-research-admin`) and one with **Actions: read** for release imports
  (one token can do both).
- The application secrets you stored on sandbox2: `auth-db-password`,
  `crm-report-db-password`, `gateway-token-secret`, the login file `users.yaml`
  (ideally a separate production one).
- Decide **where the auth database (`latent_space`) lives** — see step 1.4.

### 0.2 Check the host is healthy enough
**Why:** research is an LXC container on a shared host whose disk was saturated
(load ≈ 20, 60 % I/O wait). Dumps and image pulls will be very slow if that persists.

```bash
ssh research
uptime                 # load average should be well below 10 for a smooth run
vmstat 1 5             # the "wa" column (I/O wait) should mostly be < 20
df -h /                # need > 15 GB free
```
**Check:** if load/I/O wait is still high, find the noisy neighbour on the
underlying host first.

---

## Phase 1 — Back up everything (no changes yet)

All backups go to `/var/backups/hti-<date>/` (root-only). Copy that folder off the
machine at the end of this phase.

### 1.1 Create the backup folder
**Why:** one place for every restore point; root-only because it will contain secrets and data.
```bash
export B=/var/backups/hti-$(date +%F)          # reuse this variable in this shell
sudo install -d -m 0700 "$B"
```

### 1.2 Back up the old Team Builder state
**Why:** a full rollback point for the old team (identities, credentials, app
records), even though we don't plan to restore it.
```bash
sudo tar -C ~/.local/state/team-builder -czf "$B/team-builder-0.5.1-state.tgz" default
sudo ls -lh "$B"
```
**Check:** the `.tgz` exists and is larger than a few KB.

### 1.3 Back up both MySQL instances (all HTI schemas)
**Why:** `db-stack/backup.sh` only covers `appdb`; the HTI data lives in
`eq_crm_raw_data`, `eq_crm_data`, `equity_research`. UAT MySQL is stopped, so start it first
(it has to run for the new UAT anyway).
```bash
cd ~/projects/db-stack
sudo ./dc uat up -d --wait --wait-timeout 600 mysql          # start UAT MySQL (prod is already running)

for env in prod uat; do
  # Consistent, read-only dump of the three HTI schemas plus appdb, with views/routines/triggers/events.
  sudo bash -c "cd /home/ubuntu/projects/db-stack && ./dc $env exec -T mysql \
    mysqldump --defaults-extra-file=/run/secrets/mysql_admin_cnf \
      --single-transaction --quick --routines --triggers --events --hex-blob \
      --no-tablespaces --set-gtid-purged=OFF \
      --databases eq_crm_raw_data eq_crm_data equity_research appdb \
    </dev/null | gzip > $B/$env-mysql.sql.gz"
  sudo bash -c "zcat $B/$env-mysql.sql.gz | tail -1"          # must print '-- Dump completed on …'
done
```
**Check:** both dumps end with `-- Dump completed`. Production's is ≈ 170 MB compressed.

### 1.4 Back up both PostgreSQL instances, and find `latent_space`
**Why:** they are stopped; we back them up and learn whether the auth database
(`latent_space`, used for user roles) is here. `db-uat-postgres-1` last exited with an
error, so it may need attention.
```bash
cd ~/projects/db-stack
for env in prod uat; do
  sudo ./dc $env up -d --wait --wait-timeout 600 postgres || { echo "$env postgres failed to start:"; sudo ./dc $env logs --tail 30 postgres; continue; }
  # List databases (is latent_space here?).
  sudo ./dc $env exec -T postgres sh -c 'PGPASSWORD=$(cat /run/secrets/pg_admin) psql -h 127.0.0.1 -U postgres -Atc "select datname from pg_database where not datistemplate"' </dev/null
  # Full logical backup (roles + all databases).
  sudo bash -c "cd /home/ubuntu/projects/db-stack && ./dc $env exec -T postgres sh -c 'PGPASSWORD=\$(cat /run/secrets/pg_admin) pg_dumpall -h 127.0.0.1 -U postgres' </dev/null | gzip > $B/$env-postgres.sql.gz"
done
sudo ls -lh "$B"
```
**Check / decide:**
- If `latent_space` is listed under **prod** (and/or uat), the apps will use that
  container as `auth-db` (step 7.1). Leave those Postgres instances running.
- If it is **not** listed anywhere, `latent_space` lives elsewhere (e.g. the portal's
  server). Write down its host/port; the apps must be able to reach it.
- If a Postgres instance didn't start, keep its log output; it isn't needed for the
  install unless it holds `latent_space`.

### 1.5 Back up the db-stack folder and the old installation's secret files
**Why:** the database passwords live in `db-stack/env/*/secrets`; losing them means
losing admin access. The old installation also left files with secrets you may still
need (app environment values, REST/gateway credentials) — keep a private copy before
phase 3 deletes them.
```bash
sudo tar -C ~/projects -czf "$B/db-stack-folder.tgz" db-stack
sudo tar -C ~ -czf "$B/old-install-files.tgz" \
  production \
  mission-control.key \
  triflection_team_builder/.env triflection_team_builder/.rest_env
sudo tar -tzf "$B/old-install-files.tgz"      # lists the 7 files (4 request files, key, 2 env files)
```
**Check:** the listing shows `production/hti-research-admin/production-env.json`,
`triflection_team_builder/.env` and `.rest_env`. Look in these when Cody asks for an
app secret you no longer have elsewhere.

### 1.6 Copy the backups off research
**Why:** a backup on the same disk doesn't protect against losing that disk.
*Workstation:*
```bash
mkdir -p ~/hti-backups && chmod 700 ~/hti-backups
ssh research "sudo tar -C /var/backups -cf - hti-$(date +%F)" > ~/hti-backups/research-hti-$(date +%F).tar
ls -lh ~/hti-backups/
```
**Check:** the tar file is roughly the size of the dumps. Keep it encrypted / private.

---

## Phase 2 — Detach the databases from the old installation

### 2.1 Disconnect production MySQL from the old app network
**Why:** `db-prod-mysql-1` is attached to `tb-c1b7e32b-6af-app-hti-research-admin-production`.
Docker refuses to delete a network that still has a container attached, and the new
Team Builder will attach the database itself (as `prod-mysql`).
```bash
docker network disconnect tb-c1b7e32b-6af-app-hti-research-admin-production db-prod-mysql-1
docker inspect db-prod-mysql-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```
**Check:** only `db-prod-mysql-net` is listed. The database keeps running (this only
removes one network link).

### 2.2 Remove that network from db-stack's production override
**Why:** `compose.prod.yaml` declares the old app network as `external`; once the
network is deleted, any later `./dc prod up` would fail. The new install attaches the
database through Team Builder instead.
```bash
cd ~/projects/db-stack
sudo cp -a compose.prod.yaml "compose.prod.yaml.before-fresh-install"     # keep the original
printf 'services:\n  mysql: {}\n' | sudo tee compose.prod.yaml >/dev/null    # empty override
sudo ./dc prod config --quiet && echo "prod compose config OK"
```
**Check:** `prod compose config OK`. Nothing is restarted by this step.

---

## Phase 3 — Remove the old installation completely

Order matters: services first, then the containers and networks they created, then
files, then images. Every step only touches things of the old installation; step 3.11
proves the databases survived and nothing old is left.

### 3.1 Stop and remove the old Team Builder services
**Why:** removes relay, manager, Postgres/Redis/MinIO of the old team **and their
volumes** (old chat history, agent registry). We start fresh.
```bash
sudo docker compose -f ~/.local/state/team-builder/default/compose.yaml down -v --remove-orphans
```
**Check:** it lists `tb-c1b7e32b-6af-relay-1`, `-manager-1`, `-postgres-1`, `-redis-1`,
`-minio-1`, `-minio-init-1` as removed.

### 3.2 Remove the old agents, the old app containers and their networks/volumes
**Why:** agent and app containers were created by the old manager, not by compose, so
3.1 doesn't remove them. Everything of that installation carries the label
`io.team-builder.project=tb-c1b7e32b-6af`; we remove exactly those, then the two named
networks and the app's data volume.
```bash
P=tb-c1b7e32b-6af
docker ps -a --filter "label=io.team-builder.project=$P" --format '{{.Names}}'     # review: coa, cody, windy, app ui/api
docker ps -aq --filter "label=io.team-builder.project=$P" | xargs -r docker rm -f
docker network rm "$P-app-hti-research-admin-production" "${P}_community" 2>/dev/null || true
docker network ls -q --filter "label=io.team-builder.project=$P" | xargs -r docker network rm
docker volume rm "$P-app-hti-research-admin-production-ui-data" 2>/dev/null || true
docker volume ls -q --filter "label=io.team-builder.project=$P" | xargs -r docker volume rm
```
**Check:** `docker ps -a | grep tb-` and `docker network ls | grep tb-` print nothing.
If a network refuses to go ("active endpoints"), run
`docker network inspect <name> --format '{{range .Containers}}{{.Name}} {{end}}'` — it
must not be a `db-*` container; if it is, disconnect it (as in 2.1) and retry.

### 3.3 Remove the leftovers of the even older installation
**Why:** four orphaned volumes from an earlier, already-removed install (`tb-c443071d-901`).
```bash
docker volume rm tb-c443071d-901_minio tb-c443071d-901_postgres tb-c443071d-901_redis tb-c443071d-901_relay
```

### 3.4 Remove the test containers
```bash
docker rm laughing_cohen clever_feynman
```

### 3.5 Remove the old Team Builder state directory
**Why:** it holds the old team's identities, credentials, agent workspaces (`coa`,
`cody`, `windy`) and upgrade backups. Some files are root-owned, hence `sudo`. Its
content is in the phase-1 backup.
```bash
sudo rm -rf ~/.local/state/team-builder/default
ls -la ~/.local/state/team-builder/        # should be empty
```

### 3.6 Remove the old Team Builder program (and its secret files)
**Why:** `~/triflection_team_builder` is the 0.5.1 program plus `.env` and `.rest_env`
with secrets (backed up in 1.5). Overwrite the secret files before deleting.
```bash
shred -u ~/triflection_team_builder/.env ~/triflection_team_builder/.rest_env
rm -rf ~/triflection_team_builder
```

### 3.7 Remove the old operator files and dashboard key
**Why:** the request files from the manual setup (`production-env.json` holds app
secrets) and the old Mission Control key are of no use to the new install (backed up
in 1.5).
```bash
sudo find ~/production -type f -exec shred -u {} +
sudo rm -rf ~/production
shred -u ~/mission-control.key
```

### 3.8 (Optional) Remove the host checkout of the app repository
**Why:** `~/projects/hti-research-admin` was used to build the old app by hand. The new
setup builds releases in GitHub Actions and Cody works in its own workspace, so the
host copy isn't needed. It is clean (`main`, no `.env`); skip this if you use it yourself.
```bash
git -C ~/projects/hti-research-admin status -s      # must print nothing (no local changes)
rm -rf ~/projects/hti-research-admin
```

### 3.9 (Optional) Remove the migration-recovery container
**Why:** `db-uat-migration-recovery` was a one-off recovery copy from 21 Sept. Remove it
only if you no longer need it (its data is not part of UAT or production).
```bash
docker rm db-uat-migration-recovery
docker volume rm db-uat-migration-recovery
```

### 3.10 Remove old images and the build cache
**Why:** frees several GB: old Team Builder images, the hand-built
`hti-research-admin:main-9104be4…`, old Redis/MinIO/Postgres images, hello-world, and
the buildx cache. Images used by an existing container (the databases: `mysql:8.4`,
`postgres:18-bookworm`) are kept automatically.
```bash
docker image prune -a          # answer y; review the list it prints
docker builder prune -a -f     # build cache
docker images                  # expect only mysql:8.4 and postgres:18-bookworm (+ the recovery image if kept)
df -h /
```

### 3.11 Verify: databases intact, nothing old left
**Why:** prove that only the old installation was removed. Run all of these; every line
says what you should see.
```bash
docker ps -a --format '{{.Names}}\t{{.Status}}'
#   only db-prod-mysql-1, db-uat-mysql-1, db-prod-postgres-1, db-uat-postgres-1 (+ db-uat-migration-recovery if kept)
docker network ls --format '{{.Name}}' | grep -vE '^(bridge|host|none)$'
#   only db-prod-mysql-net, db-prod-pg-net, db-uat-mysql-net, db-uat-pg-net
docker volume ls --format '{{.Name}}'
#   only db-prod-mysql-data, db-prod-pg-data, db-uat-mysql-data, db-uat-pg-data (+ db-uat-migration-recovery if kept)
ls -a ~
#   no triflection_team_builder, production, mission-control.key
ls -a ~/.local/state/team-builder/ 2>/dev/null
#   empty (or missing)
ss -ltn | grep -E ':(3100|3101|38502)\b' || echo "old Team Builder ports are free"
#   old Team Builder ports are free
systemctl list-unit-files | grep -iE 'team-builder|release-sync' ; sudo crontab -l 2>/dev/null | grep -iE 'team|release' ; crontab -l 2>/dev/null | grep -iE 'team|release' ; echo "(no services, timers or cron jobs above)"

cd ~/projects/db-stack
for env in prod uat; do
  echo "== $env"
  sudo ./dc $env exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf -Nse \
    "select table_schema, count(*) from information_schema.tables where table_schema in ('eq_crm_raw_data','eq_crm_data','equity_research') group by 1" </dev/null
done
#   prod: eq_crm_data 33, eq_crm_raw_data 6, equity_research 4 (tables + views); uat similar
```
**Check:** every comment matches. If something old is still listed, remove it with the
matching step above before continuing.

### 3.12 Clean up on your workstation
**Why:** the new community will use the same address (`http://127.0.0.1:3100`) but a
new relay and identity; old entries cause confusing connection errors and old keys
should not linger.
- **Buzz app:** remove the old community at `http://127.0.0.1:3100` (and any cached
  channels from it) before adding the new one in phase 5.
- **Password manager:** archive the old Mission Control key and any notes for the old
  team; the new install prints a new key.
- **SSH tunnels:** close any running tunnel to research (`pkill -f 'ssh -N .*research'`
  or close its terminal); you'll open a new one in 5.1.
- **Shell history on research (optional):** if secrets were ever typed on a command
  line there, review `~/.bash_history` and remove those lines.

## Phase 4 — Install Team Builder 0.8.5

### 4.1 Create a fresh program environment
**Why:** an isolated Python environment for the Team Builder command.
```bash
python3 -m venv ~/team-builder || { sudo apt-get install -y python3.12-venv && python3 -m venv ~/team-builder; }
~/team-builder/bin/pip install -q 'https://github.com/tengso/triflection-team-builder/releases/download/v0.8.10/buzz_team_builder-0.8.10-py3-none-any.whl'
~/team-builder/bin/team-builder --help | head -3
echo '. ~/team-builder/bin/activate' >> ~/.bashrc && . ~/team-builder/bin/activate
```
**Check:** `team-builder --help` prints the command list.

### 4.2 Put the DashScope settings on research
**Why:** the team's model provider. The key goes into a private file, never into chat
or shell history. *Workstation:*
```bash
ssh sandbox2 'cat ~/secrets/.env' | ssh research 'umask 077; mkdir -p ~/secrets; cat > ~/secrets/.env; ls -l ~/secrets/.env'
```
Then on research, extract the key into its own file for the installer:
```bash
umask 077
grep '^DASHSCOPE_API_KEY=' ~/secrets/.env | cut -d= -f2- | tr -d '"' > ~/secrets/dashscope.key
export DASHSCOPE_BASE=$(grep '^DASHSCOPE_API_BASE=' ~/secrets/.env | cut -d= -f2- | tr -d '"' | sed 's#/*$##')
echo "$DASHSCOPE_BASE"        # a URL ending in /compatible-mode/v1 (not secret)
```

### 4.3 Install the team
**Why:** creates the Buzz community, the manager, COA and the Office of COA channel.
Everything listens on 127.0.0.1 only; you reach it through an SSH tunnel. Mission
Control is enabled on port 3101.
```bash
team-builder init \
  --name "HTI Research" \
  --advertised-url http://127.0.0.1:3100 \
  --internal-url http://relay:3000 \
  --bind 127.0.0.1 --port 3100 \
  --provider custom --base-url "$DASHSCOPE_BASE" --model kimi-k3 \
  --provider-key-file ~/secrets/dashscope.key \
  --dashboard
# Prompts: "Owner private key (used once, never saved)" -> paste your Nostr key (hidden).
```
**Check:** it ends with `Ready: HTI Research at http://127.0.0.1:3100`, then prints a
**dashboard access key (shown once)** and `Mission Control: enabled`. Copy the
dashboard key straight into your password manager — **don't paste it into chat**.

```bash
shred -u ~/secrets/dashscope.key                         # the installer stored its own copy
docker ps --format '{{.Names}}\t{{.Status}}' | grep tb-  # relay, manager, coa, postgres, redis, minio: healthy
```

---

## Phase 5 — Connect from your workstation

### 5.1 Open the tunnel
**Why:** the community (3100), Mission Control (3101), and later UAT (18502) and
production (38502) are only reachable on research's loopback. *Workstation*, keep it open:
```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 3100:127.0.0.1:3100 -L 3101:127.0.0.1:3101 \
  -L 18502:127.0.0.1:18502 -L 38502:127.0.0.1:38502 \
  research
```
If your sandbox2 tunnel already uses local port 18502, close it first or map UAT to
another local port (e.g. `-L 28502:127.0.0.1:18502`). Keep local port **3100**
exactly: it must match the advertised URL.

### 5.2 Join the community
- **Buzz app:** add community `http://127.0.0.1:3100` using your **owner identity**,
  then open **Office Of COA**.
- **Mission Control:** open `http://127.0.0.1:3101/dashboard/` and paste the dashboard key.

**Check:** in Office of COA, send `What does the team look like right now?` — COA
answers (this also proves DashScope works).

---

## Phase 6 — Build the team (in Buzz, Office of COA)

### 6.1 Create channels and agents
**Why:** Cody develops and runs UAT; Oppo runs production; a shared channel carries
release notices. Send to COA, then reply **approve** to its proposal:
> Create two private channels: "Platform Engineering" and "Production Operations".
> Create agent "Cody" (hermes) in Platform Engineering and Production Operations: a
> software engineer for the hti-research-admin repository, responsible for UAT.
> Create agent "Oppo" (hermes) in Production Operations: the production operations
> agent for hti-research-admin. Add me to both channels.

**Check:** Mission Control → Agents shows Cody and Oppo `connected`.

### 6.2 Make Cody and Oppo release agents
**Why:** gives them the deployment tools and the release runbook. Send, then approve:
> Make Cody a release agent and make Oppo a release agent.

### 6.3 Give Cody GitHub access, and store the release-import token
**Why:** Cody pushes branches / opens PRs; the manager downloads CI releases.
*Research:*
```bash
team-builder github-credential github --key-file /path/to/github-token.txt   # or omit --key-file to paste at a hidden prompt
shred -u /path/to/github-token.txt
```
*Buzz (Office of COA)*, then approve:
> Give Cody the GitHub credential "github".

---

## Phase 7 — Set the application up (Cody does it, you approve)

### 7.1 Make sure the databases are ready for the app
**Why:** the app connects as `app_runtime`, which needs row access to the three schemas
in **both** environments; production already has it, UAT may not.
```bash
cd ~/projects/db-stack
for env in prod uat; do
  echo "== $env"
  sudo ./dc $env exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf -Nse "SHOW GRANTS FOR 'app_runtime'@'%'" </dev/null
done
```
If UAT lacks the three schemas, grant them (row access only, same as production):
```bash
sudo ./dc uat exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf -e "
GRANT SELECT, INSERT, UPDATE, DELETE ON eq_crm_raw_data.* TO 'app_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON eq_crm_data.* TO 'app_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON equity_research.* TO 'app_runtime'@'%';" </dev/null
```
Also check the report database is reachable from research (needed by report pages):
```bash
timeout 5 bash -c '</dev/tcp/1.12.247.103/3306' && echo reachable || echo "NOT reachable (ask network/DB admins to allow research)"
```

### 7.2 (Recommended) Refresh UAT data from production
**Why:** UAT's data dates from before 21 Sept. A fresh copy makes UAT tests realistic.
This **replaces** UAT's three schemas (UAT's own backup is in phase 1).
```bash
export B=/var/backups/hti-YYYY-MM-DD      # the folder created in phase 1 (set it again in a new shell)
cd ~/projects/db-stack
# 1. Dump only the three HTI schemas from production to a file (appdb is left alone).
sudo bash -c "cd /home/ubuntu/projects/db-stack && ./dc prod exec -T mysql \
  mysqldump --defaults-extra-file=/run/secrets/mysql_admin_cnf \
    --single-transaction --quick --routines --triggers --events --hex-blob \
    --no-tablespaces --set-gtid-purged=OFF \
    --databases eq_crm_raw_data eq_crm_data equity_research \
  </dev/null | gzip > $B/prod-hti-for-uat.sql.gz"
# 2. Only continue if the dump is complete.
sudo bash -c "zcat $B/prod-hti-for-uat.sql.gz | tail -1" | grep -q 'Dump completed' && echo "dump OK" || echo "STOP: dump incomplete"
# 3. Replace UAT's three schemas with the production copy.
sudo ./dc uat exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf -e \
  "DROP DATABASE IF EXISTS eq_crm_raw_data; DROP DATABASE IF EXISTS eq_crm_data; DROP DATABASE IF EXISTS equity_research;" </dev/null
sudo bash -c "cd /home/ubuntu/projects/db-stack && zcat $B/prod-hti-for-uat.sql.gz | ./dc uat exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf"
```
Run step 3 only after step 2 printed `dump OK`.
**Check:** compare row counts — run on both `prod` and `uat` and compare:
```bash
sudo ./dc uat exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf -Nse \
  "select table_schema, sum(table_rows) from information_schema.tables where table_schema in ('eq_crm_raw_data','eq_crm_data','equity_research') group by 1" </dev/null
```
(`table_rows` is an estimate; for an exact check use `CHECKSUM TABLE …` on both.)

### 7.3 Ask Cody to propose the whole setup
**Why:** Cody registers the app for both environments, connects the databases and
configures CI imports in **one frozen proposal**; nothing happens until you approve.
In **Platform Engineering** (fill in the auth-db line from step 1.4):
> @Cody Set up `hti-research-admin` from `tengso/hti-research-admin` as a Team Builder
> application for UAT (staging) and production, using your release tools — not docker
> compose. Facts:
> - UAT MySQL: container `db-uat-mysql-1`, alias `uat-mysql`, port 3306. Production
>   MySQL: container `db-prod-mysql-1`, alias `prod-mysql`, port 3306. User `app_runtime`.
> - Auth database `latent_space`: container `<db-prod-postgres-1 or …>`, alias `auth-db`,
>   port 5432 (inside Docker). Report DB: `1.12.247.103:3306` (only add its connection
>   check if research can reach it).
> - UI host ports: UAT 18502, production 38502. The API needs no host port.
> - You operate staging; Oppo operates production. CI import: credential `github`,
>   workflow `release.yml`, branch `main`, both environments.
> Generate the API tokens yourself, propose everything for my approval, then list the
> secrets I must store on the host with the exact commands.

**Check before replying approve:** both environments; the containers/aliases above;
`MYSQL_USER=app_runtime`; grants for Cody (staging) and Oppo (production); no secret
values in the proposal. Then reply **approve** to it.

### 7.4 Store the secrets Cody lists
**Why:** passwords never go through chat; you store them on the host by name.
```bash
umask 077; T=$(mktemp -d); cd "$T"
cred() {  # usage: cred <environment> <id> <file-with-secret>
  printf '{"action":"credential","application":"hti-research-admin","environment":"%s","id":"%s"}\n' "$1" "$2" > req.json
  team-builder deployment req.json --secret-file "$3"
}
# MySQL runtime passwords come from db-stack (no typing needed):
sudo cat ~/projects/db-stack/env/uat/secrets/mysql_runtime  > uat-mysql;  cred staging    mysql-password uat-mysql
sudo cat ~/projects/db-stack/env/prod/secrets/mysql_runtime > prod-mysql; cred production mysql-password prod-mysql
# The others: put each value in a file first (read -rs keeps it off screen and out of history):
read -rs -p "auth-db password: " v; printf '%s' "$v" > auth; unset v; echo
cred staging auth-db-password auth; cred production auth-db-password auth
read -rs -p "report DB (dyxht) password: " v; printf '%s' "$v" > report; unset v; echo
cred staging crm-report-db-password report; cred production crm-report-db-password report
read -rs -p "gateway TOKEN_SECRET: " v; printf '%s' "$v" > gw; unset v; echo
cred staging gateway-token-secret gw; cred production gateway-token-secret gw
cred staging    users-yaml /path/to/uat-users.yaml       # bcrypt login file for UAT
cred production users-yaml /path/to/prod-users.yaml      # ideally a separate production file
cd ~ && shred -u "$T"/* 2>/dev/null; rm -rf "$T"
```
Use the IDs exactly as Cody listed them (the above are the IDs from sandbox2).
**Check:** each command prints `{"id": …, "changed": true}`. Then tell Cody:
> @Cody The secrets are stored. Run preflight for staging and production and tell me what is still missing.

**Check:** preflight is green for both, or Cody names exactly what is missing.

---

## Phase 8 — First UAT deployment

### 8.1 Let the first release arrive
**Why:** the manager imports CI releases every two minutes; the newest successful
`release.yml` run on `main` (currently `ci-37255024251-1`) appears within minutes of
setup. While importing, Mission Control shows the CI importer as `importing`.
**Check:** Mission Control → Deployments → hti-research-admin lists the release in both environments.

### 8.2 Deploy to UAT
In Platform Engineering:
> @Cody Deploy the latest release to staging and report when it's healthy.

Reply **approve** to the deployment proposal (check: `staging`, the expected release
number). **Check:** Cody reports `succeeded`.

### 8.3 Test UAT
Open `http://127.0.0.1:18502` (via the tunnel) and sign in with your UAT account. Check
login, the main pages, the reports, and a few numbers you know.

---

## Phase 9 — First production deployment

### 9.1 Deploy the same release
In Production Operations:
> @Oppo UAT passed for release `<release id>`. Deploy that same release to production and report when it's healthy.

Approve the proposal (check: `production`, the **same** release number). **Check:** `succeeded`.

### 9.2 Check production
Open `http://127.0.0.1:38502`; sign in, check the API-backed pages and reports.
```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep app-hti     # ui and api: both "healthy"
```

---

## Phase 10 — Hardening (same day)

### 10.1 Nightly backups
**Why:** there were no scheduled backups. This dumps the HTI schemas from both MySQL
instances and archives Team Builder's state every night, keeping 14 days.
```bash
sudo tee /usr/local/sbin/hti-backup >/dev/null <<'EOF'
#!/usr/bin/env bash
# Nightly HTI backups: MySQL HTI schemas (prod + uat) and Team Builder state. Root only.
set -Eeuo pipefail
umask 077
out=/var/backups/hti-nightly/$(date +%F); mkdir -p "$out"
cd /home/ubuntu/projects/db-stack
for env in prod uat; do
  ./dc "$env" exec -T mysql mysqldump --defaults-extra-file=/run/secrets/mysql_admin_cnf \
    --single-transaction --quick --routines --triggers --events --hex-blob --no-tablespaces \
    --set-gtid-purged=OFF --databases eq_crm_raw_data eq_crm_data equity_research appdb </dev/null \
    | gzip > "$out/$env-mysql.sql.gz"
  zcat "$out/$env-mysql.sql.gz" | tail -1 | grep -q 'Dump completed'
done
tar -C /home/ubuntu/.local/state/team-builder -czf "$out/team-builder-state.tgz" default
find /var/backups/hti-nightly -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
EOF
sudo chmod 0700 /usr/local/sbin/hti-backup
sudo /usr/local/sbin/hti-backup && sudo ls -lh /var/backups/hti-nightly/$(date +%F)     # test run now
( sudo crontab -l 2>/dev/null; echo '15 2 * * * /usr/local/sbin/hti-backup >> /var/log/hti-backup.log 2>&1' ) | sudo crontab -
```
Also copy `/var/backups/hti-nightly` off the machine regularly (e.g. a nightly `rsync`
pulled from another host).

### 10.2 Rotate secrets that were exposed
**Why:** these appeared in chat or in a repository file.
- Production MySQL `app_owner` password (pasted in chat): change it in db-stack
  (`ALTER USER 'app_owner'@'%' IDENTIFIED BY …` as admin, and update
  `env/prod/secrets/mysql_owner`).
- Report database `dyxht` and dev Postgres passwords written in
  `hti-data-portal-risk-devin/AGENTS.md`: ask the DB owners to rotate, and remove them from that file.
- The sandbox2 dashboard key: `team-builder dashboard rotate-key` on sandbox2.

### 10.3 Turn on automatic releases (after a few good manual releases)
In Platform Engineering:
> @Cody Propose an enabled release policy for hti-research-admin: run your acceptance
> checks in staging and production, notify Production Operations, and let Oppo roll
> back if production checks fail.

Read the proposal carefully and approve. From then on, merges flow to UAT and
production automatically; you're asked only for missing secrets or real decisions.

### 10.4 Tidy up
**Why:** remove temporary copies once you are confident (after a week without problems).
```bash
shred -u ~/secrets/.env                              # only if you no longer need the DashScope file on research
sudo rm -rf /var/backups/hti-YYYY-MM-DD              # the phase-1 backup folder, once its off-host copy is safe
cd ~/projects/db-stack && sudo rm compose.prod.yaml.before-fresh-install   # the original override saved in 2.2
```

---

## Rollback

| Problem | What to do |
| --- | --- |
| Install (phase 4) fails | Fix and rerun `team-builder init` with the same flags — it resumes. Databases are untouched. |
| A deployment fails | The manager restores the previous release automatically; with no previous release (first deploy), fix the cause and approve a new plan. |
| Production data damaged | Restore from `$B/prod-mysql.sql.gz` (phase 1) or the nightly backup: `zcat … | sudo ./dc prod exec -T mysql mysql --defaults-extra-file=/run/secrets/mysql_admin_cnf`. |
| db-stack prod won't start | Restore `compose.prod.yaml.before-fresh-install` only if you also recreate the old network; otherwise keep the empty override from step 2.2. |
| Need the old team back | `team-builder-0.5.1-state.tgz` holds its state; its Buzz history was removed with `down -v` in 3.1 and cannot be restored. |
