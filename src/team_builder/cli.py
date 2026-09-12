import argparse
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import secrets
import subprocess
import sys
import tempfile
import uuid
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .buzz import Buzz, policy
from .compose import render
from .nostr import attestation, key, parse_key, public
from .storage import private_write

INFRA_IMAGES = {
    "postgres": "postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73",
    "redis": "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf",
    "minio": "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e",
}
PUBLISHED_IMAGES = json.loads(
    files("team_builder").joinpath("resources/images.json").read_text()
)


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    if result.returncode:
        # Docker config/build output may contain credentials; do not echo it.
        raise RuntimeError(
            f"{args[0]} {args[1]} failed (exit {result.returncode}); inspect Docker locally"
        )
    return result.stdout.strip()


def required(value, label, noninteractive, hidden=False):
    if value:
        return value
    if noninteractive:
        raise ValueError(label + " is required in non-interactive mode")
    value = (getpass.getpass if hidden else input)(label + ": ").strip()
    if not value:
        raise ValueError(label + " cannot be empty")
    return value


def validate_network(endpoint, bind, port):
    url = urlsplit(endpoint)
    if (
        url.scheme != "http"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Use an http://host:port URL without credentials, path, query, or fragment"
        )
    if (url.port or 80) != port:
        raise ValueError("The advertised URL port must match --port")
    address = ipaddress.ip_address(bind)
    if address.version != 4 or address.is_loopback:
        raise ValueError(
            "--bind must be a private-network IPv4 address or 0.0.0.0 so agent containers can reach the relay"
        )
    if url.hostname.lower() == "localhost":
        raise ValueError(
            "Advertise a private-network hostname or address, not localhost"
        )
    try:
        advertised = ipaddress.ip_address(url.hostname)
    except ValueError:
        return
    if advertised.version != 4 or advertised.is_loopback or advertised.is_unspecified:
        raise ValueError(
            "Advertise a reachable private-network IPv4 address or hostname"
        )


def resolve_image(reference):
    if not reference:
        raise ValueError("Buzz and Hermes image references are required")
    try:
        return run(["docker", "image", "inspect", "--format", "{{.Id}}", reference])
    except RuntimeError:
        run(["docker", "pull", reference], timeout=900)
        return run(["docker", "image", "inspect", "--format", "{{.Id}}", reference])


def build_runtime(hermes_image):
    # Build a small context from the installed package, so this works from a wheel too.
    import shutil

    base_tag = "team-builder-hermes-base:" + hermes_image.removeprefix("sha256:")[:16]
    run(["docker", "tag", hermes_image, base_tag])

    with tempfile.TemporaryDirectory(prefix="team-builder-build-") as temp:
        root = Path(temp)
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(root / "wheelhouse"),
                "pip",
                "setuptools",
                "wheel",
                "httpx>=0.27,<1",
                "coincurve>=20,<22",
                "PyYAML>=6,<7",
                "bech32>=1.2,<2",
                "mcp>=1.10,<2",
            ],
            timeout=600,
        )
        package = Path(str(files("team_builder")))
        shutil.copytree(
            package,
            root / "src/team_builder",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (root / "pyproject.toml").write_text("""[build-system]
requires=["setuptools>=69"]
build-backend="setuptools.build_meta"
[project]
name="buzz-team-builder"
version="0.1.0"
requires-python=">=3.12"
dependencies=["httpx>=0.27,<1","coincurve>=20,<22","PyYAML>=6,<7","bech32>=1.2,<2","mcp>=1.10,<2"]
[project.scripts]
team-builder-manager="team_builder.server:main"
team-builder-mcp="team_builder.mcp:main"
team-builder-worker="team_builder.worker:main"
[tool.setuptools.packages.find]
where=["src"]
[tool.setuptools.package-data]
team_builder=["resources/*"]
""")
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_file():
                digest.update(path.read_bytes())
        tag = "team-builder-runtime:" + digest.hexdigest()[:16]
        run(
            [
                "docker",
                "build",
                "--build-arg",
                "HERMES_IMAGE=" + base_tag,
                "-f",
                str(root / "src/team_builder/resources/Dockerfile"),
                "-t",
                tag,
                str(root),
            ],
            timeout=1200,
        )
        return resolve_image(tag)


def prepare_runtime(image, custom_base=False):
    if custom_base:
        return build_runtime(image)
    marker = run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "io.team-builder.runtime"}}',
            image,
        ]
    )
    if marker != "1":
        raise ValueError(
            "The runtime image does not contain Team Builder; use --hermes-image for an unbundled Hermes base"
        )
    return image


