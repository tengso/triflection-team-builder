"""Bounded, read-only diagnostics. Never serialize raw registry or runtime objects."""

import json
import re
import socket
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx

from .buzz import Buzz
from .nostr import tags

SLUG = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
SERVICES = ("manager", "relay", "postgres", "redis", "minio", "minio-init")


def read_local(path, parent, limit=262144):
    path, parent = Path(path), Path(parent)
    if not path.resolve().is_relative_to(parent.resolve()) or not path.is_file():
        raise ValueError("Unavailable local diagnostic")
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Diagnostic exceeds limit")
    return data


class Redactor:
    def __init__(self, root):
        values = []
        for path in [
            root / "secrets.json",
            root / "provider.json",
            *root.glob("credentials/*.json"),
            *root.glob("github/credentials/*.json"),
        ]:
            try:
                values.extend(
                    v
                    for v in json.loads(path.read_text()).values()
                    if isinstance(v, str) and len(v) >= 8
                )
            except (OSError, ValueError):
                pass
        try:
            with sqlite3.connect(
                (root / "registry.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=0.1
            ) as db:
                values.extend(
                    json.loads(row[0]).get("secret", "")
                    for row in db.execute(
                        "SELECT body FROM resources WHERE kind='agent'"
                    )
                )
        except (sqlite3.Error, ValueError):
            pass
        self.values = sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)

    def text(self, value):
        value = str(value)
        for secret in self.values:
            value = value.replace(secret, "[redacted]")
        value = re.sub(
            r"(?i)(?:bearer\s+\S+|(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+|nsec1[a-z0-9]+)",
            "[redacted]",
            value,
        )
        value = re.sub(
            r"(?i)((?:password|token|secret|api[_-]?key|authorization)\s*[:=]\s*)([^\s,;]+)",
            r"\1[redacted]",
            value,
        )
        return value[:24000]

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        return value


