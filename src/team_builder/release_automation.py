"""Durable, operator-authorized UAT acceptance and production promotion.

Agents can observe and retry their failed stage, but cannot author policy/checks or
claim acceptance. Only the manager executes the frozen operator-defined checks.
"""

import json
import re
import time
from typing import Annotated, Literal

from pydantic import Field

from .deployments import Slug, Strict
from .docker import generation
from .nostr import sign
from .storage import private_write


class Check(Strict):
    id: Slug
    service: Slug
    # "{environment}" is replaced with staging or production at run time, so one
    # repository-owned entrypoint (e.g. python -m acceptance) serves both.
    command: Annotated[list[str], Field(min_length=1, max_length=32)]
    timeout: Annotated[int, Field(ge=1, le=300)] = 20


class Policy(Strict):
    application: Slug
    enabled: bool = False
    staging_agent: Slug
    production_agent: Slug
    staging_profile: Slug
    production_profile: Slug
    notification_channel: Slug | None = None
    checks: Annotated[list[Check], Field(min_length=1, max_length=8)]
    # agent: the production agent may restore the previous release after failed
    # checks; automatic: the manager does so immediately; off: owner approval.
    production_rollback: Literal["agent", "automatic", "off"] = "agent"
    # redacted: assigned agents see masked check output and log tails.
    staging_diagnostics: Literal["redacted", "summary"] = "redacted"
    production_diagnostics: Literal["redacted", "summary"] = "redacted"


VERIFY_INTERVAL = 60
OUTPUT_LIMIT = 2000