def init(args):
    if platform.system() != "Linux":
        raise ValueError(
            "Runtime setup supports Linux only; run this command on your Linux host"
        )
    run(["docker", "compose", "version"], timeout=10)
    root = Path(args.state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / "init.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initialize(root, args)


def initialize(root, args):
    if (root / "config.json").exists():
        config = json.loads((root / "config.json").read_text())
        stored = json.loads((root / "secrets.json").read_text())
        if config["host_root"] != str(root):
            raise ValueError(
                "Installation was moved; restore it to its recorded state directory"
            )
        for flag, field in (
            ("name", "name"),
            ("advertised_url", "advertised_url"),
            ("model", "model"),
        ):
            value = getattr(args, flag)
            if value and value != config[field]:
                raise ValueError(
                    "Existing installation settings differ; use a separate state directory"
                )
        owner_secret = None
    else:
        name = required(args.name, "Community name", args.non_interactive)
        endpoint = required(
            args.advertised_url,
            "Advertised URL, e.g. http://ubuntu.orb.local:3100",
            args.non_interactive,
        )
        validate_network(endpoint, args.bind, args.port)
        model = required(args.model, "Default model", args.non_interactive)
        supplied = (
            Path(args.owner_key_file).read_text() if args.owner_key_file else None
        )
        owner_secret = parse_key(
            required(
                supplied,
                "Owner private key (used once, never saved)",
                args.non_interactive,
                True,
            )
        )
        provider_key = (
            Path(args.provider_key_file).read_text().strip()
            if args.provider_key_file
            else os.environ.get("TEAM_BUILDER_PROVIDER_KEY")
        )
        provider_key = required(
            provider_key, "Model provider API key", args.non_interactive, True
        )
        images = {
            **INFRA_IMAGES,
            "mc": args.mc_image,
            "relay": required(
                args.buzz_image, "Buzz relay image", args.non_interactive
            ),
            "hermes": args.hermes_image
            or args.runtime_image
            or PUBLISHED_IMAGES["hermes"],
        }
        print("Pulling and verifying the shared Buzz and Hermes images…", flush=True)
        images = {name: resolve_image(image) for name, image in images.items()}
        runtime_image = prepare_runtime(
            images["hermes"], custom_base=bool(args.hermes_image)
        )
        identifier = str(uuid.uuid4())
        stored = {
            **{k: key() for k in ("admin", "coa", "relay")},
            **{
                k: secrets.token_hex(32)
                for k in ("database", "redis", "s3", "hmac", "token")
            },
            "provider_key": provider_key,
        }
        config = {
            "schema": 1,
            "id": identifier,
            "project": "tb-" + identifier[:12],
            "name": name,
            "owner": public(owner_secret),
            "relay": public(stored["relay"]),
            "coa_auth": attestation(owner_secret, public(stored["coa"])),
            "office": str(uuid.uuid5(uuid.UUID(identifier), "channel/office")),
            "host_root": str(root),
            "advertised_url": endpoint.rstrip("/"),
            "bind": args.bind,
            "port": args.port,
            "provider": args.provider,
            "model": model,
            "base_url": args.base_url,
            "images": images,
            "runtime_image": runtime_image,
        }
        # Secrets are written first: config.json is the durable commit marker for an installation.
        private_write(root / "secrets.json", stored)
        private_write(root / "config.json", config)
    private_write(root / "compose.yaml", render(config, stored).encode())
    compose = ["docker", "compose", "-f", str(root / "compose.yaml")]
    print("Starting isolated Buzz services…", flush=True)
    run([*compose, "up", "-d", "--wait", "--wait-timeout", "240", "relay"], timeout=300)
    run(
        [
            *compose,
            "exec",
            "-T",
            "relay",
            "/usr/local/bin/buzz-admin",
            "add-member",
            "--pubkey",
            public(stored["admin"]),
            "--role",
            "admin",
        ],
        timeout=30,
    )
    host = "127.0.0.1" if config["bind"] == "0.0.0.0" else config["bind"]
    origin = f"http://{host}:{config['port']}"
    admin = Buzz(
        origin,
        stored["admin"],
        config["relay"],
        canonical_origin=config["advertised_url"],
    )
    registration = admin.head(30177, config["owner"], public(stored["coa"]))
    if not registration:
        if owner_secret is None:
            supplied = (
                Path(args.owner_key_file).read_text() if args.owner_key_file else None
            )
            owner_secret = parse_key(
                required(
                    supplied,
                    "Owner key to finish interrupted COA authorization",
                    args.non_interactive,
                    True,
                )
            )
        if public(owner_secret) != config["owner"]:
            raise ValueError("Owner key does not match this installation")
        owner = Buzz(
            origin,
            owner_secret,
            config["relay"],
            canonical_origin=config["advertised_url"],
        )
        owner.replace(30177, [["d", public(stored["coa"])]], policy("Chief of Agents"))
        del owner
    elif registration["content"] != policy("Chief of Agents"):
        raise ValueError("COA registration differs from the expected bootstrap policy")
    owner_secret = None
    print("Starting Chief of Agents and verifying gateway connectivity…", flush=True)
    run(
        [*compose, "up", "-d", "--wait", "--wait-timeout", "300", "manager"],
        timeout=360,
    )
    office = admin.channel(config["office"])
    profile = admin.head(0, public(stored["coa"]))
    if (
        not office
        or config["owner"] not in office["roles"]
        or public(stored["coa"]) not in office["roles"]
        or not profile
        or config["coa_auth"] not in profile["tags"]
    ):
        raise RuntimeError("COA profile or office membership failed final readback")
    print(
        f"Ready: {config['name']} at {config['advertised_url']}\nOpen Office Of COA and describe the team you want.\nState: {root}\nMaintenance: docker compose -f {root / 'compose.yaml'} logs manager"
    )


def parser():
    parser = argparse.ArgumentParser(
        description="Bootstrap a Buzz community managed by Chief of Agents"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init")
    defaults = {
        "state-dir": "~/.local/state/team-builder/default",
        "bind": "0.0.0.0",
        "provider": "openrouter",
        "mc-image": "minio/mc:latest",
        "buzz-image": PUBLISHED_IMAGES["buzz"],
    }
    for name in (
        "state-dir",
        "name",
        "advertised-url",
        "bind",
        "provider",
        "model",
        "base-url",
        "owner-key-file",
        "provider-key-file",
        "buzz-image",
        "hermes-image",
        "runtime-image",
        "mc-image",
    ):
        command.add_argument(
            "--" + name,
            default=os.environ.get(
                "TEAM_BUILDER_" + name.upper().replace("-", "_"), defaults.get(name)
            ),
        )
    command.add_argument(
        "--port", type=int, default=int(os.environ.get("TEAM_BUILDER_PORT", "3100"))
    )
    command.add_argument("--non-interactive", action="store_true")
    return parser


def main():
    args = parser().parse_args()
    try:
        if args.hermes_image and args.runtime_image:
            raise ValueError(
                "Choose either --runtime-image or --hermes-image, not both"
            )
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        if args.provider not in ("openrouter", "openai", "custom") or (
            args.provider == "custom" and not args.base_url
        ):
            raise ValueError(
                "Provider must be openrouter, openai, or custom (with --base-url)"
            )
        if args.base_url and args.provider != "custom":
            raise ValueError("Use --provider custom when supplying --base-url")
        init(args)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        message = (
            str(exc)
            if type(exc) in (ValueError, RuntimeError)
            else "Setup failed; existing state is preserved for retry"
        )
        print(message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
