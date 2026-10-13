"""Local dashboard provisioning and independent owner authentication."""

import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .storage import private_write

OWNER_KIND = 27235  # NIP-98 HTTP auth kind; the "u" tag names Mission Control
OWNER_AUDIENCE = "mission-control"
CHALLENGE_TTL = 120


class Access:
    def __init__(self, root, clock=time.time, owner=None):
        self.path = Path(root) / "dashboard-auth.json"
        self.clock = clock
        self.lock = threading.Lock()
        self.sessions = {}
        self.attempts = OrderedDict()
        self.global_attempts = deque(maxlen=101)
        self.verifier = None
        self.challenges = OrderedDict()
        self.owner = owner

    def challenge(self, purpose):
        """A single-use nonce the owner signs to sign in ("login") or reveal."""
        with self.lock:
            now = self.clock()
            for nonce, (expires, _) in list(self.challenges.items()):
                if expires <= now:
                    del self.challenges[nonce]
            while len(self.challenges) >= 64:
                self.challenges.popitem(last=False)
            nonce = secrets.token_hex(32)
            self.challenges[nonce] = (now + CHALLENGE_TTL, purpose)
            return nonce

    def owner_check(self, event, purpose, peer):
        """Throttled check of an owner-signed challenge; the caller holds self.lock."""
        from .nostr import tags, verify

        status = self.throttle(peer)
        if status != 200:
            return status
        now = self.clock()
        try:
            nonce = tags(event, "challenge")[0][0]
            expires, expected = self.challenges.pop(nonce, (0, None))
            valid = (
                self.owner
                and expires > now
                and expected == purpose
                and event["kind"] == OWNER_KIND
                and event["pubkey"] == self.owner
                and abs(event["created_at"] - now) <= CHALLENGE_TTL
                and [[OWNER_AUDIENCE]] == tags(event, "u")
                and verify(event)
            )
        except (KeyError, IndexError, TypeError):
            valid = False
        return 200 if valid else 401

    def refresh(self):
        try:
            verifier = json.loads(self.path.read_text())["sha256"]
        except (OSError, ValueError, KeyError):
            verifier = None
        if verifier != self.verifier:
            self.sessions.clear()
            self.verifier = verifier
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}

    def login(self, key, peer, event=None):
        """Sign in with the access key, or with an owner-signed "login" challenge."""
        with self.lock:
            status = (
                self.owner_check(event, "login", peer)
                if event is not None
                else self.check(key, peer)
            )
            if status != 200:
                return status, None
            now = self.clock()
            token = secrets.token_urlsafe(32)
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[token] = now + 8 * 3600
            return 200, token

    def verify(self, key, peer, event=None):
        """Re-check the owner for sensitive actions, with the login throttle."""
        with self.lock:
            return (
                self.owner_check(event, "reveal", peer)
                if event is not None
                else self.check(key, peer)
            )

    def check(self, key, peer):
        """Throttled key comparison; the caller holds self.lock."""
        status = self.throttle(peer)
        if status != 200:
            return status
        digest = hashlib.sha256(key.encode()).hexdigest()
        if not self.verifier or not hmac.compare_digest(digest, self.verifier):
            return 401
        return 200

    def throttle(self, peer):
        """Count one attempt; 429 past 5 a minute per peer or 100 overall."""
        self.refresh()
        now = self.clock()
        attempts = self.attempts.setdefault(peer, deque(maxlen=6))
        self.attempts.move_to_end(peer)
        while len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        for queue in (attempts, self.global_attempts):
            while queue and queue[0] <= now - 60:
                queue.popleft()
        if len(attempts) >= 5 or len(self.global_attempts) >= 100:
            return 429
        attempts.append(now)
        self.global_attempts.append(now)
        return 200

    def valid(self, token):
        with self.lock:
            self.refresh()
            return bool(token and token in self.sessions)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


def provision(root, key_file=None):
    key = secrets.token_urlsafe(32)
    # Write the optional delivery file first; never store plaintext in installation state.
    if key_file:
        output = Path(key_file).expanduser()
        if output.exists():
            raise ValueError("Access key output file already exists")
        with open(
            output, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
        ) as file:
            file.write(key + "\n")
    private_write(
        root / "dashboard-auth.json",
        {"sha256": hashlib.sha256(key.encode()).hexdigest()},
    )
    return key


def command(args):
    from .cli import run

    root = Path(args.state_dir).expanduser().resolve()
    config_path = root / "config.json"
    if not config_path.is_file():
        raise ValueError("Installation not found")
    with (root / "init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads(config_path.read_text())
        if config["host_root"] != str(root):
            raise ValueError("Installation was moved; restore its recorded state path")
        current = config.get("dashboard", {})
        if args.dashboard_command == "status":
            host = urlsplit(config["advertised_url"]).hostname
            print(
                f"Mission Control: {'enabled' if current.get('enabled') else 'disabled'}"
            )
            if current.get("enabled"):
                print(f"http://{host}:{current['port']}/dashboard/")
            return
        key = None
        if args.dashboard_command in ("enable", "rotate-key"):
            if args.dashboard_command == "rotate-key" and not current.get("enabled"):
                raise ValueError("Enable the dashboard first")
            if args.dashboard_command == "enable":
                bind = args.bind or current.get("bind", config["bind"])
                port = (
                    args.port
                    if args.port is not None
                    else current.get("port", config["port"] + 1)
                )
                if (
                    ipaddress.ip_address(bind).version != 4
                    or not 1 <= port <= 65535
                    or port == config["port"]
                ):
                    raise ValueError(
                        "Use an IPv4 bind address and a distinct port between 1 and 65535"
                    )
                current = {"enabled": True, "bind": bind, "port": port}
            if (
                args.dashboard_command == "rotate-key"
                or not (root / "dashboard-auth.json").exists()
            ):
                key = provision(root, args.key_output)
                # Deliver before Compose: failed startup must not lose the one-time key.
                print(
                    f"Access key saved to {args.key_output}"
                    if args.key_output
                    else f"Dashboard access key (shown once): {key}",
                    flush=True,
                )
        if args.dashboard_command != "rotate-key":
            current["enabled"] = args.dashboard_command == "enable"
            config["dashboard"] = current
            document = yaml.safe_load((root / "compose.yaml").read_text())
            manager = document["services"]["manager"]
            manager.pop("ports", None)
            if current["enabled"]:
                manager["ports"] = [f"{current['bind']}:{current['port']}:8089"]
            private_write(config_path, config)
            private_write(root / "compose.yaml", yaml.safe_dump(document).encode())
            run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(root / "compose.yaml"),
                    "up",
                    "-d",
                    "--no-deps",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    "manager",
                ],
                timeout=360,
            )
        args.dashboard_command = "status"
    command(args)
