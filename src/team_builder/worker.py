import json
import os
import socket
import sys
from pathlib import Path

from .storage import private_write


def health():
    home = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
    try:
        state = json.loads((home / "gateway_state.json").read_text())
        pid = state["pid"]
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        buzz = state.get("platforms", {}).get("buzz", {})
        if (
            fields[0] == "Z"
            or int(fields[19]) != state["start_time"]
            or state.get("gateway_state") != "running"
            or state.get("restart_requested")
            or state.get("session_store", {}).get("status") != "ok"
            or buzz.get("state") != "connected"
            or buzz.get("writer_pid") != pid
            or buzz.get("writer_start_time") != state["start_time"]
            or buzz.get("error_code")
            or buzz.get("needs_attention")
        ):
            return False
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(home / "state" / f"gateway.loop-tick.{pid}.sock"))
            return connection.recv(2) == b"1"
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def main():
    if sys.argv[1:] == ["health"]:
        raise SystemExit(0 if health() else 1)
    if os.getuid() != 10000:
        raise SystemExit("Workers must run as UID 10000")
    home = Path("/home/hermes/.hermes")
    home.mkdir(exist_ok=True, mode=0o700)
    for name in ("config.yaml", "SOUL.md"):
        private_write(home / name, (Path("/run/team") / name).read_bytes())
    values = json.loads(Path("/run/team/env.json").read_text())
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("PROXY") and not k.endswith("proxy")
    }
    env.update(values)
    env.update(
        HOME="/home/hermes", HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS="1"
    )
    # Hermes loads its .env itself; write only this worker's credentials, never owner/admin keys.
    dotenv = "".join(
        k + "='" + v.replace("\\", "\\\\").replace("'", "\\'") + "'\n"
        for k, v in values.items()
    )
    private_write(home / ".env", dotenv.encode())
    os.execve(
        "/opt/hermes/.venv/bin/hermes",
        ["hermes", "gateway", "run", "--no-supervise", "--external-supervisor"],
        env,
    )


if __name__ == "__main__":
    main()
