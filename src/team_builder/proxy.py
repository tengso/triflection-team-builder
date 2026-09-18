"""Host-managed agent egress proxy; never inherited from the operator shell."""

import fcntl
import ipaddress
import json
import platform
from pathlib import Path
from urllib.parse import urlsplit

from .storage import private_write


def validate_url(value):
    error = "Use an HTTP proxy origin without credentials, path, query, or fragment"
    try:
        url = urlsplit(value)
        port = url.port
        if (
            url.scheme != "http"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or port == 0
            or any(c.isspace() for c in value)
        ):
            raise ValueError(error)
        host = url.hostname
        if host.lower() == "localhost":
            raise ValueError(error)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_loopback or address.is_unspecified):
            raise ValueError(error)
    except ValueError:
        raise ValueError(error) from None
    return value.rstrip("/")


def environment(root, config):
    path = root / "agent-proxy.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text()).get("url")
    if not value:
        return {}
    proxy = validate_url(value)
    bypass = {
        "localhost",
        "127.0.0.1",
        "::1",
        "relay",
        "manager",
        "postgres",
        "redis",
        "minio",
    }
    for key in ("internal_url", "advertised_url"):
        if config.get(key):
            bypass.add(urlsplit(config[key]).hostname)
    no_proxy = ",".join(sorted(bypass))
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def command(args):
    from .cli import run

    root = Path(args.state_dir).expanduser().resolve()
    if not (root / "config.json").is_file():
        raise ValueError("Installation not found")
    if args.proxy_command == "status":
        settings = environment(root, json.loads((root / "config.json").read_text()))
        print("Agent HTTP proxy: " + settings.get("HTTPS_PROXY", "disabled"))
        if settings:
            print("Direct connections: " + settings["NO_PROXY"])
        return
    if platform.system() != "Linux":
        raise ValueError("Configure the proxy on the Linux host of the installation")
    value = validate_url(args.url) if args.proxy_command == "set" else None
    with (root / "init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((root / "config.json").read_text())
        if config["host_root"] != str(root):
            raise ValueError("Installation was moved; restore its recorded state path")
        # Reject old managers before persisting a setting they would silently ignore.
        run(
            [
                "docker",
                "compose",
                "-f",
                str(root / "compose.yaml"),
                "exec",
                "-T",
                "manager",
                "/opt/hermes/.venv/bin/python",
                "-c",
                "import team_builder.proxy",
            ]
        )
        private_write(root / "agent-proxy.json", {"url": value})
        print(
            "Proxy setting saved; restarting manager to refresh running agents…",
            flush=True,
        )
        compose = ["docker", "compose", "-f", str(root / "compose.yaml")]
        run([*compose, "restart", "manager"], timeout=120)
        run(
            [
                *compose,
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
    print("Agent proxy applied. Stopped agents will use it when started.")
