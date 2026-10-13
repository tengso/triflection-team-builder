"""Mission Control embedded in a web Buzz build: proxy paths and owner-key sign-in."""

import time

from test_dashboard_profiles import MANAGE, SECRET, dashboard, post  # noqa: F401

from team_builder.dashboard import PREFIX, same_origin
from team_builder.dashboard_access import OWNER_AUDIENCE, OWNER_KIND
from team_builder.nostr import key, sign

SAME = {"Sec-Fetch-Site": "same-origin"}


def owner_event(client, owner_secret, purpose="login", **change):
    challenge = client.post(
        PREFIX + "/challenge", json={"purpose": purpose}, headers=SAME
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    assert (body["kind"], body["audience"]) == (OWNER_KIND, OWNER_AUDIENCE)
    tags = change.pop(
        "tags",
        [
            ["u", OWNER_AUDIENCE],
            ["method", "POST"],
            ["challenge", body["challenge"]],
        ],
    )
    return sign(
        change.pop("signer", owner_secret),
        change.pop("kind", OWNER_KIND),
        tags,
        "Sign in to Mission Control",
        change.pop("timestamp", int(time.time())),
    )


def login(client, event):
    return client.post(PREFIX + "/login", json={"event": event}, headers=SAME)


def test_owner_key_signs_in_once_per_challenge(dashboard, manager):  # noqa: F811
    client, *_ = dashboard
    event = owner_event(client, manager.owner_secret)
    response = login(client, event)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Path=/dashboard;" not in cookie.split(",")[0]  # scoped by the browser
    assert client.get(PREFIX + "/overview").status_code == 200
    client.cookies.clear()
    replay = login(client, event)
    assert replay.status_code == 401
    assert replay.json()["error"] == "Not signed by this community's owner"


def test_owner_sign_in_rejects_other_keys_and_altered_events(dashboard, manager):  # noqa: F811
    client, *_ = dashboard
    secret = manager.owner_secret
    for change in (
        {"signer": key()},  # a member, not the owner
        {"kind": 22242},
        {"timestamp": int(time.time()) - 600},
        {"tags": [["u", "relay"], ["challenge", "0" * 64]]},
    ):
        assert login(client, owner_event(client, secret, **change)).status_code == 401
    tampered = owner_event(client, secret)
    tampered["content"] = "changed"
    assert login(client, tampered).status_code == 401
    # A challenge issued for a reveal cannot sign in.
    assert (
        client.post(
            PREFIX + "/challenge", json={"purpose": "reveal"}, headers=SAME
        ).status_code
        == 401
    )


def test_owner_key_confirms_a_reveal(dashboard, manager):  # noqa: F811
    client, origin, _, _ = dashboard
    assert login(client, owner_event(client, manager.owner_secret)).status_code == 200
    post(client, origin, {"action": "credential", "id": "api-key", "value": SECRET})
    login_challenge = owner_event(client, manager.owner_secret, purpose="login")
    wrong_purpose = post(
        client, origin, {"action": "reveal", "id": "api-key", "event": login_challenge}
    )
    assert wrong_purpose.status_code == 400
    assert "owner" in wrong_purpose.json()["error"]
    event = owner_event(client, manager.owner_secret, purpose="reveal")
    shown = post(client, origin, {"action": "reveal", "id": "api-key", "event": event})
    assert shown.status_code == 200 and shown.json()["value"] == SECRET
    again = post(client, origin, {"action": "reveal", "id": "api-key", "event": event})
    assert again.status_code == 400


def test_proxy_paths_framing_and_same_origin(dashboard):  # noqa: F811
    client, *_ = dashboard
    page = client.get("/dashboard/")
    assert 'href="dashboard.css"' in page.text and 'src="dashboard.js"' in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    redirect = client.get("/dashboard", follow_redirects=False)
    assert redirect.headers["location"] == "dashboard/"
    assert same_origin({"Sec-Fetch-Site": "same-origin"})
    assert not same_origin({"Sec-Fetch-Site": "cross-site", "Origin": "http://h"})
    # A TLS-terminating gateway: https Origin, proxied Host.
    assert same_origin({"Origin": "https://gw.example", "Host": "gw.example"})
    assert not same_origin({"Origin": "https://evil.example", "Host": "gw.example"})
    assert not same_origin({"Host": "gw.example"})
    cross = client.post(
        PREFIX + "/challenge",
        json={"purpose": "login"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403
