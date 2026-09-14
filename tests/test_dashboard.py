import argparse
import hashlib
import json
import socket
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
import yaml
from conftest import message

from team_builder.dashboard import PREFIX, serve
from team_builder.dashboard_access import Access, command, provision
from team_builder.dashboard_observe import Engine, Observer, Redactor, gateway
from team_builder.storage import private_write


class EngineStub:
    def sample(self, name):
        return {
            "status": "exited" if name.endswith("minio-init-1") else "running",
            "health": "healthy",
            "exit_code": 0,
            "cpu_percent": 1,
            "memory_bytes": 100,
            "collected_at": time.time(),
        }

    def logs(self, name):
        return [{"level": "info", "message": "Service ready", "timestamp": None}]


@pytest.fixture
def observer(manager):
    item = Observer(manager, engine=EngineStub(), buzz=manager.buzz)
    item.collect()
    return item


def test_snapshot_read_only_and_sanitized(manager, observer):
    before = manager.registry.db.iterdump()
    before = list(before)
    observer.collect()
    value = observer.read()
    assert value["services"][-1]["status"] == "completed"
    assert value["agents"][0]["gateway"]["responsive"] is False
    assert value["agents"][0]["gateway"]["buzz"] == "unknown"
    assert value["channels"][0]["buzz"]["status"] == "present"
    assert value["channels"][0]["buzz"]["members"]
    assert value["agents"][0]["owner"] == manager.config["owner"]
    for secret in manager.secrets.values():
        assert secret not in json.dumps(value)
    assert list(manager.registry.db.iterdump()) == before
    assert len(value["history"]) == 2
    with pytest.raises(ValueError):
        observer.logs("../../other")
    with pytest.raises(ValueError):
        observer.logs("agent:unmanaged")


def test_auth_expiry_rotation_and_throttle(tmp_path):
    now = [1000]
    key = provision(tmp_path)
    assert key not in (tmp_path / "dashboard-auth.json").read_text()
    access = Access(tmp_path, clock=lambda: now[0])
    status, session = access.login(key, "owner")
    assert status == 200 and access.valid(session)
    now[0] += 28801
    assert not access.valid(session)
    _, session = access.login(key, "owner")
    replacement = provision(tmp_path)
    assert not access.valid(session)
    assert access.login(key, "old")[0] == 401
    assert access.login(replacement, "new")[0] == 200
    for _ in range(5):
        assert access.login("wrong", "attacker")[0] == 401
    assert access.login(replacement, "attacker")[0] == 429
    now[0] += 61
    assert access.login(replacement, "attacker")[0] == 200