class Engine:
    """Only exposes bounded Docker GETs for an exact, label-checked container."""

    def __init__(self, project):
        self.project = project
        self.previous_cpu = {}
        self.stats_lock = threading.Lock()
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds="/var/run/docker.sock"),
            base_url="http://docker",
            timeout=2,
        )

    def get(self, path, limit=262144):
        with self.client.stream("GET", "/v1.45" + path) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > limit:
                    raise ValueError("Diagnostic exceeds limit")
                chunks.append(chunk)
            return b"".join(chunks)

    def inspect(self, name):
        raw = self.get("/containers/" + quote(name, safe="") + "/json")
        if raw is None:
            return None
        data = json.loads(raw)
        if (
            data.get("Config", {}).get("Labels", {}).get("io.team-builder.project")
            != self.project
        ):
            raise ValueError("Container outside installation")
        return data

    def sample(self, name):
        data = self.inspect(name)
        if data is None:
            return {
                "status": "missing",
                "health": "unknown",
                "collected_at": time.time(),
            }
        state = data.get("State", {})
        out = {
            "status": state.get("Status", "unknown"),
            "health": state.get("Health", {}).get("Status", "unknown"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "restart_count": data.get("RestartCount"),
            "image": data.get("Config", {}).get("Image"),
            "cpu_percent": None,
            "memory_bytes": None,
            "memory_limit": None,
            "collected_at": time.time(),
        }
        if state.get("Running"):
            try:
                raw = self.get(
                    "/containers/"
                    + quote(data["Id"], safe="")
                    + "/stats?stream=false&one-shot=true"
                )
                stats = json.loads(raw)
                cpu = stats.get("cpu_stats", {})
                with self.stats_lock:
                    prev = self.previous_cpu.get(data["Id"], {})
                    if len(self.previous_cpu) >= 2000:
                        self.previous_cpu.clear()
                    self.previous_cpu[data["Id"]] = cpu
                delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - prev.get(
                    "cpu_usage", {}
                ).get("total_usage", 0)
                system = cpu.get("system_cpu_usage", 0) - prev.get(
                    "system_cpu_usage", 0
                )
                out["cpu_percent"] = (
                    round(delta / system * cpu.get("online_cpus", 1) * 100, 2)
                    if prev and system > 0 and delta >= 0
                    else None
                )
                memory = stats.get("memory_stats", {})
                out.update(
                    memory_bytes=memory.get("usage"), memory_limit=memory.get("limit")
                )
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        return out

    def logs(self, name):
        data = self.inspect(name)
        if not data:
            raise ValueError("Container missing")
        raw = (
            self.get(
                "/containers/"
                + quote(data["Id"], safe="")
                + "/logs?stdout=true&stderr=true&timestamps=true&tail=100",
                limit=131072,
            )
            or b""
        )
        if not data.get("Config", {}).get("Tty"):
            chunks = []
            while len(raw) >= 8:
                size = int.from_bytes(raw[4:8], "big")
                if size > len(raw) - 8:
                    break
                chunks.append(raw[8 : 8 + size])
                raw = raw[8 + size :]
            raw = b"".join(chunks)
        # Runtime output may contain whole conversations and shell commands. Only
        # emit recognized diagnostic categories, never arbitrary free-form lines.
        entries = []
        patterns = [
            (
                r"WebSocket disconnected",
                "warning",
                "Buzz WebSocket disconnected; reconnecting",
            ),
            (r"no WebSocket frame", "warning", "Buzz WebSocket inactivity timeout"),
            (r"Community manager ready", "info", "Community manager ready"),
            (r"Bootstrap pending", "warning", "Bootstrap incomplete; retry pending"),
            (
                r"Gateway.*(?:started|running)|Connected to.*[Bb]uzz",
                "info",
                "Gateway connection established",
            ),
            (r"[Ee]vent loop missed", "error", "Gateway event loop unresponsive"),
            (r"[Oo]ut of memory|OOMKilled", "error", "Out-of-memory diagnostic"),
            (
                r"database system is ready to accept connections",
                "info",
                "Database ready",
            ),
            (r"Ready to accept connections", "info", "Service ready"),
            (r"[Cc]onnection refused", "warning", "Connection refused"),
            (r"[Tt]imed out|[Tt]imeout", "warning", "Operation timeout"),
            (
                r"Traceback \(most recent call last\)",
                "error",
                "Runtime exception; inspect logs locally for details",
            ),
        ]
        for line in raw.decode(errors="replace").splitlines():
            for pattern, level, summary in patterns:
                if re.search(pattern, line):
                    timestamp = re.match(r"\d{4}-\d\d-\d\dT[0-9:.]+Z", line)
                    entries.append(
                        {
                            "timestamp": timestamp[0] if timestamp else None,
                            "level": level,
                            "message": summary,
                        }
                    )
                    break
        return entries[-100:]


def gateway(root, identifier, container):
    home = root / "agents" / identifier / "home" / ".hermes"
    out = {
        "responsive": None,
        "state": "unknown",
        "buzz": "unknown",
        "active_tasks": None,
        "updated_at": None,
        "stale": True,
    }
    try:
        if not home.resolve().is_relative_to(root / "agents" / identifier):
            return out
        state = json.loads(read_local(home / "gateway_state.json", home))
        out["updated_at"] = state.get("updated_at")
        from datetime import datetime

        updated = datetime.fromisoformat(state["updated_at"]).timestamp()
        started = container.get("started_at")
        # Hermes updates this record on state changes, not every poll. A live
        # loop witness, matching writer identity, and current container generation
        # establish freshness; an idle agent's old state timestamp is normal.
        if started and updated < datetime.fromisoformat(started).timestamp():
            return out
        buzz = state.get("platforms", {}).get("buzz", {})
        if buzz.get("writer_pid") != state.get("pid") or buzz.get(
            "writer_start_time"
        ) != state.get("start_time"):
            return out
        if container.get("status") != "running":
            return out
        out["state"] = state.get("gateway_state", "unknown")
        pid = int(state["pid"])
        path = home / "state" / f"gateway.loop-tick.{pid}.sock"
        if not path.resolve().is_relative_to(home.resolve()):
            return out
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.4)
            connection.connect(str(path))
            out["responsive"] = connection.recv(2) == b"1"
        out["stale"] = not out["responsive"]
        if out["responsive"]:
            out["buzz"] = (
                state.get("platforms", {}).get("buzz", {}).get("state", "unknown")
            )
            active = state.get("active_agents")
            out["active_tasks"] = (
                active if isinstance(active, int) and active >= 0 else None
            )
    except (OSError, ValueError, KeyError, TypeError):
        if container.get("status") == "running":
            out["responsive"] = False
    return out