class Automation:
    def __init__(self, service):
        self.service = service
        self.root = service.root / "automation"

    def configure(self, policy):
        policy = Policy.model_validate(policy).model_dump()
        app = policy["application"]
        if len({c["id"] for c in policy["checks"]}) != len(policy["checks"]):
            raise ValueError("Duplicate acceptance check")
        for env in ("staging", "production"):
            spec = self.service.get("app/" + self.service.key(app, env))["spec"]
            if {c["service"] for c in policy["checks"]} - {
                s["id"] for s in spec["services"]
            }:
                raise ValueError("Acceptance check references an unknown service")
            self.service.get(
                "profile/" + app + "/" + env + "/" + policy[env + "_profile"]
            )
            if policy["enabled"]:
                self.service.authorized(policy[env + "_agent"], app + "/" + env)
        if policy["notification_channel"] and policy["enabled"]:
            channel = self.service.manager.resource(
                policy["notification_channel"], "channel"
            )
            for role in ("staging_agent", "production_agent"):
                agent = self.service.manager.resource(policy[role], "agent")
                if channel["uuid"] not in agent["channel_ids"]:
                    raise ValueError(
                        "Release agents must belong to the notification channel"
                    )
            if policy["staging_agent"] == policy["production_agent"]:
                raise ValueError(
                    "Use dashboard notifications for a single release agent"
                )
        version = generation(policy)
        # Keep executable check specifications out of public registry/snapshots.
        private_write(self.root / (version + ".json"), policy)
        public = {k: v for k, v in policy.items() if k != "checks"}
        public.update(
            version=version,
            checks=[{"id": c["id"], "service": c["service"]} for c in policy["checks"]],
            updated_at=time.time(),
        )
        self.service.put("automation-policy/" + app, "automation_policy", public)
        return public

    def policy(self, application):
        public = self.service.get("automation-policy/" + application)
        return json.loads(
            (self.root / (public["version"] + ".json")).read_text()
        ), public

    def status(self, application, environment=None):
        with self.service.lock:
            policy = self.service.db.get("automation-policy/" + application)
            runs = [
                r
                for r in self.service.db.list("automation_run")
                if r["application"] == application
            ]
        # Handoff evidence is intentionally visible to both assigned release roles.
        return {
            "policy": policy,
            "runs": sorted(runs, key=lambda r: r["updated_at"], reverse=True)[:20],
        }

    def save(self, run):
        run["updated_at"] = time.time()
        self.service.put("automation-run/" + run["id"], "automation_run", run)

    def block(self, run, reason, owner_action=None):
        run.update(state="blocked", reason=reason, owner_action=owner_action)
        self.save(run)

    def current_evidence(self, application):
        app = self.service.get("app/" + application + "/staging")
        containers = []
        for s in app["spec"]["services"]:
            container = self.service.inspect(app, s["id"]) or {}
            if container.get("State", {}).get("Health", {}).get("Status") != "healthy":
                raise ValueError("UAT is not healthy")
            containers.append(
                {"service": s["id"], "id": container["Id"], "image": container["Image"]}
            )
        return {
            "release": app["current"],
            "revision": app["revision"],
            "configuration": app.get("configuration"),
            "containers": containers,
        }

    def check(self, app, check, environment):
        # Keep only the output tail. Kill the process group on timeout so a
        # blocked acceptance program does not survive as an unbounded exec.
        script = """import subprocess,sys,os,signal,json
p=subprocess.Popen(json.loads(sys.argv[1]),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
try:
    out,_=p.communicate(timeout=int(sys.argv[2]));code=p.returncode
except subprocess.TimeoutExpired:
    os.killpg(p.pid,signal.SIGKILL);out,_=p.communicate();code=124
sys.stdout.buffer.write((out or b"")[-8000:])
raise SystemExit(code)
"""
        command = [a.replace("{environment}", environment) for a in check["command"]]
        return self.service.docker.exec_output(
            self.service.name(app, check["service"]),
            ["python", "-c", script, json.dumps(command), str(check["timeout"])],
            timeout=check["timeout"] + 30,
        )

    def run_checks(self, application, policy, environment):
        app = self.service.get("app/" + application + "/" + environment)
        detail = policy.get(environment + "_diagnostics", "summary") == "redacted"
        redactor = self.service.redactor(app) if detail else None
        results = []
        for check in policy["checks"]:
            result = {"id": check["id"]}
            try:
                code, output = self.check(app, check, environment)
                result.update(passed=code == 0, exit_code=code)
            except Exception:  # noqa: BLE001 -- never expose Docker errors or credentials
                code, output = None, ""
                result.update(passed=False, exit_code=None)
            if detail and not result["passed"]:
                result["output"] = redactor.text(output)[-OUTPUT_LIMIT:]
            results.append(result)
        return results

    def acceptance(self, run, policy, environment):
        results = self.run_checks(run["application"], policy, environment)
        run[environment + "_checks"] = results
        self.save(run)
        return all(c["passed"] for c in results)

    def verify(self, application, environment, agent):
        """Run the policy's checks now, without deploying or spending a retry."""
        policy, _ = self.policy(application)
        if policy[environment + "_agent"] != agent:
            raise ValueError("Only the assigned release agent may verify this scope")
        key = "automation-verify/" + application + "/" + environment
        with self.service.lock:
            last = self.service.db.get(key)
            if last and time.time() - last["checked_at"] < VERIFY_INTERVAL:
                raise ValueError("Checks ran less than a minute ago; wait and retry")
            record = {"checked_at": time.time()}
            self.service.db.put(key, "automation_verify", record)
        app = self.service.get("app/" + application + "/" + environment)
        record.update(
            application=application,
            environment=environment,
            release=app["current"],
            checks=self.run_checks(application, policy, environment),
        )
        record["passed"] = all(c["passed"] for c in record["checks"])
        self.service.put(key, "automation_verify", record)
        return record

    def authorize_job(self, job):
        authority = job.get("automation")
        if not authority:
            return
        policy, public = self.policy(job["application"])
        if not policy["enabled"] or public["version"] != authority["policy"]:
            raise ValueError("Automatic release policy changed or disabled")
        env = job["environment"]
        self.service.authorized(policy[env + "_agent"], job["application"] + "/" + env)
        run = self.service.get("automation-run/" + authority["run"])
        if run["state"] == "blocked" or run.get(env + "_plan") != job["id"]:
            raise ValueError("Automatic release run no longer authorizes this job")
        if (
            job["release"] != run["release"]
            or job["configuration"] != run["bindings"][env]
        ):
            raise ValueError("Automatic release plan changed")
        if env == "production" and self.current_evidence(job["application"]) != run.get(
            "evidence"
        ):
            raise ValueError("UAT acceptance is stale; production was not changed")

    def queue(self, run, policy, env):
        d = self.service
        if not run.get(env + "_plan"):
            app = d.get("app/" + run["application"] + "/" + env)
            if (
                app["current"] == run["release"]
                and app.get("configuration") == run["bindings"][env]
            ):
                healthy = all(
                    (d.inspect(app, s["id"]) or {})
                    .get("State", {})
                    .get("Health", {})
                    .get("Status")
                    == "healthy"
                    for s in app["spec"]["services"]
                )
                if healthy:
                    run[env + "_retained"] = True
                    self.save(run)
                    return {"state": "succeeded"}
            # Planning can fail while a different authorized operation is active;
            # do not manufacture a new plan on restart if one was already frozen.
            result = d.profiles.plan(
                run["application"], env, policy[env + "_profile"], run["release"]
            )
            plan = d.get("plan/" + result["plan_id"])
            plan["automation"] = {"policy": run["policy"], "run": run["id"]}
            id = generation(plan)
            d.put("plan/" + id, "plan", plan)
            run[env + "_plan"] = id
            self.save(run)
        job = d.enqueue(
            run[env + "_plan"],
            source="release-policy/" + run["policy"],
            actor=policy[env + "_agent"],
        )
        return job

    def advance(self, run, policy):
        env = run["stage"]
        self.service.authorized(policy[env + "_agent"], run["application"] + "/" + env)
        for scope in ("staging", "production"):
            binding = self.service.profiles.binding(
                run["application"], scope, policy[scope + "_profile"]
            )
            if binding != run["bindings"][scope]:
                self.block(
                    run,
                    "Configuration changed; a new automatic release run is required",
                )
                return
        result = self.service.profiles.preflight(
            run["application"], env, policy[env + "_profile"]
        )
        if not result["ready"]:
            missing = [
                c["id"]
                for c in result["checks"]
                if c["kind"] == "credential" and c["status"] == "failed"
            ]
            configuration = any(
                c["kind"] in ("required_file", "required_variable")
                and c["status"] == "failed"
                for c in result["checks"]
            )
            if missing:
                self.block(
                    run,
                    "Required credentials are missing: " + ", ".join(missing),
                    owner_action(run["application"], env, missing),
                )
            elif configuration:
                self.block(
                    run,
                    "Required configuration is missing; the release agent should propose a corrected profile",
                )
            else:
                self.block(
                    run,
                    "A dependency is unavailable; the release agent should investigate",
                )
            return
        job = self.queue(run, policy, env)
        if job["state"] in ("queued", "running"):
            return
        if job["state"] != "succeeded":
            self.block(
                run,
                "Deployment failed; the assigned release agent should inspect diagnostics",
            )
            return
        if env == "staging":
            before = self.current_evidence(run["application"])
            if before["release"] != run["release"] or not self.acceptance(
                run, policy, env
            ):
                self.block(
                    run,
                    "UAT acceptance checks failed; the UAT agent must investigate before promotion",
                )
                return
            if before != self.current_evidence(run["application"]):
                self.block(run, "UAT changed during acceptance; no promotion permitted")
                return
            run.update(
                evidence=before,
                accepted_at=time.time(),
                stage="production",
                reason="UAT passed; handed off to the production release agent",
            )
            self.save(run)
        else:
            if not self.acceptance(run, policy, env):
                self.block(
                    run,
                    "Production acceptance checks failed; the release agent should investigate",
                )
                if policy.get("production_rollback") == "automatic":
                    try:
                        self.restore(run, policy[env + "_agent"])
                    except ValueError:
                        pass
                return
            run.update(
                state="succeeded",
                reason="UAT and production checks passed",
                completed_at=time.time(),
            )
            self.save(run)

    def retry(self, application, environment, agent):
        policy, public = self.policy(application)
        if policy[environment + "_agent"] != agent or not public["enabled"]:
            raise ValueError("Only the assigned release agent may retry this stage")
        runs = self.status(application)["runs"]
        if not runs:
            raise ValueError("No automatic release run")
        run = runs[0]
        if (
            run["stage"] != environment
            or run["state"] != "blocked"
            or run["policy"] != public["version"]
        ):
            raise ValueError("No retryable stage in this scope")
        if run.get("retries", 0) >= 2:
            raise ValueError(
                "Retry limit reached; fix configuration or publish a new release"
            )
        run.pop(environment + "_plan", None)
        run.update(
            state="active",
            retries=run.get("retries", 0) + 1,
            owner_action=None,
            reason="Assigned agent requested a bounded retry",
        )
        self.save(run)
        return run

    def latest_blocked(self, application, environment, public):
        runs = self.status(application)["runs"]
        run = runs[0] if runs else None
        if (
            not run
            or run["stage"] != environment
            or run["state"] != "blocked"
            or run["policy"] != public["version"]
        ):
            raise ValueError("No blocked stage in this scope")
        return run

    def rollback(self, application, agent):
        """Production agent restores the previous release after a failed stage."""
        policy, public = self.policy(application)
        if policy["production_agent"] != agent or not public["enabled"]:
            raise ValueError("Only the assigned production agent may roll back")
        if policy.get("production_rollback", "agent") == "off":
            raise ValueError(
                "The release policy disables agent rollback; propose a rollback plan for owner approval"
            )
        return self.restore(
            self.latest_blocked(application, "production", public), agent
        )

    def restore(self, run, agent):
        d = self.service
        app = d.get("app/" + run["application"] + "/production")
        if not app["previous"]:
            raise ValueError("No previous production release to restore")
        plan = d.plan(run["application"], "production", action="rollback")
        job = d.enqueue(
            plan["plan_id"], source="release-policy-rollback/" + run["id"], actor=agent
        )
        run.update(
            rollback={"plan": plan["plan_id"], "release": plan["release"]},
            reason="Production checks failed; restoring previous release "
            + plan["release"]
            + " (images only, database unchanged)",
        )
        self.save(run)
        return job

    def post(self, notice_id, sender, channel, tag_list, content):
        """Durable, idempotent Buzz notice signed by a release agent."""
        manager = self.service.manager
        with self.service.lock:
            notice = self.service.db.get("automation-notice/" + notice_id)
        if notice and notice.get("sent"):
            return
        if not notice:
            notice = {
                "event": sign(
                    sender["secret"], 9, [["h", channel["uuid"]], *tag_list], content
                ),
                "sent": False,
            }
            self.service.put(
                "automation-notice/" + notice_id, "automation_notice", notice
            )
        manager.actor(sender["secret"], sender["auth_tag"]).publish(notice["event"])
        notice["sent"] = True
        self.service.put("automation-notice/" + notice_id, "automation_notice", notice)

    def notify(self, run, policy):
        """Durable, idempotent technical handoff; no COA or owner signing key."""
        if not policy.get("notification_channel"):
            return
        if run["state"] == "active" and run["stage"] != "production":
            return
        manager = self.service.manager
        try:
            channel = manager.resource(policy["notification_channel"], "channel")
            target = manager.resource(policy[run["stage"] + "_agent"], "agent")
            sender_role = (
                "staging_agent" if run["stage"] == "production" else "production_agent"
            )
            sender = manager.resource(policy[sender_role], "agent")
            authoritative = manager.buzz.channel(channel["uuid"])
            if not authoritative or any(
                a["pubkey"] not in authoritative["roles"]
                or channel["uuid"] not in a["channel_ids"]
                for a in (target, sender)
            ):
                return
            state = [
                run["id"],
                run["state"],
                run["stage"],
                run.get("retries", 0),
                run["reason"],
            ]
            self.post(
                generation(state),
                sender,
                channel,
                # The p tag is what wakes a buzz-acp agent; the mention is display metadata.
                [
                    ["p", target["pubkey"]],
                    ["mention", target["pubkey"], "agent-address"],
                ],
                "@"
                + target["name"]
                + " Automatic release "
                + run["release"]
                + " for "
                + run["application"]
                + ": "
                + run["reason"]
                + ". Follow your release runbook skill: inspect release automation, the failed checks' output and service logs. Routine rollout is authorized by the standing release policy; do not request another owner approval. Fix the cause (code fixes arrive as a new CI release), verify, and retry only after a fix. Ask the owner only for missing credentials or a decision outside the policy.",
            )
            owner = manager.config["owner"]
            if (
                run["state"] == "blocked"
                and run.get("owner_action")
                and owner in authoritative["roles"]
            ):
                self.post(
                    generation(state + ["owner", run["owner_action"]]),
                    target,
                    channel,
                    [["p", owner]],
                    "Owner action needed for "
                    + run["application"]
                    + " release "
                    + run["release"]
                    + ": "
                    + run["owner_action"],
                )
        except Exception:  # noqa: BLE001 -- durable retry; notifications do not block releases
            return

    def tick(self):
        with self.service.lock:
            policies = self.service.db.list("automation_policy")
        for public in policies:
            if not public["enabled"]:
                continue
            run = None
            try:
                policy, public = self.policy(public["application"])
                application = public["application"]
                runs = self.status(application)["runs"]
                active = next(
                    (
                        r
                        for r in runs
                        if r["state"] == "active" and r["policy"] == public["version"]
                    ),
                    None,
                )
                if active:
                    run = active
                else:
                    with self.service.lock:
                        releases = self.service.db.list("release")
                    candidates = [
                        r
                        for r in releases
                        if r["application"] == application
                        and r["environment"] == "staging"
                        and re.fullmatch(r"ci-[0-9]+-[0-9]+", r["id"])
                    ]
                    if not candidates:
                        continue
                    release = max(
                        candidates,
                        key=lambda r: tuple(map(int, r["id"].split("-")[1:])),
                    )
                    target = self.service.get(
                        "release/" + application + "/production/" + release["id"]
                    )
                    if any(target[k] != release[k] for k in ("commit", "images")):
                        continue
                    bindings = {
                        env: self.service.profiles.binding(
                            application, env, policy[env + "_profile"]
                        )
                        for env in ("staging", "production")
                    }
                    id = generation([public["version"], release, bindings])
                    with self.service.lock:
                        previous = self.service.db.get("automation-run/" + id)
                    if previous:
                        self.notify(previous, policy)
                        continue
                    run = {
                        "id": id,
                        "application": application,
                        "release": release["id"],
                        "commit": release["commit"],
                        "policy": public["version"],
                        "bindings": bindings,
                        "stage": "staging",
                        "state": "active",
                        "reason": "New verified release selected",
                        "owner_action": None,
                        "created_at": time.time(),
                    }
                    self.save(run)
                self.advance(run, policy)
                self.notify(run, policy)
            except Exception:  # noqa: BLE001 -- leave a durable, sanitized technical failure
                if run:
                    self.block(
                        run,
                        "Automatic release paused; the assigned agent should inspect configuration and service diagnostics",
                    )
                    self.notify(run, policy)


def owner_action(application, environment, missing):
    """Exact host commands for credentials only the operator can supply."""
    commands = "; ".join(
        "echo '"
        + json.dumps(
            {
                "action": "credential",
                "application": application,
                "environment": environment,
                "id": ref,
            }
        )
        + "' > "
        + ref
        + ".json && team-builder deployment "
        + ref
        + ".json --secret-file /path/to/"
        + ref
        for ref in missing
    )
    return (
        "provide "
        + ", ".join(missing)
        + " for "
        + application
        + "/"
        + environment
        + " on the host (never in chat): "
        + commands
    )
