import json
import sqlite3
import uuid

import pytest

from team_builder import worker
from team_builder.storage import private_write

CHANNEL = str(uuid.uuid4())


@pytest.fixture
def gateway(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "state_dir", lambda: tmp_path)
    capsys.readouterr()
    return worker.Gateway()


def test_turn_lines_record_messages(gateway):
    gateway.handle_line(f"INFO buzz_acp::queue turn starting for channel {CHANNEL}")
    gateway.handle_line(
        f"INFO buzz_acp::queue turn delivered Buzz events for channel {CHANNEL}"
    )
    rows = (
        sqlite3.connect(gateway.state / "state.db")
        .execute("SELECT session_id, role FROM messages ORDER BY id")
        .fetchall()
    )
    assert rows == [(CHANNEL, "user"), (CHANNEL, "assistant")]


def test_buzz_state_transitions_written(gateway):
    gateway.handle_line("INFO buzz_acp connected to relay at ws://relay:3000")
    state = json.loads((gateway.state / "gateway_state.json").read_text())
    assert state["gateway_state"] == "running"
    assert state["platforms"]["buzz"]["state"] == "connected"
    for line in (
        "WARN relay event stream ended; reconnecting in 2s",
        "WARN relay connection lost",
        "INFO requesting reconnect",
        "INFO triggering reconnect",
        "WARN connection dead, reconnecting",
    ):
        gateway.handle_line(line)
        state = json.loads((gateway.state / "gateway_state.json").read_text())
        assert state["platforms"]["buzz"]["state"] == "reconnecting", line
    for line in (
        "INFO relay reconnected to ws://relay:3000",
        "INFO autonomous reconnect succeeded",
        "INFO presence set to online",
    ):
        gateway.handle_line(line)
        state = json.loads((gateway.state / "gateway_state.json").read_text())
        assert state["platforms"]["buzz"]["state"] == "connected", line


def test_health_requires_connected_live_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        worker, "process_start_time", lambda pid: 4242 if pid < 100 else None
    )
    state = {
        "gateway_state": "running",
        "pid": 7,
        "start_time": 4242,
        "platforms": {"buzz": {"state": "connected", "writer_pid": 9}},
    }
    private_write(tmp_path / "gateway_state.json", state)
    assert worker.health() is True
    state["platforms"]["buzz"]["state"] = "connecting"
    private_write(tmp_path / "gateway_state.json", state)
    assert worker.health() is False
    state["platforms"]["buzz"] = {"state": "connected", "writer_pid": 2**22}
    private_write(tmp_path / "gateway_state.json", state)
    assert worker.health() is False
    (tmp_path / "gateway_state.json").unlink()
    assert worker.health() is False
