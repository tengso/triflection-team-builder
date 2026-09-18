"""Verify applied gateway environment and native Hermes proxy selection."""

import json
import subprocess
import sys
import time


def exercise(state):
    config = json.loads((state / "config.json").read_text())
    container = config["project"] + "-agent-coa"
    before = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.Id}}", container], text=True
    )
    check = """
import json, os
from pathlib import Path
from agent.process_bootstrap import _get_proxy_for_base_url
state = json.loads(Path('/home/hermes/.hermes/gateway_state.json').read_text())
entries = Path('/proc/' + str(state['pid']) + '/environ').read_bytes().split(b'\\0')
env = dict(entry.decode().split('=', 1) for entry in entries if b'=' in entry)
for name in list(os.environ):
    if name.lower().endswith('proxy'):
        del os.environ[name]
os.environ.update({k: v for k, v in env.items() if k.lower().endswith('proxy')})
EXPECTED
assert _get_proxy_for_base_url('http://manager:8088') is None
assert _get_proxy_for_base_url('http://relay:3000') is None
"""
    for action, expected in [("set", "http://proxy.invalid:3128"), ("disable", None)]:
        command = [
            sys.executable,
            "-m",
            "team_builder.cli",
            "proxy",
            action,
            "--state-dir",
            str(state),
        ]
        if expected:
            command += ["--url", expected]
        subprocess.run(command, check=True, capture_output=True, text=True)
        script = check.replace(
            "EXPECTED",
            "assert _get_proxy_for_base_url('https://api.openai.com/v1') == "
            + repr(expected),
        )
        deadline = time.monotonic() + 60
        while True:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "/opt/hermes/.venv/bin/python",
                    "-",
                ],
                check=False,
                input=script,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise AssertionError(
                    "Gateway did not apply proxy policy: " + result.stderr
                )
            time.sleep(1)
    after = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.Id}}", container], text=True
    )
    assert before == after
    print(
        "PASS: proxy application/removal in live gateway, native model routing, internal bypass, container retention",
        flush=True,
    )