def test_http_boundary(observer):
    key = provision(observer.root)
    server = serve(observer, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        with httpx.Client(base_url=origin, trust_env=False) as client:
            assert client.get("/dashboard/").status_code == 200
            assert client.get(PREFIX + "/overview").status_code == 401
            assert (
                client.get(
                    PREFIX + "/overview",
                    headers={
                        "Authorization": "Bearer " + observer.manager.secrets["token"]
                    },
                ).status_code
                == 401
            )
            assert (
                client.post(
                    PREFIX + "/login",
                    json={"key": key},
                    headers={"Origin": "http://evil"},
                ).status_code
                == 403
            )
            result = client.post(
                PREFIX + "/login", json={"key": key}, headers={"Origin": origin}
            )
            assert result.status_code == 200
            assert "HttpOnly" in result.headers["set-cookie"]
            assert "SameSite=Strict" in result.headers["set-cookie"]
            for path in (
                "overview",
                "agents",
                "services",
                "channels",
                "projects",
                "activity",
            ):
                assert client.get(PREFIX + "/" + path).status_code == 200
            for path in (
                "/execute",
                "/operator/github-access",
                PREFIX + "/execute",
                PREFIX + "/restart",
            ):
                assert (
                    client.post(path, json={}, headers={"Origin": origin}).status_code
                    == 405
                )
            assert client.delete(PREFIX + "/agents/coa").status_code == 405
            assert client.get(PREFIX + "/logs?component=relay").status_code == 200
            assert client.get(PREFIX + "/logs?component=other").status_code == 400
            assert client.get("/dashboard/../secrets.json").status_code in (401, 404)
            assert (
                client.post(
                    PREFIX + "/logout", json={}, headers={"Origin": origin}
                ).status_code
                == 200
            )
            assert client.get(PREFIX + "/overview").status_code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_engine_cross_installation_and_log_suppression():
    engine = Engine("mine")
    calls = []

    def transport(request):
        calls.append(request.method)
        if request.url.path.endswith("/json"):
            return httpx.Response(
                200,
                json={
                    "Id": "abc",
                    "Config": {"Labels": {"io.team-builder.project": "other"}},
                },
            )
        raise AssertionError("Must not read another installation logs/stats")

    engine.client.close()
    engine.client = httpx.Client(
        transport=httpx.MockTransport(transport), base_url="http://docker"
    )
    with pytest.raises(ValueError):
        engine.logs("other")
    assert calls == ["GET"]

    def transport(request):
        if request.url.path.endswith("/json"):
            return httpx.Response(
                200,
                json={
                    "Id": "abc",
                    "Config": {
                        "Labels": {"io.team-builder.project": "mine"},
                        "Tty": True,
                    },
                },
            )
        return httpx.Response(
            200,
            content=b"2026-09-14T00:00:00Z Buzz WebSocket disconnected TOKEN=private\nSECRET COMMAND curl secret\n",
        )

    engine.client = httpx.Client(
        transport=httpx.MockTransport(transport), base_url="http://docker"
    )
    assert "private" not in json.dumps(engine.logs("ours"))
    assert "curl" not in json.dumps(engine.logs("ours"))
    assert len(engine.logs("ours")) == 1


def test_gateway_live_witness_and_stopped_archive(tmp_path):
    # Short socket path is necessary on macOS and Linux.
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="tb-gw-") as directory:
        from pathlib import Path

        root = Path(directory).resolve()
        home = root / "agents/a/home/.hermes"
        (home / "state").mkdir(parents=True)
        state = {
            "pid": 7,
            "start_time": 12,
            "updated_at": "2020-01-01T00:00:00+00:00",
            "gateway_state": "running",
            "active_agents": 0,
            "platforms": {
                "buzz": {"state": "connected", "writer_pid": 7, "writer_start_time": 12}
            },
        }
        private_write(home / "gateway_state.json", state)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(home / "state/gateway.loop-tick.7.sock"))
        listener.listen()

        def respond():
            conn, _ = listener.accept()
            conn.sendall(b"1")
            conn.close()

        thread = threading.Thread(target=respond)
        thread.start()
        current = gateway(root, "a", {"status": "running"})
        thread.join()
        listener.close()
        assert current["responsive"] is True and current["buzz"] == "connected"
        assert current["stale"] is False  # state-change timestamp is not heartbeat
        current = gateway(root, "a", {"status": "running"})
        assert current["responsive"] is False and current["buzz"] == "unknown"
        for status in ("stopped", "exited", "missing"):
            assert gateway(root, "a", {"status": status})["buzz"] == "unknown"
        assert gateway(
            root,
            "a",
            {"status": "running", "started_at": datetime.now(UTC).isoformat()},
        )["stale"]


def test_registry_failure_preserves_stale_snapshot(observer, monkeypatch):
    import sqlite3

    old = observer.read()

    def unavailable():
        raise sqlite3.OperationalError("busy")

    monkeypatch.setattr(observer, "registry", unavailable)
    observer.snapshot["collected_at"] = time.time() - 20
    observer.collect()
    value = observer.read()
    assert value["stale"] and value["errors"]
    assert value["agents"] == old["agents"]


def test_proposals_and_outcomes(manager, observer):
    source = message(manager)
    manager.propose(source, [{"action": "create_channel", "id": "new", "name": "New"}])
    manager.execute(source, [{"action": "create_channel", "id": "new", "name": "New"}])
    observer.collect()
    activity = observer.read()["activity"]
    assert activity["proposals"][0]["actions"] == ["create_channel"]
    assert activity["operations"][0]["state"] == "done"


