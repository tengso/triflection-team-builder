import json
import shutil
import sys
import tarfile

import pytest

from team_builder import lifecycle
from team_builder.lifecycle import LABEL, Installation, restore

P = "tb-test-123"


class FakeDocker:
    """A small in-memory Docker: containers, networks, volumes and a call log."""

    def __init__(self):
        self.calls = []
        self.containers = {
            f"{P}-manager-1": ("running", ""),
            f"{P}-relay-1": ("running", ""),
            f"{P}-postgres-1": ("running", ""),
            f"{P}-agent-coa": ("running", ""),
            f"{P}-agent-cody": ("exited", ""),
            f"{P}-app-portal-staging-ui": (
                "running",
                "io.team-builder.application=portal",
            ),
        }
        self.networks = {
            f"{P}_community": [f"{P}-manager-1", f"{P}-agent-coa"],
            f"{P}-app-portal-staging": [f"{P}-app-portal-staging-ui", "db-uat-mysql"],
        }
        self.volumes = [f"{P}_postgres", f"{P}_relay"]
        self.images_present = {"sha256:abc": "sha256:pinned"}
        self.image_list = []
        self.profiles = None
        self.blocked = set()
        self.unhealthy = False

    def __call__(self, *args, timeout=120, input=None):
        self.calls.append(args)
        if args[:2] == ("ps", "-a"):
            return "\n".join(
                json.dumps(
                    {
                        "Names": name,
                        "State": state,
                        "Status": state,
                        "Labels": f"io.team-builder.project={P}"
                        + ("," + extra if extra else ""),
                    }
                )
                for name, (state, extra) in self.containers.items()
            )
        if args[:2] == ("network", "ls"):
            return "\n".join(self.networks)
        if args[:2] == ("network", "inspect"):
            return " ".join(self.networks[args[2]])
        if args[:2] == ("network", "disconnect"):
            self.networks[args[2]].remove(args[3])
        if args[:2] == ("network", "rm"):
            assert not [
                c for c in self.networks[args[2]] if c not in self.containers
            ], "network removed while an external container is attached"
            del self.networks[args[2]]
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.volumes)
        if args[:2] == ("volume", "rm"):
            self.volumes.remove(args[2])
        if args[0] == "rm":
            assert len(args) == 3, "containers must be removed one at a time"
            self.containers.pop(args[2], None)
            for members in self.networks.values():
                if args[2] in members:
                    members.remove(args[2])
        if args[0] == "compose" and "down" in args:
            for name in [n for n in self.containers if n.endswith("-1")]:
                del self.containers[name]
            self.networks.pop(f"{P}_community", None)
            self.volumes = [v for v in self.volumes if not v.startswith(P + "_")]
        if args[0] == "exec" and "bash" in args:
            if any(f"/{name}/" in args[-1] for name in self.blocked):
                raise RuntimeError("docker exec failed: exit 1")
            return ""
        if args[0] == "exec":
            return "-- PostgreSQL database cluster dump"
        if args[0] == "compose" and "up" in args and self.unhealthy:
            raise RuntimeError("docker compose failed: container relay is unhealthy")
        if args[:2] == ("inspect", "--format") and "Health" in args[2]:
            return json.dumps(
                {"Log": [{"Output": "check failed redis://:s3cretpassword@redis"}]}
            )
        if args[0] == "logs":
            return "line one\nERROR Redis pool timed out url=redis://:s3cretpassword@redis:6379"
        if args[:2] == ("volume", "inspect"):
            return json.dumps({LABEL: P, "com.docker.compose.project": P})
        if args[:2] == ("image", "inspect"):
            if args[-1] not in self.images_present:
                raise RuntimeError("docker image failed: No such image")
            return self.images_present[args[-1]]
        if args[0] == "images":
            return "\n".join(json.dumps(i) for i in self.image_list)
        if args[:2] == ("ps", "-aq"):
            return "c1 c2"
        if args[0] == "inspect":
            return "sha256:used"
        if args[0] == "info":
            return "/" if "DockerRootDir" in args[-1] else "27.3.1"
        if args[0] == "run" and "sh" in args:
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
            out = next(m.split(":")[0] for m in mounts if ":/out:" in m)
            with tarfile.open(out + "/profiles.tgz", "w:gz") as tar:
                tar.add(self.profiles, arcname="profiles")
        if args[0] == "run" and "tar" in args:
            # The helper writes into the mounted /backup directory.
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
            backup = next(m.split(":")[0] for m in mounts if ":/backup:" in m)
            out = next(a for a in args if a.startswith("/backup/"))
            with open(backup + "/" + out.removeprefix("/backup/"), "w") as f:
                f.write("archive")
        return ""


