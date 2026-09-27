"""Run inside the manager of a disposable Image Smoke Test installation only."""

import json
import os
import time
from pathlib import Path

from team_builder.manager import Manager
from team_builder.nostr import reply_tags, sign, tags
from team_builder.runtime import HARNESSES, python_for

TURN_TIMEOUT = 600


def exercise(owner_secret):
    m = Manager("/state")
    assert m.config["name"] == "Image Smoke Test"
    owner = m.actor(owner_secret)
    counter = 0

    def execute(ops):
        nonlocal counter
        counter += 1
        source = sign(
            owner_secret,
            9,
            [["h", m.config["office"]]],
            f"Test {counter}: {json.dumps(ops)}",
        )
        owner.publish(source)
        result = m.execute(source["id"], ops)
        assert result["state"] == "complete", result
        return result

    harnesses = sorted(set(m.config.get("images", {})) & set(HARNESSES))
    if not harnesses:
        print("No harness images configured; skipping harness checks")
        return
    devin_credential = os.environ.get("TEAM_BUILDER_DEVIN_CREDENTIAL")
    if "devin" in harnesses and not devin_credential:
        harnesses.remove("devin")
        print("TEAM_BUILDER_DEVIN_CREDENTIAL unset; skipping devin")
    expect_failure = os.environ.get("HARNESS_EXPECT_FAILURE") == "1"

    execute([{"action": "create_channel", "id": "harness-lab", "name": "Lab"}])
    channel = m.resource("harness-lab", "channel")["uuid"]

    def wait_ready(agent, timeout=600):
        name = m.name(agent)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if m.docker.ready(name):
                return
            time.sleep(5)
        raise RuntimeError(f"Harness container never became healthy: {name}")

    for harness in harnesses:
        agent_id = f"harness-{harness}"
        op = {
            "action": "create_agent",
            "id": agent_id,
            "name": f"Harness {harness}",
            "instructions": "Reply concisely when mentioned.",
            "channels": ["harness-lab"],
            "harness": harness,
        }
        if harness == "devin":
            op["harness_credential"] = devin_credential
        execute([op])
        agent = m.resource(agent_id, "agent")
        wait_ready(agent)
        home = Path(f"/state/agents/{agent_id}/home/{HARNESSES[harness]['state']}")
        deadline = time.monotonic() + 120
        state = {}
        while time.monotonic() < deadline:
            state = json.loads((home / "gateway_state.json").read_text())
            if state["platforms"]["buzz"]["state"] == "connected":
                break
            time.sleep(5)
        assert state["platforms"]["buzz"]["state"] == "connected", state
        trigger = owner.publish(
            sign(
                owner_secret,
                9,
                [["h", channel], ["p", agent["pubkey"]]],
                f"@{agent['name']} please confirm you are online.",
            )
        )
        deadline = time.monotonic() + TURN_TIMEOUT
        reply = None
        while time.monotonic() < deadline and reply is None:
            events = m.buzz.query(
                [{"kinds": [9, 45001, 45003], "#h": [channel], "limit": 100}]
            )
            reply = next(
                (
                    e
                    for e in events
                    if e["pubkey"] == agent["pubkey"]
                    and trigger["id"] in [t[0] for t in tags(e, "e")]
                ),
                None,
            )
            time.sleep(10)
        assert reply, f"{harness} agent produced no reply"
        print(f"{harness} reply: {reply['content'][:300]!r}")
        if expect_failure:
            assert reply["content"].startswith("Turn failed:"), reply["content"]
        else:
            assert not reply["content"].startswith("Turn failed:"), reply["content"]
        m.docker.restart(m.name(agent), python_for(agent))
        wait_ready(agent)
        deadline = time.monotonic() + 120
        state = {}
        while time.monotonic() < deadline:
            state = json.loads((home / "gateway_state.json").read_text())
            if state["platforms"]["buzz"]["state"] == "connected":
                break
            time.sleep(5)
        assert state["platforms"]["buzz"]["state"] == "connected", state
        print(f"PASS: {harness} agent answered and recovered from restart")
    if not expect_failure:
        # COA's rules.toml allows unmentioned owner messages in the office.
        coa = m.resource("coa", "agent")
        names = ", ".join(f"Harness {h}" for h in harnesses)
        trigger = owner.publish(
            sign(
                owner_secret,
                9,
                [["h", m.config["office"]]],
                "Which agents are on the team right now? Reply with their names.",
            )
        )
        deadline = time.monotonic() + TURN_TIMEOUT
        reply = None
        while time.monotonic() < deadline and reply is None:
            events = m.buzz.query(
                [{"kinds": [9], "#h": [m.config["office"]], "limit": 100}]
            )
            reply = next(
                (
                    e
                    for e in events
                    if e["pubkey"] == coa["pubkey"]
                    and trigger["id"] in [t[0] for t in tags(e, "e")]
                ),
                None,
            )
            time.sleep(10)
        assert reply, "COA produced no reply to unmentioned office message"
        print(f"coa reply: {reply['content'][:300]!r}")
        assert any(name in reply["content"] for name in names.split(", ")), reply[
            "content"
        ]
        print("PASS: COA answered unmentioned office message via rules.toml")
    if not expect_failure and len(harnesses) >= 2:
        agent_notice_wakes_agent(m, harnesses)
    image = os.environ.get("APPLICATION_IMAGE")
    if image and "pi" in harnesses and not expect_failure:
        pi_deployment_flow(m, owner, owner_secret, execute, wait_ready, image)
    print("PASS: harness bridge turns, gateway state, and restart recovery")


