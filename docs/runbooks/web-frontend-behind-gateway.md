# Fresh team with the Buzz web app behind a login gateway

Goal: install a Team Builder team from scratch on a Linux host inside the firewall
and use the [buzz-app](https://github.com/tengso/buzz-app) **browser build** as its
front end, reached from outside through the corporate login gateway at
`https://et2.htisec.com/HaiChat/<app>/`. No desktop app, VPN or SSH tunnel is needed
by users; each user still signs in to Buzz with their own Nostr key.

The worked values below are from the **HTI AI Lab** team on `hti-ailab`
(DashScope as model provider), modelled on the **HTI Research** team on
`hti-research`. Replace them for another host:

| Value | HTI AI Lab | HTI Research (reference) |
| --- | --- | --- |
| Host (LXD container) | `hti-ailab`, `10.174.198.22` | `hti-research`, `10.174.198.15` |
| Cloud VM (gateway side, LXD host) | `10.174.198.1` | same |
| Gateway path | `/HaiChat/hti_ailab_team/` | `/HaiChat/hti_research_team/` |
| Relay `--bind:--port` | `127.0.0.1:3100` | same |
| Advertised URL | `http://127.0.0.1:3100` | same |
| Mission Control | `127.0.0.1:3101` | same |
| Container nginx listener | `10.174.198.22:3190` | `10.174.198.15:3190` |

How to read this: every step says **why**, **run** and **check**. Run commands as
`ubuntu` on the host unless a step says *cloud VM*, *gateway* or *browser*. Never
paste secret values into Buzz or this document.

---

## How the pieces fit

```text
browser  https://et2.htisec.com/HaiChat/hti_ailab_team/
   │  HaiChat gateway: login cookie, strips /HaiChat
   ▼
cloud VM nginx 10.174.198.1     location /hti_ailab_team/ → http://10.174.198.22:3190/
   ▼
host nginx 10.174.198.22:3190   (LXD bridge address only)
   ├─ static files              /srv/buzz-web-gateway   (buzz-app browser build)
   ├─ /dashboard/            →  127.0.0.1:3101          (Mission Control)
   └─ everything else,
      WebSocket, NIP-11      →  relay 127.0.0.1:3100, with Host: 127.0.0.1:3100
```

The one rule that makes this work: the browser signs every NIP-42, NIP-98 and
Blossom request for the relay's **advertised URL**, and the relay accepts a
signature only for that URL. Three settings must therefore name the same
`host:port`:

1. `team-builder init --advertised-url http://127.0.0.1:3100`
2. the buzz-app build's `BUZZ_PROXIED_RELAY_URL=ws://127.0.0.1:3100`
3. the host nginx `proxy_set_header Host 127.0.0.1:3100;`

The advertised address of an existing community cannot be changed, so a team
created for direct access (for example `--advertised-url http://hti-ailab:3100
--bind 0.0.0.0`) has to be removed and created again (phase 1).

---

## Phase 0 — Prepare

### 0.1 Collect what you need
- The **owner Nostr private key** (nsec or hex). Whoever holds it owns the team. It is
  typed at a hidden prompt or read from a file you delete afterwards; Team Builder
  never stores it.
- A **model provider key**. This runbook uses Alibaba Cloud DashScope through its
  OpenAI-compatible endpoint (any OpenRouter/OpenAI/custom key works the same way).
- Someone who can edit the **cloud VM nginx** (phase 5) and register an app with the
  **HaiChat gateway** (phase 6).

### 0.2 Check the host
**Why:** the installer needs Linux x86-64, Python 3.12+ with venv, Docker Engine with
Compose v2, and Docker access for `ubuntu`.
```bash
ssh hti-ailab
uname -m; . /etc/os-release; echo "$PRETTY_NAME"
python3 --version; python3 -c 'import ensurepip' && echo venv-ok
docker --version; docker compose version
docker ps >/dev/null && echo docker-ok
```
**Fix what fails** (Ubuntu 24.04):
```bash
sudo apt-get update && sudo apt-get install -y python3.12-venv   # "No module named 'ensurepip'"
sudo usermod -aG docker ubuntu                                    # "permission denied ... docker.sock"
exit                                                              # log in again for the group to apply
```
**Check:** every line prints a version or `…-ok`. Also check that ports `3100`,
`3101` and `3190` are free: `ss -ltn | grep -E ':(3100|3101|3190) '` prints nothing.

### 0.3 Check outbound access
**Why:** images come from `ghcr.io`, the build tools from public Hermit/npm sources, and
the model from the provider.
```bash
for u in https://ghcr.io/v2/ https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -m 10 -w '%{http_code}\n' "$u"
done
```
**Check:** both print `401` (reachable, no credentials sent). A timeout needs a proxy or
firewall change first.

### 0.4 Check the model key and model
**Why:** a wrong region or model ID only shows up later as a silent agent. DashScope
keys are region-specific; use the base URL of the key's region:

| Region | Base URL |
| --- | --- |
| China (Beijing) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Singapore (international) | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| US (Virginia) | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` |
| China (Hong Kong) | `https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1` |

```bash
umask 077
cat > ~/dashscope.key            # paste the key, then Ctrl-D
BASE=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
MODEL=deepseek-v4.1-flash-1017   # HTI Research uses kimi-k3

# Model IDs this key can use (use the bare ID, not an OpenRouter-style vendor/ prefix):
curl -s -H "Authorization: Bearer $(cat ~/dashscope.key)" "$BASE/models" \
  | python3 -c 'import sys,json; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))' | head -50

# COA depends on tool calls; the model must return one here:
curl -s "$BASE/chat/completions" \
  -H "Authorization: Bearer $(cat ~/dashscope.key)" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"Weather in Hangzhou?"}],
       "tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}' \
  | python3 -m json.tool | grep -A3 tool_calls