@pytest.fixture
def team(tmp_path):
    root = tmp_path / "state" / "default"
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {"project": P, "runtime_image": "sha256:abc", "advertised_url": "http://x"}
        )
    )
    (root / "compose.yaml").write_text(
        "services:\n  postgres: {restart: unless-stopped}\n  redis: {}\n  relay: {}\n"
        "  minio-init: {restart: 'no'}\n"
    )
    fake = FakeDocker()
    lines = []
    streamed = []
    item = Installation(
        root, run=fake, out=lines.append, stream=lambda *a: streamed.append(a) or 0
    )
    item.streamed = streamed
    return item, fake, lines


def test_inventory_classifies_roles_and_external_attachments(team):
    item, _, lines = team
    roles = {c["name"]: c["role"] for c in item.containers()}
    assert roles[f"{P}-agent-coa"] == "agent"
    assert roles[f"{P}-app-portal-staging-ui"] == "app"
    assert roles[f"{P}-manager-1"] == "service"
    assert item.external_attachments() == [(f"{P}-app-portal-staging", "db-uat-mysql")]
    item.status()
    assert any("external db-uat-mysql" in line for line in lines)


def test_stop_order_and_keep_apps(team):
    item, fake, _ = team
    item.stop()
    stops = [c[3] for c in fake.calls if c[0] == "stop"]
    assert stops == [
        f"{P}-app-portal-staging-ui",
        f"{P}-agent-coa",
    ]  # cody already stopped
    assert fake.calls[-1][-1] == "stop" and fake.calls[-1][0] == "compose"
    fake.calls.clear()
    item.stop(keep_apps=True)
    assert [c[3] for c in fake.calls if c[0] == "stop"] == [f"{P}-agent-coa"]


def test_start_brings_up_services_then_stopped_apps(team):
    item, fake, _ = team
    fake.containers[f"{P}-app-portal-staging-ui"] = (
        "exited",
        "io.team-builder.application=portal",
    )
    item.start()
    up = next(i for i, c in enumerate(fake.calls) if c[0] == "compose" and "up" in c)
    start = next(i for i, c in enumerate(fake.calls) if c[0] == "start")
    assert up < start and fake.calls[start][1] == f"{P}-app-portal-staging-ui"


def test_remove_disconnects_externals_first_and_removes_one_at_a_time(team):
    item, fake, _ = team
    item.remove()
    first_rm = next(i for i, c in enumerate(fake.calls) if c[0] == "rm")
    disconnect = next(
        i for i, c in enumerate(fake.calls) if c[:2] == ("network", "disconnect")
    )
    assert disconnect < first_rm
    assert fake.calls[disconnect][3] == "db-uat-mysql"
    down = next(
        i for i, c in enumerate(fake.calls) if c[0] == "compose" and "down" in c
    )
    assert all(
        i < down for i, c in enumerate(fake.calls) if c[0] == "rm" and "agent" in c[2]
    )
    assert not fake.containers and not fake.networks and not fake.volumes
    helper = [c for c in fake.calls if c[0] == "run"][-1]
    assert helper[-2:] == ("-rf", "/state-parent/default") and "sha256:abc" in helper


def test_remove_refuses_to_report_success_with_leftovers(team, monkeypatch):
    item, fake, _ = team
    real = fake.__call__

    def keep_volume(*args, **kw):
        if args[:2] == ("volume", "rm"):
            return ""
        return real(*args, **kw)

    item.run = keep_volume
    fake.volumes.append("tb-test-123-extra")
    with pytest.raises(RuntimeError, match="remain"):
        item.remove()


def test_backup_archives_state_database_and_volumes(team, tmp_path):
    item, _, _ = team
    target = item.backup(tmp_path / "out")
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["project"] == P
    assert set(manifest["files"]) == {
        "state.tgz",
        "community-postgres.sql",
        f"volume-{P}_postgres.tgz",
        f"volume-{P}_relay.tgz",
    }
    assert (target / "community-postgres.sql").read_text().startswith("-- PostgreSQL")
    assert oct(target.stat().st_mode)[-3:] == "700"
    assert all(
        oct((target / f).stat().st_mode)[-3:] == "600" for f in manifest["files"]
    )


def test_docker_timeouts_explain_a_stuck_daemon(monkeypatch):
    def hang(*a, **k):
        raise lifecycle.subprocess.TimeoutExpired(a[0], k["timeout"])

    monkeypatch.setattr(lifecycle.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="restart it"):
        lifecycle.docker("ps", timeout=5)


