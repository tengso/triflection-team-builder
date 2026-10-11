"""Start, stop, inspect, back up and remove one Team Builder installation.

Every Docker call has a timeout so a stuck daemon surfaces as an error instead of
a hang. Containers are removed one at a time, after external containers (e.g.
databases attached with attach_dependency) are disconnected from the team's
networks: removing many network-attached containers at once has deadlocked the
Docker daemon. Root-owned state is read and removed through a short-lived
container from the installation's own runtime image, so no sudo is needed.
"""

import hashlib
import json
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import yaml

LABEL = "io.team-builder.project"
GIB = 1024**3
STUCK = (
    "Docker did not answer within {}s. If `docker info` also hangs, the daemon is "
    "stuck: restart it (sudo systemctl restart docker) and run this command again."
)


def docker(*args, timeout=120, input=None):
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            input=input,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(STUCK.format(timeout)) from None
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(
            f"docker {args[0]} failed: {detail[-1] if detail else 'exit ' + str(result.returncode)}"
        )
    return result.stdout.strip()


def passthrough(*args):
    """Run docker with output going straight to the terminal (for logs)."""
    return subprocess.run(["docker", *args], check=False).returncode


def helper_command(image, *args, mounts=()):
    """docker-run arguments for a root command in a throwaway container."""
    volumes = []
    for source, target, mode in mounts:
        volumes += ["-v", f"{source}:{target}:{mode}"]
    return [
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        *volumes,
        "--entrypoint",
        args[0],
        image,
        *args[1:],
    ]


def pinned_images(config):
    refs = {*config.get("images", {}).values(), config.get("runtime_image")}
    refs.add(config.get("manager_image"))
    return {r for r in refs if r}


def repository(ref):
    name = ref.split("@", 1)[0]
    return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name


def verify_backup(directory):
    directory = Path(directory).expanduser().resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Backup file {name} does not match its checksum")
    return directory, manifest


def restore(backup_dir, state_dir=None, run=docker, out=print):
    """Recreate a removed installation from a `backup` directory on this host."""
    directory, manifest = verify_backup(backup_dir)
    for key in ("host_root", "helper_image", "volumes"):
        if key not in manifest:
            raise ValueError(
                "This backup predates restore support (Team Builder 0.8.10)"
            )
    project, root = manifest["project"], Path(manifest["host_root"])
    if state_dir and Path(state_dir).expanduser().resolve() != root:
        raise ValueError(
            f"This backup belongs at {root}: agent workspaces are mounted from that "
            "path, so restore it there (omit --state-dir)"
        )
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"{root} is not empty; remove the installation first")
    for kind, listing in (
        ("containers", ["ps", "-aq"]),
        ("volumes", ["volume", "ls", "-q"]),
        ("networks", ["network", "ls", "-q"]),
    ):
        if run(*listing, "--filter", f"label={LABEL}={project}"):
            raise ValueError(
                f"Docker still has {kind} of {project}; run `team-builder remove` first"
            )
    image = manifest["helper_image"]
    root.parent.mkdir(parents=True, exist_ok=True)
    out(f"Restoring {project} from {directory}")
    run(
        *helper_command(
            image,
            "tar",
            "-xzpf",
            "/backup/state.tgz",
            "-C",
            "/state-parent",
            mounts=[(directory, "/backup", "ro"), (root.parent, "/state-parent", "rw")],
        ),
        timeout=1800,
    )
    dump = "community-postgres.sql" in manifest["files"]
    for volume, labels in manifest["volumes"].items():
        run(
            "volume",
            "create",
            *[f"--label={k}={v}" for k, v in sorted(labels.items())],
            volume,
        )
        if dump and volume == f"{project}_postgres":
            continue
        out(f"  volume {volume}")
        run(
            *helper_command(
                image,
                "tar",
                "-xzpf",
                f"/backup/volume-{volume}.tgz",
                "-C",
                "/volume",
                mounts=[(directory, "/backup", "ro"), (volume, "/volume", "rw")],
            ),
            timeout=1800,
        )
    compose = ["compose", "-f", str(root / "compose.yaml")]
    if dump:
        out("  community database (from its SQL dump)")
        run(*compose, "up", "-d", "--wait", "postgres", timeout=420)
        run(
            "exec",
            "-i",
            f"{project}-postgres-1",
            "psql",
            "-q",
            "-U",
            "buzz",
            "-d",
            "postgres",
            input=(directory / "community-postgres.sql").read_text(),
            timeout=1800,
        )
    out("Starting the team")
    run(*compose, "up", "-d", "--wait", "--wait-timeout", "300", timeout=420)
    out(
        f"Restored {project}. Agents are relaunched by the manager within a minute. "
        "Deployed applications are not restored as containers: redeploy their current "
        "releases (Mission Control → Redeploy) once the team is up."
    )
    return root


