"""Durable, operator-authorized UAT acceptance and production promotion.

Agents can observe and retry their failed stage, but cannot author policy/checks or
claim acceptance. Only the manager executes the frozen operator-defined checks.
"""

import json
import re
import time
from typing import Annotated

from pydantic import Field

from .deployments import Slug, Strict
from .docker import generation
from .nostr import sign
from .storage import private_write


class Check(Strict):
    id: Slug
    service: Slug
    command: Annotated[list[str], Field(min_length=1, max_length=32)]
    timeout: Annotated[int, Field(ge=1, le=30)] = 20


class Policy(Strict):
    application: Slug
    enabled: bool = False
    staging_agent: Slug
    production_agent: Slug
    staging_profile: Slug
    production_profile: Slug
    notification_channel: Slug | None = None
    checks: Annotated[list[Check], Field(min_length=1, max_length=8)]


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

    def check(self, app, check):
        # Suppress all application output. Kill the process group on timeout so a
        # blocked acceptance program does not survive as an unbounded exec.
        script = """import subprocess,sys,os,signal,json
p=subprocess.Popen(json.loads(sys.argv[1]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
try:
    code=p.wait(timeout=int(sys.argv[2]))
except subprocess.TimeoutExpired:
    os.killpg(p.pid,signal.SIGKILL);p.wait();code=124
raise SystemExit(code)
"""
        self.service.docker.exec(
            self.service.name(app, check["service"]),
            [
                "python",
                "-c",
                script,
                json.dumps(check["command"]),
                str(check["timeout"]),
            ],
        )

    def acceptance(self, run, policy, environment):
        app = self.service.get("app/" + run["application"] + "/" + environment)
        results = []
        for check in policy["checks"]:
            try:
                self.check(app, check)
                results.append({"id": check["id"], "passed": True})
            except Exception:  # noqa: BLE001 -- never expose check output or credentials
                results.append({"id": check["id"], "passed": False})
        run[environment + "_checks"] = results
        self.save(run)
        return all(c["passed"] for c in results)

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
            credential = any(
                c["kind"] in ("credential", "required_file", "required_variable")
                and c["status"] == "failed"
                for c in result["checks"]
            )
            self.block(
                run,
                "Required configuration is missing"
                if credential
                else "A dependency is unavailable; the release agent should investigate",
                "Please provide the missing application credential through the private operator setup."
                if credential
                else None,
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
            id = generation(
                [run["id"], run["state"], run["stage"], run.get("retries", 0)]
            )
            with self.service.lock:
                notice = self.service.db.get("automation-notice/" + id)
            if notice and notice.get("sent"):
                return
            if not notice:
                content = (
                    "@"
                    + target["name"]
                    + " Automatic release "
                    + run["release"]
                    + " for "
                    + run["application"]
                    + ": "
                    + run["reason"]
                    + ". Inspect release automation and service diagnostics. Routine rollout is authorized by the standing release policy; do not request another owner approval. Investigate technical failures and retry only after a fix. Ask the owner only for missing credentials or a decision outside the policy."
                )
                notice = {
                    "event": sign(
                        sender["secret"],
                        9,
                        [
                            ["h", channel["uuid"]],
                            ["mention", target["pubkey"], "agent-address"],
                        ],
                        content,
                    ),
                    "sent": False,
                }
                self.service.put("automation-notice/" + id, "automation_notice", notice)
            manager.actor(sender["secret"], sender["auth_tag"]).publish(notice["event"])
            notice["sent"] = True
            self.service.put("automation-notice/" + id, "automation_notice", notice)
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