def test_remove_requires_typing_the_project_name(team, monkeypatch, capsys):
    from team_builder import cli

    item, _, _ = team
    monkeypatch.setattr(cli, "input", lambda prompt: "wrong", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "wrong")
    monkeypatch.setattr(lifecycle, "Installation", lambda state_dir: item)
    monkeypatch.setattr(
        sys, "argv", ["team-builder", "remove", "--state-dir", str(item.root)]
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "cancelled" in capsys.readouterr().err
    assert (item.root / "config.json").exists()


def test_logs_resolve_services_agents_and_containers(team):
    item, _, _ = team
    item.logs()
    item.logs("relay", follow=True, tail=50)
    item.logs("coa", since="10m")
    item.logs("portal-staging-ui")
    compose = item.compose
    assert item.streamed == [
        (*compose, "logs", "--tail", "200"),
        (*compose, "logs", "--tail", "50", "--follow", "relay"),
        ("logs", "--tail", "200", "--since", "10m", f"{P}-agent-coa"),
        ("logs", "--tail", "200", f"{P}-app-portal-staging-ui"),
    ]
    with pytest.raises(ValueError, match="Unknown or ambiguous"):
        item.logs("nope")


def test_agent_list_and_restart(team):
    item, fake, _ = team
    assert len(item.agent("list")) == 2
    item.agent("restart", "cody")
    assert fake.calls[-1] == ("restart", "-t", "20", f"{P}-agent-cody")
    item.agent("start", "cody")
    assert fake.calls[-1] == ("start", f"{P}-agent-cody")
    with pytest.raises(ValueError, match="No agent 'ghost'; agents: coa, cody"):
        item.agent("stop", "ghost")


def test_prune_keeps_pinned_used_and_foreign_images(team):
    item, fake, _ = team
    item.config["images"] = {"hermes": "ghcr.io/x/tb/hermes@sha256:new"}
    (item.root / "config.json").write_text(json.dumps(item.config))
    fake.images_present["ghcr.io/x/tb/hermes@sha256:new"] = "sha256:pinned"
    fake.containers[f"{P}-probe"] = (
        "exited",
        "io.team-builder.component=preflight,io.team-builder.application=portal",
    )

    def image(id, repo):
        return {"ID": id, "Repository": repo, "Size": "1GB", "CreatedSince": "2 weeks"}

    fake.image_list = [
        image("sha256:pinned", "ghcr.io/x/tb/hermes"),
        image("sha256:used", "ghcr.io/x/tb/hermes"),
        image("sha256:old", "ghcr.io/x/tb/hermes"),
        image("sha256:foreign", "postgres"),
    ]
    assert item.prune(dry_run=True) == []
    assert not [c for c in fake.calls if c[:2] == ("image", "rm")]
    assert item.prune(confirm=lambda prompt: False) == []
    assert item.prune() == [f"{P}-probe", "sha256:old"]
    assert ("image", "rm", "-f", "sha256:old") in fake.calls


def test_doctor_reports_failures(team):
    item, fake, _ = team
    (item.root / "secrets.json").write_text("{}")
    (item.root / "secrets.json").chmod(0o644)
    fake.networks[f"{P}-app-portal-production"] = ["db-uat-mysql"]
    results = {(level, check) for level, check, _ in item.doctor()}
    assert ("FAIL", "permissions") in results
    assert ("PASS", "service postgres") in results
    assert ("FAIL", "service redis") in results  # no container
    assert not any(check == "service minio-init" for _, check in results)
    assert ("WARN", "agent agent-cody") in results  # stopped
    assert ("FAIL", "isolation") in results
    assert ("WARN", "backup") in results


def test_doctor_stops_when_docker_is_stuck(team):
    item, _, _ = team

    def stuck(*args, **kw):
        raise RuntimeError("Docker did not answer within 15s")

    item.run = stuck
    assert item.doctor() == [("FAIL", "docker", "Docker did not answer within 15s")]


def test_backup_keep_removes_only_older_backups_of_this_team(team, tmp_path):
    item, _, _ = team
    out = tmp_path / "out"
    for name in (
        f"{P}-20260101T000000Z",
        f"{P}-20260201T000000Z",
        "other-20250101T000000Z",
    ):
        (out / name).mkdir(parents=True)
        (out / name / "manifest.json").write_text("{}")
    target = item.backup(out, keep=2)
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [f"{P}-20260201T000000Z", target.name, "other-20250101T000000Z"]
    )
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["host_root"] == str(item.root)
    assert manifest["volumes"][f"{P}_postgres"][LABEL] == P