def agent_notice_wakes_agent(m, harnesses):
    """Release notices are agent-signed messages with a p tag; buzz-acp must dispatch them."""
    sender = m.resource(f"harness-{harnesses[0]}", "agent")
    target = m.resource(f"harness-{harnesses[1]}", "agent")
    channel = m.resource("harness-lab", "channel")["uuid"]
    notice = m.actor(sender["secret"], sender["auth_tag"]).publish(
        sign(
            sender["secret"],
            9,
            [
                ["h", channel],
                ["p", target["pubkey"]],
                ["mention", target["pubkey"], "agent-address"],
            ],
            f"@{target['name']} Automatic release check: reply with one word to confirm you received this notice.",
        )
    )
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        events = m.buzz.query([{"kinds": [9], "#h": [channel], "limit": 100}])
        reply = next(
            (
                e
                for e in events
                if e["pubkey"] == target["pubkey"]
                and notice["id"] in [t[0] for t in tags(e, "e")]
            ),
            None,
        )
        if reply:
            print(f"notice reply from {target['id']}: {reply['content'][:120]!r}")
            print("PASS: agent-signed p-tag notice woke another buzz-acp agent")
            return
        time.sleep(10)
    raise AssertionError(f"{target['id']} ignored an agent-signed p-tag notice")


def pi_deployment_flow(m, owner, owner_secret, execute, wait_ready, image):
    """A pi agent plans and proposes via the bundled CLI; the owner approves."""
    m.deployments.register(
        {
            "id": "pi-acceptance",
            "environment": "staging",
            "repository": "test/pi-acceptance",
            "services": [
                {
                    "id": "web",
                    "command": ["python", "-m", "http.server", "8000"],
                    "port": 8000,
                    "health_path": "/",
                }
            ],
        }
    )
    m.deployments.release(
        {
            "id": "r1",
            "application": "pi-acceptance",
            "environment": "staging",
            "commit": "b" * 40,
            "images": {"web": image},
        }
    )
    execute(
        [
            {
                "action": "configure_deployment_access",
                "agent": "harness-pi",
                "application": "pi-acceptance",
                "environment": "staging",
            }
        ]
    )
    agent = m.resource("harness-pi", "agent")
    time.sleep(5)
    wait_ready(agent)
    channel = m.resource("harness-lab", "channel")["uuid"]
    request = owner.publish(
        sign(
            owner_secret,
            9,
            [["h", channel], ["p", agent["pubkey"]]],
            "@Harness pi Using your deployment CLI, plan deploying release r1 to "
            "pi-acceptance in the staging environment, then propose that plan in "
            "this thread for my approval. Do not execute it.",
        )
    )

    def proposal_by_agent():
        for (event,) in m.registry.db.execute("SELECT event FROM proposals"):
            event = json.loads(event)
            if event["pubkey"] == agent["pubkey"]:
                return event
        return None

    deadline = time.monotonic() + TURN_TIMEOUT
    proposal = None
    while time.monotonic() < deadline and not (proposal := proposal_by_agent()):
        time.sleep(10)
    assert proposal, "pi agent never published a deployment proposal"
    assert request["id"] in [t[0] for t in tags(proposal, "e")], proposal["tags"]
    print(f"pi proposal: {proposal['content'][:200]!r}")
    approval = owner.publish(
        sign(
            owner_secret,
            9,
            [
                ["h", channel],
                *reply_tags(proposal),
                ["p", agent["pubkey"]],
                ["mention", agent["pubkey"], "agent-address"],
            ],
            "approve",
        )
    )
    deadline = time.monotonic() + TURN_TIMEOUT
    job = None
    while time.monotonic() < deadline:
        jobs = [
            j
            for j in m.deployments.db.list("job")
            if j["application"] == "pi-acceptance"
        ]
        job = jobs[0] if jobs else None
        if job and job["state"] in ("succeeded", "failed"):
            break
        time.sleep(10)
    assert job and job["state"] == "succeeded", job
    print(f"PASS: pi agent deployed via CLI after owner approval {approval['id'][:8]}")