```
**Check:** the model is listed, and the second call prints a `tool_calls` entry for
`get_weather`. A `401` means the key and base URL are from different regions.

Harness note: `hermes` (COA and the default) and `pi` use chat completions and work
with this endpoint. `codex` needs the Responses API at the base URL; verify that before
creating codex agents. `devin` agents use a Devin account instead.

---

## Phase 1 — Remove the old team (only if one exists)

**Why:** a team created with another advertised URL cannot be moved to
`127.0.0.1:3100`. This deletes its containers, networks, volumes (all Buzz history)
and state; `remove` writes a backup first.
```bash
python3 -m venv ~/buzz-team/.venv && . ~/buzz-team/.venv/bin/activate   # if not already there
pip install -U 'https://github.com/tengso/triflection-team-builder/releases/download/v0.8.18/buzz_team_builder-0.8.18-py3-none-any.whl'
team-builder status
team-builder remove                      # asks for confirmation; backup goes to ~/.local/state/team-builder/backups
docker ps -a --filter label=io.team-builder.project
```
**Check:** the last command lists nothing. Also remove the old community from any Buzz
desktop app that used it.

---

## Phase 2 — Create the team, loopback only

**Why:** the relay and Mission Control listen on loopback only. Browsers reach them
through the host nginx (phase 4); agents connect to the relay over Docker
networking (`--internal-url`), so they keep working regardless of the front end.
```bash
. ~/buzz-team/.venv/bin/activate
umask 077
cat > ~/owner.key                        # owner private key, then Ctrl-D

team-builder init --non-interactive \
  --name "HTI AI Lab" \
  --advertised-url http://127.0.0.1:3100 \
  --internal-url http://relay:3000 \
  --bind 127.0.0.1 --port 3100 \
  --provider custom \
  --base-url https://dashscope-intl.aliyuncs.com/compatible-mode/v1 \
  --model deepseek-v4.1-flash-1017 \
  --owner-key-file ~/owner.key \
  --provider-key-file ~/dashscope.key \
  --dashboard

shred -u ~/owner.key ~/dashscope.key     # the CLI never deletes files you supply
```
Interactive alternative: drop `--non-interactive` and the key/name/model flags and
answer the prompts.

**Check:**
```bash
team-builder status                      # relay, manager, postgres, redis, minio and agent-coa: Up (healthy)
team-builder dashboard status            # enabled on 127.0.0.1:3101
curl -s -H 'Accept: application/nostr+json' -H 'Host: 127.0.0.1:3100' http://127.0.0.1:3100/ | head -c 200   # relay JSON
ls ~/owner.key ~/dashscope.key 2>&1      # "No such file": no key files left behind
```
If `--dashboard` printed a one-time access key, keep it somewhere safe. The owner can
also sign in to Mission Control with their Buzz key from inside the web app.

---

## Phase 3 — Build the Buzz web app

### 3.1 Tools
**Why:** buzz-app pins Node.js and pnpm through Hermit. The pinned pnpm (11.8.0) is a
native binary that needs `libatomic.so.1`, which a minimal Ubuntu lacks
(`error while loading shared libraries: libatomic.so.1`).
```bash
sudo apt-get install -y git rsync nginx libatomic1
cd ~ && git clone https://github.com/tengso/buzz-app.git && cd buzz-app
. bin/activate-hermit
pnpm install --frozen-lockfile
```
**Check:** `pnpm install` finishes without errors. If a shared library is still missing,
`ldd ~/.cache/hermit/pkg/pnpm-*/pnpm | grep 'not found'` names it.

### 3.2 Branding (optional)
**Why:** replaces "Buzz" in the interface, the tab title and icons. Fixed at build time.
```bash
mkdir -p ~/brand
echo '{"name":"HTI AI Lab","title":"HTI AI Lab Team","themeColor":"#0b3d91"}' > ~/brand/brand.json
# optional: logo.svg, loading.svg, favicon.png, apple-touch-icon.png
```

### 3.3 Build and publish
**Why:** `BUZZ_PROXIED_RELAY_URL` makes asset paths relative, sends relay traffic to the
page's own path (so it works under `/HaiChat/<app>/`) and opens that community
automatically. `BUZZ_MISSION_CONTROL=1` adds the Mission Control page.
```bash
cd ~/buzz-app && . bin/activate-hermit
BUZZ_HOST=browser \
BUZZ_PROXIED_RELAY_URL=ws://127.0.0.1:3100 \
BUZZ_MISSION_CONTROL=1 \
BUZZ_BRAND_DIR=$HOME/brand \
  pnpm exec vite build --outDir dist-gateway --emptyOutDir