def test_enable_disable_does_not_expose_mcp(manager, monkeypatch, capsys):
    root = manager.root
    config = json.loads((root / "config.json").read_text())
    config.update(bind="0.0.0.0", port=3100)
    private_write(root / "config.json", config)
    private_write(
        root / "compose.yaml",
        yaml.safe_dump(
            {
                "services": {
                    "manager": {"image": "test"},
                    "relay": {"ports": ["3100:3000"]},
                }
            }
        ).encode(),
    )
    calls = []
    monkeypatch.setattr("team_builder.cli.run", lambda args, **kw: calls.append(args))
    args = argparse.Namespace(
        state_dir=str(root),
        dashboard_command="enable",
        bind=None,
        port=None,
        key_output=str(root / "delivery"),
    )
    command(args)
    document = yaml.safe_load((root / "compose.yaml").read_text())
    assert document["services"]["manager"]["ports"] == ["0.0.0.0:3101:8089"]
    assert "8088" not in json.dumps(document)
    assert (root / "delivery").stat().st_mode & 0o777 == 0o600
    verifier = json.loads((root / "dashboard-auth.json").read_text())["sha256"]
    assert (
        verifier
        == hashlib.sha256((root / "delivery").read_text().strip().encode()).hexdigest()
    )
    assert (root / "delivery").read_text().strip() not in capsys.readouterr().out
    args.dashboard_command = "disable"
    command(args)
    assert (
        "ports"
        not in yaml.safe_load((root / "compose.yaml").read_text())["services"][
            "manager"
        ]
    )
    assert all(call[-1] == "manager" for call in calls)


def test_redact_role_text_and_named_credentials(manager):
    secret = manager.secrets["coa"]
    value = Redactor(manager.root).clean(
        {"instructions": f"never expose {secret} or github_pat_dummy"}
    )
    assert secret not in json.dumps(value) and "github_pat_dummy" not in json.dumps(
        value
    )


def test_partial_sources_and_manager_lock(observer, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("Buzz offline")

    monkeypatch.setattr(observer.buzz, "head", unavailable)
    monkeypatch.setattr(observer.buzz, "channel", unavailable)
    monkeypatch.setattr(observer.buzz, "roster", unavailable)
    observer.manager.lock.acquire()
    try:
        worker = threading.Thread(target=observer.collect)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive(), (
            "Diagnostic collection must not acquire manager mutation lock"
        )
        value = observer.read()
        assert value["channels"][0]["buzz"]["status"] == "unavailable"
        assert value["community"]["membership"]["status"] == "unavailable"
        assert value["services"][0]["status"] == "running"
    finally:
        observer.manager.lock.release()


def test_cpu_uses_successive_samples_and_missing_engine():
    engine = Engine("mine")
    tick = [0]

    def transport(request):
        assert request.method == "GET"
        if request.url.path.endswith("/json"):
            return httpx.Response(
                200,
                json={
                    "Id": "abc",
                    "Config": {"Labels": {"io.team-builder.project": "mine"}},
                    "State": {"Running": True, "Status": "running"},
                },
            )
        tick[0] += 1
        return httpx.Response(
            200,
            json={
                "cpu_stats": {
                    "cpu_usage": {"total_usage": tick[0] * 20},
                    "system_cpu_usage": tick[0] * 100,
                    "online_cpus": 2,
                },
                "memory_stats": {"usage": 100},
            },
        )

    engine.client.close()
    engine.client = httpx.Client(
        transport=httpx.MockTransport(transport), base_url="http://docker"
    )
    assert engine.sample("ours")["cpu_percent"] is None
    assert engine.sample("ours")["cpu_percent"] == 40
    engine.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        base_url="http://docker",
    )
    assert engine.sample("ours")["status"] == "missing"


def test_approved_proposal_status_and_no_message_content(manager, observer):
    from team_builder.nostr import sign

    source = message(manager)
    proposal = manager.propose(
        source, [{"action": "create_channel", "id": "approved", "name": "Approved"}]
    )
    event = sign(
        manager.owner_secret,
        9,
        [["h", manager.config["office"]], ["e", proposal["message_id"], "", "reply"]],
        "approve",
    )
    manager.buzz.events[event["id"]] = event
    manager.approve(approval_event_id=event["id"])
    observer.collect()
    value = observer.read()
    assert value["activity"]["proposals"][0]["status"] == "completed"
    assert "content" not in json.dumps(value)


def test_credentials_never_enter_compose_on_rotation(manager, monkeypatch, tmp_path):
    from team_builder.dashboard_access import provision

    delivery = tmp_path / "one-time.key"
    key = provision(manager.root, delivery)
    assert key not in (manager.root / "dashboard-auth.json").read_text()
    with pytest.raises(ValueError):
        provision(manager.root, delivery)