def agent_activity(root, identifier):
    home = root / "agents" / identifier / "home" / ".hermes"
    path = home / "state.db"
    if (
        not home.resolve().is_relative_to(root / "agents" / identifier)
        or not path.exists()
        or not path.resolve().is_relative_to(home.resolve())
    ):
        return {"status": "unavailable", "messages": []}
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            rows = db.execute(
                "SELECT id,session_id,role,tool_name,timestamp FROM messages ORDER BY id DESC LIMIT 20"
            ).fetchall()
        return {
            "status": "available",
            "messages": [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "role": r[2],
                    "tool": r[3],
                    "timestamp": r[4],
                }
                for r in rows
            ],
        }
    except sqlite3.Error:
        return {"status": "unavailable", "messages": []}


class Observer:
    def __init__(self, manager, engine=None, buzz=None):
        self.root, self.config = manager.root, manager.config.copy()
        self.manager = manager
        self.engine = engine or Engine(self.config["project"])
        self.buzz = buzz or Buzz(
            "http://relay:3000",
            manager.secrets["admin"],
            self.config["relay"],
            canonical_origin=self.config["advertised_url"],
        )
        if hasattr(self.buzz, "client"):
            self.buzz.client.timeout = httpx.Timeout(2)
        self.lock = threading.Lock()
        self.snapshot = {
            "collected_at": None,
            "agents": [],
            "services": [],
            "channels": [],
            "projects": [],
            "repositories": [],
            "activity": {},
            "errors": ["Collecting first snapshot"],
        }
        self.samples = deque(maxlen=720)
        self.stop = threading.Event()
        self.started_at = time.time()

    def registry(self):
        with sqlite3.connect(
            (self.root / "registry.sqlite3").as_uri() + "?mode=ro",
            uri=True,
            timeout=0.2,
        ) as db:
            db.execute("BEGIN")
            resources = [
                (kind, json.loads(body))
                for kind, body in db.execute(
                    "SELECT kind,body FROM resources WHERE kind IN ('agent','channel','project','repository') ORDER BY id LIMIT 2000"
                )
            ]
            operations = []
            for identifier, body, state in db.execute(
                "SELECT id,body,state FROM operations ORDER BY rowid DESC LIMIT 100"
            ):
                op = json.loads(body)
                operations.append(
                    {
                        "id": identifier,
                        "action": op.get("action"),
                        "target": op.get(
                            "id", op.get("agent", op.get("channel", op.get("project")))
                        ),
                        "state": state,
                    }
                )
            proposals = []
            for identifier, source, body, event in db.execute(
                "SELECT id,source,body,event FROM proposals ORDER BY rowid DESC LIMIT 100"
            ):
                operations_body = json.loads(body)
                event_body = json.loads(event)
                proposal_status = "approval_not_recorded"
                for approval_id, source_tags, author in db.execute(
                    "SELECT a.event,json_extract(r.body,'$.tags'),json_extract(r.body,'$.pubkey') "
                    "FROM authorizations a JOIN resources r ON r.id='source/' || a.event WHERE a.body=?",
                    (body,),
                ):
                    if author != self.config["owner"] or [
                        "e",
                        event_body.get("id"),
                        "",
                        "reply",
                    ] not in json.loads(source_tags):
                        continue
                    states = [
                        row[0]
                        for row in db.execute(
                            "SELECT state FROM operations WHERE id LIKE ?",
                            (approval_id + "/%",),
                        )
                    ]
                    proposal_status = (
                        "completed"
                        if len(states) == len(operations_body)
                        and all(state == "done" for state in states)
                        else "partial_failure"
                        if "failed" in states
                        else "approved"
                    )
                proposals.append(
                    {
                        "id": identifier,
                        "source_event_id": source,
                        "event_id": event_body.get("id"),
                        "created_at": event_body.get("created_at"),
                        "actions": [op.get("action") for op in operations_body],
                        "status": proposal_status,
                    }
                )
        return resources, {
            "operations": operations,
            "proposals": proposals,
            "note": "Legacy operations have no timestamps; newest recorded entries first.",
        }

    def container(self, name):
        try:
            return self.engine.sample(name)
        except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError):
            return {
                "status": "unavailable",
                "health": "unknown",
                "collected_at": time.time(),
            }

    def resource(self, kind, item):
        identifier = item.get("id", "")
        if not SLUG.fullmatch(identifier):
            return None
        fields = {
            "agent": (
                "id",
                "name",
                "pubkey",
                "state",
                "model",
                "instructions",
                "channel_ids",
                "github_credential",
            ),
            "channel": ("id", "uuid", "name", "description", "visibility", "state"),
            "project": (
                "id",
                "name",
                "description",
                "coordinate",
                "channel",
                "repositories",
                "visibility",
                "state",
            ),
            "repository": ("id", "name", "coordinate", "url", "state"),
        }
        if kind not in fields:
            return None
        out = {key: item.get(key) for key in fields[kind]}
        out["collected_at"] = time.time()
        if kind == "agent":
            name = self.config["project"] + "-agent-" + identifier
            out["container"] = self.container(name)
            out["gateway"] = gateway(self.root, identifier, out["container"])
            out["activity"] = agent_activity(self.root, identifier)
            out["owner"] = (
                self.config["owner"]
                if identifier == "coa"
                else self.config.get("coa_pubkey")
            )
            out["model"] = out["model"] or self.config.get("model")
            try:
                registration = self.buzz.head(30177, out["owner"], out["pubkey"])
                profile = self.buzz.head(0, out["pubkey"])
                out["registration"] = {
                    "status": "present" if registration and profile else "missing",
                    "owner": out["owner"],
                    "profile_present": bool(profile),
                    "collected_at": time.time(),
                }
            except (
                httpx.HTTPError,
                ValueError,
                RuntimeError,
                OSError,
                KeyError,
                TypeError,
            ):
                out["registration"] = {
                    "status": "unavailable",
                    "collected_at": time.time(),
                }
        if kind in ("channel", "project"):
            try:
                if kind == "channel":
                    live = self.buzz.channel(item["uuid"])
                    out["buzz"] = {
                        "status": "present" if live else "missing",
                        "members": [
                            {"pubkey": key, "role": role}
                            for key, role in (live or {}).get("roles", {}).items()
                        ],
                        "name": ((live or {}).get("name") or [[None]])[0][0],
                        "collected_at": time.time(),
                    }
                else:
                    head = self.buzz.head(30621, self.buzz.pubkey, identifier)
                    out["buzz"] = {
                        "status": "present" if head else "missing",
                        "repositories": [t[0] for t in tags(head, "a")] if head else [],
                        "event_id": head["id"] if head else None,
                        "collected_at": time.time(),
                    }
            except (
                httpx.HTTPError,
                ValueError,
                RuntimeError,
                OSError,
                KeyError,
                TypeError,
            ):
                out["buzz"] = {"status": "unavailable", "collected_at": time.time()}
        out["collected_at"] = time.time()
        return kind, out

    def collect(self):
        collection_started = time.time()
        redactor = Redactor(self.root)
        try:
            resources, activity = self.registry()
        except (sqlite3.Error, ValueError, OSError, TypeError):
            with self.lock:
                self.snapshot = {
                    **self.snapshot,
                    "errors": ["Registry unavailable; showing last snapshot"],
                }
            return
        redactor.values += [
            item["secret"]
            for kind, item in resources
            if kind == "agent"
            and isinstance(item.get("secret"), str)
            and len(item["secret"]) >= 8
        ]
        coa = next(
            (
                item
                for kind, item in resources
                if kind == "agent" and item.get("id") == "coa"
            ),
            {},
        )
        self.config["coa_pubkey"] = coa.get("pubkey")
        try:
            provider = json.loads((self.root / "provider.json").read_text())
        except (OSError, ValueError):
            provider = self.config
        self.config["model"] = provider.get("model")
        snapshot = {
            "community": {
                "name": self.config.get("name", "Buzz community"),
                "url": self.config["advertised_url"],
                "owner": self.config["owner"],
                "provider": provider.get("provider"),
                "model": provider.get("model"),
                "provider_credential": provider.get(
                    "credential", "initial provider credential"
                ),
                "manager_ready": bool(getattr(self.manager, "bootstrapped", False)),
            },
            "agents": [],
            "channels": [],
            "projects": [],
            "repositories": [],
            "services": [],
            "activity": activity,
            "errors": [],
        }
        with ThreadPoolExecutor(max_workers=12) as pool:
            service_jobs = [
                (
                    service,
                    pool.submit(
                        self.container, self.config["project"] + "-" + service + "-1"
                    ),
                )
                for service in SERVICES
            ]
            for result in pool.map(lambda pair: self.resource(*pair), resources):
                if result:
                    kind, item = result
                    snapshot[
                        {
                            "agent": "agents",
                            "channel": "channels",
                            "project": "projects",
                            "repository": "repositories",
                        }[kind]
                    ].append(item)
            for service, future in service_jobs:
                observed = future.result()
                if (
                    service == "minio-init"
                    and observed.get("status") == "exited"
                    and observed.get("exit_code") == 0
                ):
                    observed["status"] = "completed"
                snapshot["services"].append({"id": service, **observed})
        try:
            roster = self.buzz.roster()
            snapshot["community"]["membership"] = {
                "status": "available",
                "members": [
                    {"pubkey": key, "role": role} for key, role in roster.items()
                ],
                "collected_at": time.time(),
            }
        except (httpx.HTTPError, ValueError, RuntimeError, OSError, TypeError):
            snapshot["community"]["membership"] = {
                "status": "unavailable",
                "members": [],
                "collected_at": time.time(),
            }
        snapshot["collected_at"] = collection_started
        snapshot = redactor.clean(snapshot)
        sample = {
            "timestamp": time.time(),
            "services": [
                {k: service.get(k) for k in ("id", "cpu_percent", "memory_bytes")}
                for service in snapshot["services"]
            ],
            "agents": [
                {
                    "id": agent["id"],
                    **{
                        k: agent["container"].get(k)
                        for k in ("cpu_percent", "memory_bytes")
                    },
                }
                for agent in snapshot["agents"]
            ],
        }
        with self.lock:
            self.snapshot = snapshot
            self.samples.append(sample)
            while self.samples and self.samples[0]["timestamp"] < time.time() - 3600:
                self.samples.popleft()

    def read(self):
        with self.lock:
            return {
                **self.snapshot,
                "stale": not self.snapshot["collected_at"]
                or time.time() - self.snapshot["collected_at"] > 15,
                "history_started_at": self.started_at,
                "history": list(self.samples),
            }

    def logs(self, identifier):
        snapshot = self.read()
        if identifier in SERVICES:
            name = self.config["project"] + "-" + identifier + "-1"
        elif identifier.startswith("agent:") and any(
            agent["id"] == identifier[6:] for agent in snapshot["agents"]
        ):
            name = self.config["project"] + "-agent-" + identifier[6:]
        else:
            raise ValueError("Unknown component")
        return {
            "entries": self.engine.logs(name),
            "note": "Recognized diagnostic entries from the last 100 container log lines. Command and conversation content omitted.",
            "collected_at": time.time(),
        }

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.collect()
            except Exception:  # noqa: BLE001 -- keep previous snapshot on diagnostic failure
                with self.lock:
                    self.snapshot = {
                        **self.snapshot,
                        "errors": ["Diagnostics unavailable; showing last snapshot"],
                    }
            self.stop.wait(max(0.1, 5 - (time.monotonic() - started)))