def test_export_and_import_credentials(team, tmp_path):
    item, fake, _ = team
    tree = tmp_path / "profiles" / "portal" / "staging"
    (tree / "credentials").mkdir(parents=True)
    (tree / "credentials" / "users-yaml.json").write_text(
        json.dumps({"id": "users-yaml", "version": "b" * 32})
    )
    (tree / "versions" / "users-yaml").mkdir(parents=True)
    (tree / "versions" / "users-yaml" / ("a" * 32)).write_text("old")
    (tree / "versions" / "users-yaml" / ("b" * 32)).write_text("users:\n  song: {}\n")
    fake.profiles = tmp_path / "profiles"
    (item.root / "deployments").mkdir()
    output = tmp_path / "creds.json"
    bundle = item.export_credentials(output)
    current = {
        "application": "portal",
        "environment": "staging",
        "id": "users-yaml",
        "value": "users:\n  song: {}\n",
    }
    assert bundle["credentials"] == [current]
    assert oct(output.stat().st_mode)[-3:] == "600"
    with pytest.raises(ValueError, match="already exists"):
        item.export_credentials(output)

    sent = []

    def request(root, body):
        sent.append(body)
        if body["environment"] == "production":
            raise ValueError("Unknown application environment")
        return {"changed": True}

    data = json.loads(output.read_text())
    data["credentials"].append({**current, "environment": "production"})
    output.write_text(json.dumps(data))
    summary = item.import_credentials(output, rotate=True, request=request)
    assert summary == {"stored": 1, "unchanged": 0, "failed": 1}
    assert sent[0] == {"action": "credential", **current, "rotate": True}


def test_restore_checks_then_rebuilds_in_order(team, tmp_path):
    item, _, _ = team
    backup = item.backup(tmp_path / "out")
    calls = []

    def run(*args, timeout=120, input=None):
        calls.append((args, input))
        return ""

    with pytest.raises(ValueError, match="not empty"):
        restore(backup, run=run)
    with pytest.raises(ValueError, match="belongs at"):
        restore(backup, tmp_path / "elsewhere", run=run)
    item.remove()
    shutil.rmtree(item.root)  # the fake helper does not delete files
    calls.clear()
    restore(backup, run=run, out=lambda line: None)
    steps = [a for a, _ in calls]
    assert steps[3][0] == "run" and "/backup/state.tgz" in steps[3]
    created = [a[-1] for a in steps if a[:2] == ("volume", "create")]
    assert created == [f"{P}_postgres", f"{P}_relay"]
    untarred = [a for a in steps if a[0] == "run" and any("volume-" in x for x in a)]
    assert len(untarred) == 1 and f"/backup/volume-{P}_relay.tgz" in untarred[0]
    psql = next(i for i, (a, _) in enumerate(calls) if a[0] == "exec")
    assert calls[psql][1].startswith("-- PostgreSQL")
    assert steps[psql - 1][-1] == "postgres" and "up" in steps[-1]
    (backup / "state.tgz").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore(backup, run=run)


def test_failed_start_explains_unhealthy_services_and_blocked_network(team):
    item, fake, _ = team
    (item.root / "secrets.json").write_text(json.dumps({"redis": "s3cretpassword"}))
    fake.containers[f"{P}-relay-1"] = ("running", "")
    fake.unhealthy = True
    fake.blocked = {"redis"}
    original = fake.containers.copy()

    def ps_status(*args, **kw):
        out = fake(*args, **kw)
        if args[:2] == ("ps", "-a") and "--format" in args:
            out = out.replace(
                f'"Names": "{P}-relay-1", "State": "running", "Status": "running"',
                f'"Names": "{P}-relay-1", "State": "running", "Status": "Up 9 minutes (unhealthy)"',
            )
        return out

    item.run = ps_status
    with pytest.raises(RuntimeError) as failure:
        item.compose_up("relay", wait=240)
    message = str(failure.value)
    assert message.startswith(
        "Starting relay failed (docker compose failed: container relay is unhealthy)"
    )
    assert f"- {P}-relay-1: Up 9 minutes (unhealthy)" in message
    assert "last health check: check failed redis://:***@redis" in message
    assert "log: ERROR Redis pool timed out" in message
    assert "s3cretpassword" not in message
    assert "cannot open a TCP connection to redis:6379" in message
    assert "sudo iptables -t raw -S PREROUTING" in message
    assert fake.containers == original


def test_doctor_checks_relay_network_paths(team):
    item, fake, _ = team
    fake.blocked = {"minio"}
    results = {check: level for level, check, _ in item.doctor()}
    assert results["network relay → postgres"] == "PASS"
    assert results["network relay → redis"] == "PASS"
    assert results["network relay → minio"] == "FAIL"
