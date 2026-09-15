"""Real processes: gateway restarts must preserve detached HTTP applications."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from team_builder.worker_supervisor import request_restart


def eventually(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("Process readiness timed out")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux worker process semantics")
def test_gateway_restart_preserves_detached_http_server(tmp_path):
    # Short Unix socket path also works on macOS.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tb-supervisor-") as directory:
        root = Path(directory)
        control = root / "control.sock"
        child_info = root / "app.json"
        app = root / "app.py"
        app.write_text("""import json,os,sys
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b"alive")
server=HTTPServer(("127.0.0.1",0),Handler)
Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),"port":server.server_port}))
server.serve_forever()
""")
        gateway = root / "gateway.py"
        gateway.write_text("""import os,sys,time,subprocess
from pathlib import Path
root=Path(sys.argv[1])
if not (root/"app.json").exists():
    with (root/"app.log").open("ab",buffering=0) as log:
        subprocess.Popen([sys.executable,str(root/"app.py"),str(root/"app.json")],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
(root/"gateway.pid").write_text(str(os.getpid()))
while True: time.sleep(1)
""")
        program = "from team_builder.worker_supervisor import supervise; import os,sys; supervise([sys.executable,sys.argv[1],sys.argv[2]],os.environ.copy(),sys.argv[3])"
        supervisor = subprocess.Popen(
            [sys.executable, "-c", program, str(gateway), str(root), str(control)]
        )
        app_pid = None
        try:
            info = eventually(lambda: json.loads(child_info.read_text()))
            app_pid = info["pid"]
            old = eventually(lambda: int((root / "gateway.pid").read_text()))

            def healthy():
                with urlopen(f"http://127.0.0.1:{info['port']}", timeout=1) as response:
                    return response.read() == b"alive"

            assert healthy()
            for _ in range(2):
                result = request_restart(control)
                assert result["pid"] != old
                old = result["pid"]
                eventually(
                    lambda expected=old: (
                        int((root / "gateway.pid").read_text()) == expected
                    )
                )
                assert supervisor.poll() is None
                assert json.loads(child_info.read_text()) == info
                assert healthy()  # Writes a log too: stdio still works after restart.
            # Gateway crash also leaves the container supervisor and app alive.
            os.kill(old, signal.SIGKILL)
            eventually(lambda: int((root / "gateway.pid").read_text()) != old)
            assert healthy()
        finally:
            supervisor.terminate()
            supervisor.wait(timeout=10)
            if app_pid is not None:
                try:
                    os.kill(app_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def test_missing_supervisor_does_not_fall_back_to_container_restart(tmp_path):
    with pytest.raises(OSError):
        request_restart(tmp_path / "missing.sock")