sudo mkdir -p /srv/buzz-web-gateway && sudo chown ubuntu: /srv/buzz-web-gateway
rsync -a --delete dist-gateway/ /srv/buzz-web-gateway/
```
**Check:** `grep -c 127.0.0.1:3100 /srv/buzz-web-gateway/assets/index-*.js` is non-zero.
To update the front end later: `git pull`, `pnpm install --frozen-lockfile`, the same
build command and `rsync`. No restart is needed (`index.html` is served uncached).

---

## Phase 4 — Host nginx

**Why:** one server block serves the app and proxies the relay and Mission Control on
the same origin. It listens only on the LXD bridge address, so it is reachable from
the cloud VM but not from Docker or Tailscale. This is buzz-app's
`docs/browser-host.gateway.nginx.conf` with the Keel block removed.

Write `/etc/nginx/sites-available/buzz-gateway`:
```nginx
# et2.htisec.com/HaiChat/hti_ailab_team -> cloud-VM nginx (strips /hti_ailab_team/) -> here
server {
  listen 10.174.198.22:3190;     # LXD bridge only: reachable from the cloud VM
  server_name _;
  client_max_body_size 200m;
  root /srv/buzz-web-gateway;

  # Strict CSP: required when users keep their key in the page. frame-src 'self'
  # lets the Mission Control page frame <app>/dashboard/.
  set $buzz_csp "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

  # "/" is both the app and the relay: WebSocket upgrades and NIP-11 go to the relay.
  location = / {
    error_page 418 = @relay;
    if ($http_upgrade ~* websocket)                { return 418; }
    if ($http_accept ~* "application/nostr\+json") { return 418; }
    add_header Vary Accept always;
    add_header Cache-Control no-cache always;
    etag off;
    if_modified_since off;
    add_header Content-Security-Policy $buzz_csp always;
    try_files /index.html =404;
  }

  location /dashboard/ {                         # Mission Control (BUZZ_MISSION_CONTROL=1)
    proxy_pass http://127.0.0.1:3101;            # team-builder dashboard --bind/--port
    proxy_set_header Host $http_host;
    proxy_read_timeout 300s;
  }

  location / {
    add_header Content-Security-Policy $buzz_csp always;
    try_files $uri @relay;
  }

  location @relay {
    proxy_pass http://127.0.0.1:3100;            # team-builder --bind/--port
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host 127.0.0.1:3100;        # must equal the --advertised-url host:port
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
    proxy_request_buffering off;
    proxy_buffering off;
  }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/buzz-gateway /etc/nginx/sites-enabled/buzz-gateway
sudo nginx -t && sudo systemctl reload nginx
```
**Check** both meanings of `/`, and the dashboard:
```bash
curl -s -H 'Accept: application/nostr+json' http://10.174.198.22:3190/ | head -c 200   # relay JSON
curl -s http://10.174.198.22:3190/ | head -c 100                                       # <!doctype html>
curl -s -o /dev/null -w '%{http_code}\n' http://10.174.198.22:3190/dashboard/          # 200 or 30x
```
If the first returns HTML, the browser later fails with
`Unexpected token '<' ... is not valid JSON`.

---

## Phase 5 — Cloud VM nginx (*cloud VM*)

**Why:** the VM receives the gateway's requests for `/hti_ailab_team/…`, strips that
prefix and forwards to the host. WebSocket upgrades, long timeouts and 200 MB uploads
are required. Add next to the existing `hti_research_team` block:
```nginx
# absolute_redirect off keeps the Location relative, so the gateway can map it back under /HaiChat.
location = /hti_ailab_team {
  absolute_redirect off;
  return 301 /hti_ailab_team/;
}
location /hti_ailab_team/ {
  proxy_pass http://10.174.198.22:3190/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "Upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_read_timeout 1d;
  proxy_send_timeout 1d;
  client_max_body_size 200m;
  proxy_buffering off;
  proxy_request_buffering off;
}
```
```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s -H 'Accept: application/nostr+json' http://127.0.0.1/hti_ailab_team/ | head -c 200   # relay JSON
```
**Check:** relay JSON. If not, test the host directly from the VM
(`curl http://10.174.198.22:3190/`); a failure there is a network ACL/firewall between
the VM and the container, not nginx.

---

## Phase 6 — Register the app with the HaiChat gateway (*gateway*)

**Why:** the gateway is the outer login gate and owns the `https://et2.htisec.com`
certificate. Register `hti_ailab_team` the same way as `hti_research_team` (and the
Streamlit apps), pointing at the cloud VM. Its proxy must pass WebSocket upgrades,
allow uploads of up to 200 MB and use long read timeouts.

**Check (*browser*):** `https://et2.htisec.com/HaiChat/hti_ailab_team/` (with the
trailing slash) shows the gateway login, then the Buzz sign-in page.

---

## Phase 7 — Sign in and talk to COA (*browser*)

1. Open `https://et2.htisec.com/HaiChat/hti_ailab_team/` and log in to the gateway.
2. Choose **Use my key on this device**, paste the owner `nsec` and set a passphrase
   (only the NIP-49 encrypted key is stored in the browser). A NIP-07 signer extension
   also works.
3. The community opens automatically. Open **Office Of COA** and ask
   *"What does the team look like right now?"*.
4. **Mission Control** in the sidebar signs the owner in with the same key.
5. Invite people: ask COA for an invite link (*"Make a 7-day, 5-use invite link"*).
   Each person opens the same URL, passes the gateway login and signs in with their
   own key. The gateway login does not grant Buzz access by itself.

**Check:** COA replies in the office; on the host,
`team-builder logs coa --since 5m` shows a turn starting and `turn delivered`.

Optional, for desktop-app users inside the network: because the advertised URL is
`127.0.0.1:3100`, the Buzz desktop app works through
`ssh -N -L 127.0.0.1:3100:127.0.0.1:3100 hti-ailab` with community
`http://127.0.0.1:3100`.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `python3 -m venv` fails: `No module named 'ensurepip'` | `sudo apt-get install python3.12-venv` (0.2). |
| `permission denied ... /var/run/docker.sock` | `sudo usermod -aG docker ubuntu`, then log in again (0.2). |
| `pnpm: ... libatomic.so.1: cannot open shared object file` | `sudo apt-get install libatomic1` (3.1). |
| App shows `Unexpected token '<' ... is not valid JSON` | NIP-11 requests are not reaching the relay. Check the `location = /` block and the phase 4 curl. |
| Sign-in or relay requests fail with auth errors | The three `127.0.0.1:3100` settings disagree (advertised URL, `BUZZ_PROXIED_RELAY_URL`, nginx `Host`). Rebuild or fix nginx; the advertised URL itself cannot change. |
| Mission Control page says the frame is blocked | `frame-src 'self'` is missing from `$buzz_csp`, or the `/dashboard/` block is missing. |
| Everything worked, then requests fail | The gateway session expired. Reload the page and log in to the gateway again. |
| COA does not answer, all containers healthy | `team-builder logs coa --tail 200`. A `401` from the provider means key/region mismatch; `404` a wrong model ID. |
| COA started a reply that never arrived after it added itself to a channel | Known issue ([#1](https://github.com/tengso/triflection-team-builder/issues/1)): changing an agent's own channel membership restarts its gateway mid-turn. Ask COA to *create channel X and add yourself to it*, wait for its reply, then ask it to post there. |
| Sidebar app (Settings → Apps) refuses or breaks another web app | Apps must be on the same origin under a path (e.g. `/HaiChat/hti_ailab_team/<app>/`) and allow framing. Apps that only work at the root of their own hostname (for example Plane) cannot be embedded; open them in their own tab. |

## Removing or re-creating later

- `team-builder remove` deletes the team (with a backup). The nginx site and
  `/srv/buzz-web-gateway` stay; a new team with the **same** advertised URL works with
  them unchanged, but users must add the community again.
- Anything attached to the team's Docker network by name (for example a sidecar on
  `tb-<project>_community`) must be updated after re-creation, because the project ID
  changes.
