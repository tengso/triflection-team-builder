"""Keep the worker container alive across gateway-only restarts.

Only the gateway PID is signalled. Detached applications must own their stdio
(e.g. log files), since pipes and PTYs owned by a gateway cannot survive its exit.
"""

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

CONTROL = "/tmp/team-builder-worker.sock"


def request_restart(path=CONTROL):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        connection.connect(str(path))
        connection.sendall(b"restart\n")
        value = json.loads(connection.makefile("rb").readline(1024))
        if value.get("status") != "restarted":
            raise RuntimeError("Gateway restart was not confirmed")
        return value


def supervise(command, env, path=CONTROL):
    """Act as the container's stable main process; supervise one gateway child."""
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)

    def launch():
        return subprocess.Popen(command, env=env, start_new_session=True)

    child = None
    control_path = Path(path)
    control_path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(control_path))
        os.chmod(control_path, 0o600)
        listener.listen(4)
        listener.settimeout(0.25)
        try:
            while not stopping:
                if child is None or child.poll() is not None:
                    # Bound crash loops without exiting the container and killing apps.
                    time.sleep(1)
                    if stopping:
                        break
                    try:
                        child = launch()
                    except OSError:
                        # A broken gateway executable must not bring down apps.
                        child = None
                        continue
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(1)
                    try:
                        if connection.makefile("rb").readline(32) != b"restart\n":
                            connection.sendall(b'{"error":"Unsupported request"}\n')
                            continue
                        # SIGTERM invokes Hermes' global tool-process cleanup. Kill
                        # only this child PID instead; never its group or descendants.
                        # Popen owns/reaps the child, preventing PID-reuse signalling.
                        child.kill()
                        child.wait(timeout=5)
                        child = launch()
                        connection.sendall(
                            json.dumps(
                                {"status": "restarted", "pid": child.pid}
                            ).encode()
                            + b"\n"
                        )
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        # A lost client must not terminate the supervisor/container.
                        continue
        finally:
            control_path.unlink(missing_ok=True)
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["restart"]:
        raise SystemExit("Expected restart")
    try:
        request_restart()
    except (OSError, ValueError, RuntimeError):
        raise SystemExit(
            "Gateway supervisor unavailable; runtime upgrade required"
        ) from None
