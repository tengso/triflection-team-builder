import json
import threading

import httpx
import pytest
from test_dashboard import EngineStub
from test_release_automation import prepare

from team_builder.dashboard import PREFIX, serve
from team_builder.dashboard_access import provision
from team_builder.dashboard_observe import Observer

MANAGE = PREFIX + "/deployments/manage"
SECRET = "stored-private-value"


@pytest.fixture
def dashboard(manager):
    d, _ = prepare(manager)
    observer = Observer(manager, engine=EngineStub(), buzz=manager.buzz)
    observer.collect()
    key = provision(observer.root)
    server = serve(observer, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=origin, trust_env=False) as client:
        yield client, origin, key, d
    server.shutdown()
    server.server_close()


def post(client, origin, body, environment="staging"):
    return client.post(
        MANAGE,
        json={"application": "portal", "environment": environment, **body},
        headers={"Origin": origin},
    )


def sign_in(client, origin, key):
    assert (
        client.post(
            PREFIX + "/login", json={"key": key}, headers={"Origin": origin}
        ).status_code
        == 200
    )


def test_management_requires_owner_session_and_same_origin(dashboard):
    client, origin, key, _ = dashboard
    view = MANAGE + "?application=portal&environment=staging"
    assert client.get(view).status_code == 401
    assert (
        post(client, origin, {"action": "preflight", "profile": "standard"}).status_code
        == 401
    )
    sign_in(client, origin, key)
    assert (
        client.post(
            MANAGE,
            json={
                "action": "preflight",
                "application": "portal",
                "environment": "staging",
            },
            headers={"Origin": "http://evil"},
        ).status_code
        == 403
    )
    assert client.get(view).status_code == 200


def test_profiles_and_secrets_lifecycle_with_audit(dashboard):
    client, origin, key, d = dashboard
    sign_in(client, origin, key)
    stored = post(
        client, origin, {"action": "credential", "id": "api-key", "value": SECRET}
    )
    assert stored.status_code == 200 and stored.json()["changed"] is True
    # Replacing needs an explicit rotate; the manager's reason is shown.
    refused = post(
        client, origin, {"action": "credential", "id": "api-key", "value": "other"}
    )
    assert refused.status_code == 400 and "rotate" in refused.json()["error"]
    rotated = post(
        client,
        origin,
        {"action": "credential", "id": "api-key", "value": "v2", "rotate": True},
    )
    assert rotated.json()["changed"] is True
    generated = post(
        client, origin, {"action": "credential", "id": "api-token", "generate": True}
    )
    assert generated.json()["changed"] is True

    profile = {
        "id": "standard-v2",
        "values": {"BACKEND": "rest"},
        "secrets": {"API_KEY": "api-key", "TOKEN": "missing-ref"},
    }
    saved = post(client, origin, {"action": "profile", "profile": profile})
    assert saved.status_code == 200 and saved.json()["id"] == "standard-v2"
    again = post(client, origin, {"action": "profile", "profile": profile})
    assert again.status_code == 400 and "immutable" in again.json()["error"]
    invalid = post(
        client,
        origin,
        {"action": "profile", "profile": {"id": "x", "values": {"bad": 1}}},
    )
    assert invalid.status_code == 400 and invalid.json()["error"].startswith(
        "Invalid request"
    )

    view = client.get(MANAGE + "?application=portal&environment=staging").json()
    listed = {p["id"]: p for p in view["profiles"]}
    assert listed["standard-v2"]["values"] == {"BACKEND": "rest"}
    credentials = {c["id"]: c for c in view["credentials"]}
    assert credentials["api-key"]["version"] and credentials["api-key"]["used_by"] == [
        "standard-v2"
    ]
    assert credentials["missing-ref"]["version"] is None  # referenced, not stored
    assert view["policy"]["profile"] == "standard"
    page = json.dumps(view)
    assert SECRET not in page and 'v2"' not in page.replace('standard-v2"', "")

    preflight = post(
        client, origin, {"action": "preflight", "profile": "standard-v2"}
    ).json()
    assert preflight["ready"] is False  # missing-ref is not stored

    actions = [a["action"] for a in view["audit"]]
    assert actions.count("credential") == 4 and "profile" in actions
    audit = (d.root / "dashboard-audit.jsonl").read_text()
    assert SECRET not in audit and '"value"' not in audit
    assert '"outcome": "refused"' in audit


def test_reveal_requires_the_access_key_and_is_throttled(dashboard):
    client, origin, key, d = dashboard
    sign_in(client, origin, key)
    post(client, origin, {"action": "credential", "id": "api-key", "value": SECRET})
    wrong = post(client, origin, {"action": "reveal", "id": "api-key", "key": "wrong"})
    assert wrong.status_code == 400 and "incorrect" in wrong.json()["error"]
    shown = post(client, origin, {"action": "reveal", "id": "api-key", "key": key})
    assert shown.status_code == 200 and shown.json()["value"] == SECRET
    missing = post(
        client, origin, {"action": "reveal", "id": "nothing-here", "key": key}
    )
    assert missing.status_code == 400
    for _ in range(4):
        post(client, origin, {"action": "reveal", "id": "api-key", "key": "wrong"})
    throttled = post(client, origin, {"action": "reveal", "id": "api-key", "key": key})
    assert "Too many attempts" in throttled.json()["error"]
    audit = (d.root / "dashboard-audit.jsonl").read_text()
    assert SECRET not in audit and audit.count('"action": "reveal"') >= 2


def test_release_profile_switch_refuses_stale_pages(dashboard):
    client, origin, key, d = dashboard
    sign_in(client, origin, key)
    d.profiles.register(
        "portal", "staging", {"id": "standard-v2", "values": {"BACKEND": "db"}}
    )
    version = d.db.get("automation-policy/portal")["version"]
    stale = post(
        client,
        origin,
        {
            "action": "policy-profile",
            "profile": "standard-v2",
            "expected_version": "old",
        },
    )
    assert stale.status_code == 400 and "reload" in stale.json()["error"]
    unknown = post(
        client,
        origin,
        {"action": "policy-profile", "profile": "nope", "expected_version": version},
    )
    assert unknown.status_code == 400
    switched = post(
        client,
        origin,
        {
            "action": "policy-profile",
            "profile": "standard-v2",
            "expected_version": version,
        },
    )
    assert switched.status_code == 200 and switched.json()["profile"] == "standard-v2"
    live = d.db.get("automation-policy/portal")
    assert (
        live["staging_profile"] == "standard-v2"
        and live["production_profile"] == "standard"
    )


def test_redeploy_current_release_with_a_profile(dashboard):
    client, origin, key, d = dashboard
    sign_in(client, origin, key)
    nothing = post(client, origin, {"action": "redeploy", "profile": "standard"})
    assert (
        nothing.status_code == 400 and "Nothing is deployed" in nothing.json()["error"]
    )
    app = d.get("app/portal/staging")
    d.put("app/portal/staging", "application", {**app, "current": "ci-20-1"})
    queued = post(client, origin, {"action": "redeploy", "profile": "standard"})
    assert queued.status_code == 200
    job = d.get("job/" + queued.json()["job"])
    assert job["state"] == "queued" and job["source"] == "dashboard"
    assert job["release"] == "ci-20-1" and job["configuration"]["profile"] == "standard"
    # A double click maps to the same plan, so it never queues a second deployment.
    again = post(client, origin, {"action": "redeploy", "profile": "standard"})
    assert again.json()["job"] == queued.json()["job"]
    assert len([j for j in d.db.list("job") if j["environment"] == "staging"]) == 1
