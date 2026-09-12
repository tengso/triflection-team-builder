"""Exercise an already initialized, isolated Linux test community.

Run with --state-dir and a generated --owner-key-file. Never point this at a
community containing real conversations: this script posts test messages and
creates/archives a test agent. It does not read any deployment credentials other
than those in the explicitly selected test state directory.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

from team_builder.buzz import Buzz
from team_builder.nostr import parse_key, public, reply_tags, tags, verify


def wait_for(label, function, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            print("Verified:", label, flush=True)
            return value
        time.sleep(3)
    raise RuntimeError("Timed out: " + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--owner-key-file", required=True)
    args = parser.parse_args()
    root = Path(args.state_dir)
    config = json.loads((root / "config.json").read_text())
    secrets = json.loads((root / "secrets.json").read_text())
    owner_key = parse_key(Path(args.owner_key_file).read_text())
    assert public(owner_key) == config["owner"]
    assert "Smoke Test" in config["name"], "Refusing a non-test community"
    assert owner_key not in (root / "config.json").read_text()
    assert owner_key not in (root / "secrets.json").read_text()
    buzz = Buzz(
        f"http://127.0.0.1:{config['port']}",
        owner_key,
        config["relay"],
        canonical_origin=config["advertised_url"],
    )
    container = json.loads(
        subprocess.check_output(["docker", "inspect", config["project"] + "-manager-1"])
    )[0]
    ip = container["NetworkSettings"]["Networks"][config["project"] + "_community"][
        "IPAddress"
    ]
    client = httpx.Client(
        base_url=f"http://{ip}:8088",
        headers={"Authorization": "Bearer " + secrets["token"]},
        timeout=600,
        trust_env=False,
    )

    def rpc(path, data):
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    def messages(author, channel, since):
        events = buzz.query(
            [
                {
                    "kinds": [9],
                    "authors": [author],
                    "#h": [channel],
                    "since": since,
                    "limit": 100,
                }
            ]
        )
        return [
            e
            for e in events
            if e["pubkey"] == author and tags(e, "h") == [[channel]] and verify(e)
        ]

    coa = public(secrets["coa"])
    office = config["office"]

    def proposal():
        # Resume a proposal already posted before an interrupted test run.
        entries = messages(coa, office, 0)
        return next(
            (
                e
                for e in entries
                if e["content"].startswith("Proposed team changes:")
                and "smoke-engineer" in e["content"]
            ),
            None,
        )

    if not proposal():
        request = buzz.event(
            9,
            [["h", office]],
            "Please propose these exact team changes using propose_changes, without executing yet: "
            "create a private channel with id smoke-dev and name Smoke Development; "
            "create an agent with id smoke-engineer and name Smoke Engineer, assigned only to smoke-dev. "
            "Its instructions: Reply concisely when mentioned; when asked for the smoke test phrase, reply COA_SMOKE_OK. "
            "Use the default model. Post the proposal now; these details are sufficient.",
        )
        print("Posted isolated owner request", request["id"], flush=True)
    proposed = wait_for("COA posted an executable proposal through MCP", proposal)
    assert not any(a["id"] == "smoke-engineer" for a in rpc("/inspect", {})["agents"])
    approval = buzz.event(
        9,
        [["h", office], *reply_tags(proposed)],
        "approve",
    )
    print("Approved exact proposal", approval["id"], flush=True)

    def ready_agent():
        return next(
            (
                a
                for a in rpc("/inspect", {})["agents"]
                if a["id"] == "smoke-engineer" and a["gateway_ready"]
            ),
            None,
        )

    agent = wait_for("owner-approved agent is running", ready_agent, timeout=360)
    profile = buzz.head(0, agent["pubkey"])
    registration = buzz.head(30177, coa, agent["pubkey"])
    assert tags(profile, "auth")[0][0] == coa
    assert json.loads(registration["content"])["parallelism"] == 1
    dev = next(c for c in rpc("/inspect", {})["channels"] if c["id"] == "smoke-dev")[
        "uuid"
    ]
    assert buzz.channel(dev)["roles"][agent["pubkey"]] == "bot"
    assert agent["pubkey"] not in buzz.channel(office)["roles"]
    started = int(time.time())
    buzz.event(
        9,
        [["h", dev], ["p", agent["pubkey"]]],
        "@Smoke Engineer Please reply with the smoke test phrase.",
    )
    wait_for(
        "created agent replies in its assigned Buzz channel",
        lambda: any(
            "COA_SMOKE_OK" in e["content"]
            for e in messages(agent["pubkey"], dev, started)
        ),
    )

    # Test lifecycle through the same service with actual signed owner messages.
    for index, operation in enumerate(
        [
            {"action": "add_member", "channel": "office", "agent": "smoke-engineer"},
            {
                "action": "update_agent",
                "id": "smoke-engineer",
                "name": "Smoke Engineer Updated",
            },
            {"action": "stop_agent", "id": "smoke-engineer"},
            {"action": "start_agent", "id": "smoke-engineer"},
            {"action": "remove_member", "channel": "office", "agent": "smoke-engineer"},
            {"action": "archive_agent", "id": "smoke-engineer"},
        ]
    ):
        # A p-tag for the human avoids requesting a second simultaneous action from COA;
        # service execution remains authorized by the signed owner request.
        event = buzz.event(
            9,
            [["h", office]],
            f"Integration harness operation {index}; no action needed from COA: "
            + json.dumps(operation),
        )
        result = rpc(
            "/execute", {"source_event_id": event["id"], "operations": [operation]}
        )
        assert result["state"] == "complete", result
        assert (
            rpc("/execute", {"source_event_id": event["id"], "operations": [operation]})
            == result
        )
        print("Verified:", operation["action"], flush=True)
    final = next(
        a for a in rpc("/inspect", {})["agents"] if a["id"] == "smoke-engineer"
    )
    assert final["state"] == "archived" and not final["gateway_ready"]
    assert (root / "agents/smoke-engineer/work").is_dir()
    print("PASS: isolated owner → COA → agent conversation and lifecycle", flush=True)


if __name__ == "__main__":
    main()
