"""Single-container worker process: verify the bundle, then run buzz-acp."""

import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .runtime import HARNESSES
from .storage import private_write
from .worker_config import applied_marker, install_skills, verify_bundle

LOG_TAIL = 40
CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"
CONNECT_MARKERS = (
    "connected to relay at",
    "presence set to online",
    "relay reconnected to",
    "reconnect succeeded",
)
RECONNECT_MARKERS = (
    "relay event stream ended",
    "relay connection lost",
    "requesting reconnect",
    "triggering reconnect",
    "connection dead",
)
TURN_START = re.compile(r"turn starting for channel ([0-9a-f-]{36})")
TURN_DELIVERED = re.compile(r"turn delivered Buzz events for channel ([0-9a-f-]{36})")


def harness():
    return os.environ.get("TEAM_BUILDER_HARNESS", "hermes")


def spec():
    return HARNESSES[harness()]


def home_dir():
    return Path(spec()["home"])


def state_dir():
    directory = home_dir() / spec()["state"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def process_start_time(pid):
    try:
        raw = Path("/proc/" + str(pid) + "/stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def config(managed):
    values = dict(os.environ)
    values.update(
        {
            key: str(value)
            for key, value in json.loads((managed / "env.json").read_text()).items()
        }
    )
    values["HOME"] = spec()["home"]
    return values


def prepare(managed, env):
    """Write per-harness CLI configuration from the managed bundle."""
    state = state_dir()
    private_write(state / "SOUL.md", (managed / "SOUL.md").read_bytes())
    if harness() == "hermes":
        hermes_home = home_dir() / ".hermes"
        hermes_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(
            hermes_home / "config.yaml", (managed / "config.yaml").read_bytes()
        )
        private_write(hermes_home / "SOUL.md", (managed / "SOUL.md").read_bytes())
        # Hermes loads its .env itself; write only managed values, escaped.
        values = json.loads((managed / "env.json").read_text())
        dotenv = "".join(
            k + "='" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'\n"
            for k, v in values.items()
        )
        private_write(hermes_home / ".env", dotenv.encode())
        env["HERMES_HOME"] = str(hermes_home)
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        install_skills(hermes_home, managed)
    else:
        from .harness_config import prepare as prepare_cli

        prepare_cli(managed, env, state)


def record(state, session_id, role):
    with closing(sqlite3.connect(state / "state.db", timeout=30)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, "
            "session_id TEXT, role TEXT, tool_name TEXT, timestamp TEXT)"
        )
        connection.execute(
            "INSERT INTO messages(session_id, role, tool_name, timestamp) "
            "VALUES (?, ?, NULL, datetime('now'))",
            (session_id, role),
        )
        connection.commit()


class Gateway:
    """Own the buzz-acp child: mirror its log, publish state, record turns."""

    def __init__(self):
        self.state = state_dir()
        self.buzz = CONNECTING
        self.child = None
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def write_state(self):
        with self.lock:
            self.write_state_locked()

    def set_buzz(self, value):
        with self.lock:
            self.buzz = value
            self.write_state_locked()

    def write_state_locked(self):
        writer = self.child.pid if self.child else None
        private_write(
            self.state / "gateway_state.json",
            {
                "gateway_state": "running",
                "pid": os.getpid(),
                "start_time": process_start_time(os.getpid()),
                "updated_at": datetime.now(UTC).isoformat(),
                "platforms": {"buzz": {"state": self.buzz, "writer_pid": writer}},
            },
        )

    def handle_line(self, line):
        sys.stdout.write(line)
        sys.stdout.flush()
        # CONNECT wins: "reconnect succeeded" also contains "reconnect".
        if any(marker in line for marker in CONNECT_MARKERS):
            self.set_buzz(CONNECTED)
        elif any(marker in line for marker in RECONNECT_MARKERS):
            self.set_buzz(RECONNECTING)
        match = TURN_START.search(line)
        if match:
            try:
                record(self.state, match.group(1), "user")
            except (OSError, sqlite3.Error):
                pass
            return
        match = TURN_DELIVERED.search(line)
        if match:
            try:
                record(self.state, match.group(1), "assistant")
            except (OSError, sqlite3.Error):
                pass

    def heartbeat(self):
        while not self.stop.wait(15):
            self.write_state()

    def launch(self, argv, env):
        def child_setup():
            os.setsid()
            try:
                import ctypes

                # Die with the worker so supervisor restarts never orphan buzz-acp.
                ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
            except (AttributeError, OSError):
                pass

        return subprocess.Popen(
            argv,
            env=env,
            cwd="/work",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            preexec_fn=child_setup,  # noqa: PLW1509 - no threads exist at launch
        )

    def run(self, argv, env):
        self.child = self.launch(argv, env)
        self.write_state()
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()

        def forward(signum, frame):
            if self.child.poll() is None:
                try:
                    os.killpg(self.child.pid, signum)
                except (ProcessLookupError, PermissionError):
                    pass
            self.stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, forward)
        try:
            for line in self.child.stdout:
                self.handle_line(line)
        finally:
            self.stop.set()
            if self.child.poll() is None:
                try:
                    os.killpg(self.child.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                self.child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.child.wait()
        return self.child.returncode or 0


def gateway():
    managed = Path("/run/team")
    state_dir()
    with (managed / ".config.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        verify_bundle(managed)
        env = config(managed)
        prepare(managed, env)
        applied_marker(state_dir(), managed)
    acp = spec()["acp"]
    argv = [
        "buzz-acp",
        "--agent-command",
        acp[0],
        "--system-prompt-file",
        str(state_dir() / "SOUL.md"),
    ]
    # Turn dispatch/delivery lines use target "pool::prompt", which the default
    # buzz_acp=info filter drops; state.db depends on them.
    env.setdefault("RUST_LOG", "buzz_acp=info,pool=info")
    # Marks the agent process as buzz-acp-managed; Hermes gates the BUZZ_*
    # credential passthrough into terminal children on it.
    env["BUZZ_MANAGED_AGENT"] = "1"
    # Agent containers mount /tmp noexec; buzz-acp's Pi launcher is exec'd.
    tmp = state_dir() / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    # buzz-acp requires a ws(s):// relay URL; our managed URL is http(s)://.
    relay = env.get("BUZZ_RELAY_URL", "")
    if relay.startswith("http://"):
        env["BUZZ_RELAY_URL"] = "ws://" + relay[len("http://") :]
    elif relay.startswith("https://"):
        env["BUZZ_RELAY_URL"] = "wss://" + relay[len("https://") :]
    # buzz-acp's --agent-args default is "acp"; always pass it explicitly.
    argv += ["--agent-args", ",".join(acp[1:])]
    if (managed / "rules.toml").exists():
        argv += ["--subscribe", "config", "--config", "/run/team/rules.toml"]
    sys.exit(Gateway().run(argv, env))


def health():
    try:
        state = json.loads((state_dir() / "gateway_state.json").read_text())
        pid = state.get("pid")
        buzz = state.get("platforms", {}).get("buzz", {})
        writer = buzz.get("writer_pid")
        return bool(
            state.get("gateway_state") == "running"
            and pid
            and process_start_time(pid) == state.get("start_time")
            and buzz.get("state") == CONNECTED
            and writer
            and process_start_time(writer) is not None
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    if sys.argv[1:] == ["health"]:
        raise SystemExit(0 if health() else 1)
    if sys.argv[1:] == ["restart"]:
        from .worker_supervisor import request_restart

        try:
            request_restart()
        except (OSError, ValueError, RuntimeError):
            raise SystemExit(
                "Gateway supervisor unavailable; a runtime upgrade is required"
            ) from None
        return
    if os.getuid() != 10000:
        raise SystemExit("Workers must run as UID 10000")
    if sys.argv[1:] != ["gateway"]:
        from .worker_supervisor import supervise

        supervise(
            [sys.executable, "-m", "team_builder.worker", "gateway"], os.environ.copy()
        )
        return
    gateway()


if __name__ == "__main__":
    main()
