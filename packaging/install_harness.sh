#!/bin/sh
# Install one pinned harness ACP adapter (pi, codex, devin) inside the image.
set -eu

harness="$1"
pins=/opt/team-builder/harness.json

field() {
    python3 -c "import json; print(json.load(open('$pins'))['$1']['$2'])"
}

install_node() {
    curl -fsSL "$(field node url)" -o /tmp/node.tar.xz
    echo "$(field node sha256)  /tmp/node.tar.xz" | sha256sum -c -
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
    rm /tmp/node.tar.xz
}

case "$harness" in
pi)
    install_node
    npm install -g "@earendil-works/pi-coding-agent@$(field pi version)" \
        "pi-acp@$(field pi_acp version)"
    npm cache clean --force
    pi-acp --version
    ;;
codex)
    install_node
    npm install -g "@agentclientprotocol/codex-acp@$(field codex_acp version)"
    npm cache clean --force
    codex-acp --version
    mkdir -p /usr/share/team-builder
    npm ls -g --depth=1 --json | python3 -c "
import json, sys
tree = json.load(sys.stdin)
deps = tree.get('dependencies', {})
nested = deps.get('@agentclientprotocol/codex-acp', {}).get('dependencies', {})
resolved = nested.get('@openai/codex', {}).get('version') \
    or deps.get('@openai/codex', {}).get('version') \
    or 'unknown'
open('/usr/share/team-builder/codex-runtime.txt', 'w').write(resolved + '\n')
"
    cat /usr/share/team-builder/codex-runtime.txt
    ;;
devin)
    curl -fsSL "$(field devin url)" -o /tmp/devin.tar.gz
    echo "$(field devin sha256)  /tmp/devin.tar.gz" | sha256sum -c -
    mkdir -p /tmp/devin-cli
    tar -xzf /tmp/devin.tar.gz -C /tmp/devin-cli
    install -m 0755 /tmp/devin-cli/bin/devin /usr/local/bin/devin
    rm -rf /tmp/devin.tar.gz /tmp/devin-cli
    devin --version
    ;;
*)
    echo "Unknown harness: $harness" >&2
    exit 1
    ;;
esac