class Installation:
    def __init__(self, state_dir, run=docker, out=print, stream=passthrough):
        self.root = Path(state_dir).expanduser().resolve()
        config_path = self.root / "config.json"
        if not config_path.is_file():
            raise ValueError(f"No Team Builder installation at {self.root}")
        self.config = json.loads(config_path.read_text())
        self.project = self.config["project"]
        self.compose = ["compose", "-f", str(self.root / "compose.yaml")]
        self.helper_image = (
            self.config.get("manager_image") or self.config["runtime_image"]
        )
        self.run = run
        self.out = out
        self.stream = stream

    # ── inventory ──────────────────────────────────────────────────────────
    def containers(self):
        """Team containers as dicts: name, state, status, role."""
        lines = self.run(
            "ps",
            "-a",
            "--filter",
            f"label={LABEL}={self.project}",
            "--format",
            "{{json .}}",
        )
        result = []
        for line in filter(None, lines.splitlines()):
            item = json.loads(line)
            labels = dict(
                pair.split("=", 1)
                for pair in item.get("Labels", "").split(",")
                if "=" in pair
            )
            name = item["Names"]
            if labels.get("io.team-builder.component") == "preflight":
                role = "probe"
            elif "io.team-builder.application" in labels:
                role = "app"
            elif name.startswith(self.project + "-agent-"):
                role = "agent"
            else:
                role = "service"
            result.append(
                {
                    "name": name,
                    "state": item["State"],
                    "status": item["Status"],
                    "role": role,
                    "image": item.get("Image", ""),
                }
            )
        return sorted(result, key=lambda c: (c["role"], c["name"]))

    def networks(self):
        names = set(
            self.run(
                "network",
                "ls",
                "--filter",
                f"label={LABEL}={self.project}",
                "--format",
                "{{.Name}}",
            ).split()
        )
        names |= {
            n
            for n in self.run("network", "ls", "--format", "{{.Name}}").split()
            if n.startswith((self.project + "-", self.project + "_"))
        }
        return sorted(names)

    def volumes(self):
        names = set(
            self.run(
                "volume", "ls", "-q", "--filter", f"label={LABEL}={self.project}"
            ).split()
        )
        names |= {
            v
            for v in self.run("volume", "ls", "-q").split()
            if v.startswith((self.project + "-", self.project + "_"))
        }
        return sorted(names)

    def external_attachments(self):
        """(network, container) pairs where the container is not part of this team."""
        pairs = []
        team = {c["name"] for c in self.containers()}
        for network in self.networks():
            members = self.run(
                "network",
                "inspect",
                network,
                "--format",
                "{{range .Containers}}{{.Name}} {{end}}",
            ).split()
            pairs += [(network, c) for c in members if c not in team]
        return pairs

    # ── commands ───────────────────────────────────────────────────────────
    def status(self):
        self.out(f"Installation {self.project} at {self.root}")
        self.out(f"Community: {self.config.get('advertised_url', 'unknown')}")
        rows = self.containers()
        if not rows:
            self.out("No containers (stopped and removed, or never started).")
        for c in rows:
            self.out(f"  {c['role']:<8} {c['name']:<60} {c['status']}")
        for network, container in self.external_attachments():
            self.out(f"  external {container} attached to {network}")
        return rows

    def stop(self, keep_apps=False):
        """Stop apps, agents and services; keep every container, volume and file."""
        rows = self.containers()
        order = ["app", "probe", "agent"] if not keep_apps else ["probe", "agent"]
        for role in order:
            for c in rows:
                if c["role"] == role and c["state"] == "running":
                    self.out(f"Stopping {role} {c['name']}")
                    self.run("stop", "-t", "20", c["name"], timeout=90)
        self.out("Stopping core services")
        self.run(*self.compose, "stop", timeout=300)
        self.out("Stopped. Data is kept; `team-builder start` resumes the team.")

    def start(self):
        """Start services (the manager relaunches running agents), then the apps."""
        self.out("Starting core services")
        self.run(
            *self.compose, "up", "-d", "--wait", "--wait-timeout", "300", timeout=420
        )
        for c in self.containers():
            if c["role"] == "app" and c["state"] != "running":
                self.out(f"Starting app {c['name']}")
                self.run("start", c["name"], timeout=90)
        self.out("Started. Agents are relaunched by the manager within a minute.")

    def helper(self, *args, mounts=(), timeout=1800):
        """Run a command as root in a throwaway container of the runtime image."""
        return self.run(
            *helper_command(self.helper_image, *args, mounts=mounts), timeout=timeout
        )

    def services(self):
        spec = yaml.safe_load((self.root / "compose.yaml").read_text())["services"]
        return [name for name, s in spec.items() if s.get("restart") != "no"]

    def logs(self, target=None, follow=False, tail=200, since=None):
        options = ["--tail", str(tail), *(["--since", since] if since else [])]
        options += ["--follow"] if follow else []
        if target is None or target in self.services():
            return self.stream(*self.compose, "logs", *options, *filter(None, [target]))
        names = [c["name"] for c in self.containers()]
        prefix = self.project + "-"
        exact = (
            target,
            f"{prefix}agent-{target}",
            prefix + target,
            f"{prefix}{target}-1",
        )
        matches = [n for n in names if n in exact] or [
            n for n in names if n.endswith(target)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Unknown or ambiguous target {target!r}. Services: "
                + ", ".join(self.services())
                + "; containers: "
                + ", ".join(n.removeprefix(self.project + "-") for n in names)
            )
        return self.stream("logs", *options, matches[0])

    def agent(self, action, identifier=None):
        agents = [c for c in self.containers() if c["role"] == "agent"]
        if action == "list":
            for c in agents:
                agent_id = c["name"].removeprefix(self.project + "-agent-")
                self.out(f"  {agent_id:<20} {c['status']:<28} {c['image']}")
            return agents
        name = f"{self.project}-agent-{identifier}"
        if name not in {c["name"] for c in agents}:
            known = ", ".join(
                c["name"].removeprefix(self.project + "-agent-") for c in agents
            )
            raise ValueError(f"No agent {identifier!r}; agents: {known or 'none'}")
        verb = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}
        self.out(f"{verb[action]} agent {identifier}")
        self.run(
            action, *([] if action == "start" else ["-t", "20"]), name, timeout=120
        )
        if action == "stop":
            self.out(
                "Stopped. COA still lists the agent as running, so the manager starts it "
                "again when it restarts or the agent's configuration changes."
            )

    def prune(self, dry_run=False, confirm=lambda prompt: True):
        """Remove leftover probe containers and Team Builder images nothing uses."""
        probes = [
            c["name"]
            for c in self.containers()
            if c["role"] == "probe" and c["state"] != "running"
        ]
        keep = set()
        for config in self.root.parent.glob("*/config.json"):
            try:
                keep |= pinned_images(json.loads(config.read_text()))
            except (OSError, ValueError):
                continue
        repos = {repository(r) for r in keep}
        keep_ids = set()
        for ref in keep:
            try:
                keep_ids.add(self.run("image", "inspect", "--format", "{{.Id}}", ref))
            except RuntimeError:
                pass
        containers = self.run("ps", "-aq", "--no-trunc").split()
        used = set(
            self.run("inspect", "--format", "{{.Image}}", *containers).split()
            if containers
            else []
        )
        candidates = {}
        for line in filter(
            None,
            self.run("images", "--no-trunc", "--format", "{{json .}}").splitlines(),
        ):
            image = json.loads(line)
            if (
                image["Repository"] in repos
                and image["ID"] not in keep_ids | used
                and image["ID"] not in candidates
            ):
                candidates[image["ID"]] = (
                    f"{image['Repository']} ({image['Size']}, {image['CreatedSince']})"
                )
        for name in probes:
            self.out(f"  probe container {name}")
        for description in candidates.values():
            self.out(f"  image {description}")
        if not probes and not candidates:
            self.out("Nothing to prune.")
            return []
        if dry_run or not confirm(
            f"Remove {len(probes)} containers and {len(candidates)} images? [y/N] "
        ):
            self.out("Nothing removed.")
            return []
        for name in probes:
            self.run("rm", "-f", name, timeout=120)
        for image_id in candidates:
            self.run("image", "rm", "-f", image_id, timeout=300)
        self.out(f"Removed {len(probes)} containers and {len(candidates)} images.")
        return [*probes, *candidates]

    def doctor(self):
        """Read-only health check; returns a list of (level, check, detail)."""
        results = []

        def report(level, check, detail):
            results.append((level, check, detail))
            self.out(f"{level:<4} {check}: {detail}")

        try:
            report(
                "PASS",
                "docker",
                "daemon "
                + self.run("info", "--format", "{{.ServerVersion}}", timeout=15),
            )
        except RuntimeError as exc:
            report("FAIL", "docker", str(exc))
            return results
        docker_root = self.run("info", "--format", "{{.DockerRootDir}}", timeout=15)
        for label, path in (("state disk", self.root), ("docker disk", docker_root)):
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            free = f"{usage.free / GIB:.1f} GiB free of {usage.total / GIB:.0f} GiB"
            level = (
                "FAIL"
                if usage.free < GIB
                else "WARN"
                if usage.free < 5 * GIB or usage.free < usage.total / 10
                else "PASS"
            )
            report(level, label, free)
        loose = [
            p.name
            for p in (self.root / "secrets.json", self.root / "config.json")
            if p.exists() and p.stat().st_mode & 0o077
        ]
        report(
            "FAIL" if loose else "PASS",
            "permissions",
            f"{', '.join(loose)} readable by others" if loose else "private",
        )
        missing = []
        for ref in sorted(pinned_images(self.config)):
            try:
                self.run("image", "inspect", "--format", "{{.Id}}", ref)
            except RuntimeError:
                missing.append(ref)
        report(
            "WARN" if missing else "PASS",
            "images",
            f"not present locally: {', '.join(missing)}"
            if missing
            else "all pinned images present",
        )
        rows = {c["name"]: c for c in self.containers()}

        def health(c):
            if c["state"] != "running":
                return "FAIL", c["status"]
            if "(unhealthy)" in c["status"]:
                return "FAIL", c["status"]
            return ("WARN" if "health: starting" in c["status"] else "PASS"), c[
                "status"
            ]

        for service in self.services():
            c = rows.get(f"{self.project}-{service}-1")
            level, detail = health(c) if c else ("FAIL", "no container")
            report(level, f"service {service}", detail)
        bind, port = self.config.get("bind", "127.0.0.1"), self.config.get("port")
        if port:
            host = "127.0.0.1" if bind in ("0.0.0.0", "", None) else bind
            try:
                urllib.request.urlopen(f"http://{host}:{port}/", timeout=5)
                report("PASS", "relay", f"answers on {host}:{port}")
            except urllib.error.HTTPError as exc:
                report("PASS", "relay", f"answers on {host}:{port} (HTTP {exc.code})")
            except OSError as exc:
                report("FAIL", "relay", f"no answer on {host}:{port} ({exc})")
        dashboard = self.config.get("dashboard", {})
        if dashboard.get("enabled"):
            try:
                socket.create_connection(
                    (dashboard.get("bind", "127.0.0.1"), dashboard["port"]), timeout=5
                ).close()
                report(
                    "PASS", "mission control", f"listening on port {dashboard['port']}"
                )
            except OSError:
                report(
                    "FAIL", "mission control", f"nothing on port {dashboard['port']}"
                )
        for c in rows.values():
            if c["role"] in ("agent", "app"):
                level, detail = health(c)
                if c["role"] == "agent" and c["state"] != "running":
                    level = "WARN"
                report(
                    level,
                    f"{c['role']} {c['name'].removeprefix(self.project + '-')}",
                    detail,
                )
            if c["role"] == "probe":
                report(
                    "WARN",
                    "leftover probe",
                    f"{c['name']} (team-builder prune removes it)",
                )
        environments = {}
        for network, container in self.external_attachments():
            environments.setdefault(container, set()).add(network.rsplit("-", 1)[-1])
            report("PASS", "attached", f"{container} → {network}")
        for container, envs in environments.items():
            if len(envs) > 1:
                report(
                    "FAIL",
                    "isolation",
                    f"{container} is attached to {', '.join(sorted(envs))} networks",
                )
        backups = sorted(
            (self.root.parent / "backups").glob(f"{self.project}-*/manifest.json")
        )
        if not backups:
            report("WARN", "backup", "none found (team-builder backup)")
        else:
            age = (time.time() - backups[-1].stat().st_mtime) / 86400
            report(
                "WARN" if age > 7 else "PASS",
                "backup",
                f"newest {age:.0f} days old ({backups[-1].parent.name})",
            )
        return results

    def export_credentials(self, output):
        """Write the current version of every deployment credential to a private file."""
        output = Path(output).expanduser()
        if output.exists():
            raise ValueError(f"{output} already exists; choose a new file")
        bundle = {"project": self.project, "credentials": []}
        profiles = self.root / "deployments" / "profiles"
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "profiles.tgz"
            if profiles.parent.is_dir():
                self.helper(
                    "sh",
                    "-c",
                    "[ -d /deployments/profiles ] && tar -czf /out/profiles.tgz -C /deployments profiles"
                    f" && chown {Path(temporary).stat().st_uid} /out/profiles.tgz || true",
                    mounts=[
                        (profiles.parent, "/deployments", "ro"),
                        (temporary, "/out", "rw"),
                    ],
                )
            if archive.exists():
                with tarfile.open(archive) as tar:
                    members = {m.name: m for m in tar.getmembers() if m.isfile()}
                    for name, member in sorted(members.items()):
                        parts = name.split("/")
                        if len(parts) != 5 or parts[3] != "credentials":
                            continue
                        _, app, env, _, _ = parts
                        current = json.load(tar.extractfile(member))
                        value = (
                            tar.extractfile(
                                members[
                                    f"profiles/{app}/{env}/versions/{current['id']}/{current['version']}"
                                ]
                            )
                            .read()
                            .decode()
                        )
                        bundle["credentials"].append(
                            {
                                "application": app,
                                "environment": env,
                                "id": current["id"],
                                "value": value,
                            }
                        )
                archive.unlink()
        output.touch(mode=0o600, exist_ok=False)
        output.write_text(json.dumps(bundle, indent=2) + "\n")
        self.out(
            f"Exported {len(bundle['credentials'])} credentials to {output} "
            "(plain text secrets: keep it private and delete it after importing)"
        )
        return bundle

    def import_credentials(self, path, rotate=False, request=None):
        """Store exported credentials through the manager of this installation."""
        if request is None:
            from .deployment_cli import operator_request as request
        bundle = json.loads(Path(path).expanduser().read_text())
        summary = {"stored": 0, "unchanged": 0, "failed": 0}
        for item in bundle["credentials"]:
            label = f"{item['application']}/{item['environment']}/{item['id']}"
            try:
                result = request(
                    self.root,
                    {
                        "action": "credential",
                        "application": item["application"],
                        "environment": item["environment"],
                        "id": item["id"],
                        "value": item["value"],
                        "rotate": rotate,
                    },
                )
                key = "stored" if result.get("changed") else "unchanged"
                summary[key] += 1
                self.out(f"  {key:<9} {label}")
            except (ValueError, RuntimeError) as exc:
                summary["failed"] += 1
                self.out(f"  failed    {label}: {exc}")
        self.out(
            f"{summary['stored']} stored, {summary['unchanged']} unchanged, {summary['failed']} failed"
        )
        return summary

    def backup(self, output=None, keep=None):
        """Archive the state directory, the community database and the data volumes."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = (
            Path(output).expanduser().resolve()
            if output
            else self.root.parent / "backups"
        )
        target = base / f"{self.project}-{stamp}"
        target.mkdir(parents=True, mode=0o700)
        target.chmod(0o700)
        self.out(f"Backing up {self.project} to {target}")
        self.helper(
            "tar",
            "-czf",
            "/backup/state.tgz",
            "-C",
            "/state-parent",
            self.root.name,
            mounts=[
                (self.root.parent, "/state-parent", "ro"),
                (target, "/backup", "rw"),
            ],
        )
        files = ["state.tgz"]
        postgres = f"{self.project}-postgres-1"
        if any(
            c["name"] == postgres and c["state"] == "running" for c in self.containers()
        ):
            dump = self.run("exec", postgres, "pg_dumpall", "-U", "buzz", timeout=1800)
            (target / "community-postgres.sql").write_text(dump + "\n")
            files.append("community-postgres.sql")
        else:
            self.out("  community database not running; its volume is archived instead")
        volumes = {}
        for volume in self.volumes():
            labels = self.run(
                "volume", "inspect", "--format", "{{json .Labels}}", volume
            )
            volumes[volume] = json.loads(labels or "null") or {}
            name = f"volume-{volume}.tgz"
            self.helper(
                "tar",
                "-czf",
                f"/backup/{name}",
                "-C",
                "/volume",
                ".",
                mounts=[(volume, "/volume", "ro"), (target, "/backup", "rw")],
            )
            files.append(name)
        self.helper(
            "chown",
            "-R",
            f"{target.stat().st_uid}:{target.stat().st_gid}",
            "/backup",
            mounts=[(target, "/backup", "rw")],
        )
        manifest = {
            "project": self.project,
            "created_at": stamp,
            "team_builder": version("buzz-team-builder"),
            "advertised_url": self.config.get("advertised_url"),
            "host_root": str(self.root),
            "helper_image": self.helper_image,
            "volumes": volumes,
            "files": {},
        }
        for name in files:
            path = target / name
            path.chmod(0o600)
            manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (target / "manifest.json").chmod(0o600)
        self.out(
            f"Backup complete: {len(files)} archives in {target} (contains secrets; keep it private)"
        )
        if keep:
            older = sorted(
                p.parent
                for p in base.glob(f"{self.project}-*/manifest.json")
                if p.parent != target
            )
            for old in older[: max(0, len(older) - (keep - 1))]:
                self.out(f"  removing old backup {old.name}")
                shutil.rmtree(old)
        return target

    def remove(self):
        """Remove every container, network, volume and the state of this team."""
        for network, container in self.external_attachments():
            self.out(f"Disconnecting external {container} from {network}")
            self.run("network", "disconnect", network, container, timeout=60)
        for c in self.containers():
            if c["role"] != "service":
                self.out(f"Removing {c['role']} {c['name']}")
                self.run("rm", "-f", c["name"], timeout=120)
        self.out("Removing core services and their volumes")
        self.run(*self.compose, "down", "-v", "--remove-orphans", timeout=600)
        for c in self.containers():
            self.run("rm", "-f", c["name"], timeout=120)
        for network in self.networks():
            self.out(f"Removing network {network}")
            self.run("network", "rm", network, timeout=60)
        for volume in self.volumes():
            self.out(f"Removing volume {volume}")
            self.run("volume", "rm", volume, timeout=120)
        self.out(f"Removing state directory {self.root}")
        self.helper(
            "rm",
            "-rf",
            f"/state-parent/{self.root.name}",
            mounts=[(self.root.parent, "/state-parent", "rw")],
        )
        left = self.containers() or self.networks() or self.volumes()
        if left:
            raise RuntimeError(f"Some resources remain: {left}")
        self.out(
            f"Removed {self.project}. Images are kept (docker image prune -a frees them); "
            "remove the community from your Buzz app before creating a new team."
        )
