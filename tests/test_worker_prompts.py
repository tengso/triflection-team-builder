import sqlite3

import pytest

from team_builder.storage import private_write
from team_builder.worker import refresh_session_prompts


def setup_home(home):
    private_write(home / "SOUL.md", b"Current instructions: link_github_repository")
    private_write(home / "config.yaml", b"model: test\n")
    with sqlite3.connect(home / "state.db") as db:
        db.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, system_prompt TEXT, system_prompt_hash TEXT,
                tool_names TEXT, ended_at REAL);
            CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT);
            CREATE TABLE messages (session_id TEXT, content TEXT);
            INSERT INTO system_prompts VALUES ('old-hash', 'Old capabilities');
            INSERT INTO sessions VALUES ('active', NULL, 'old-hash', '["old"]', NULL);
            INSERT INTO sessions VALUES ('legacy', 'Old inline prompt', NULL, NULL, NULL);
            INSERT INTO sessions VALUES ('ended', NULL, 'old-hash', '["old"]', 123);
            INSERT INTO messages VALUES ('active', 'Keep my conversation');
        """)


def test_upgrade_invalidates_both_prompt_formats_preserving_history(tmp_path):
    setup_home(tmp_path)
    assert refresh_session_prompts(tmp_path) == 2
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT * FROM sessions ORDER BY id").fetchall() == [
            ("active", None, None, None, None),
            ("ended", None, "old-hash", '["old"]', 123),
            ("legacy", None, None, None, None),
        ]
        assert db.execute("SELECT * FROM messages").fetchall() == [
            ("active", "Keep my conversation")
        ]
        assert db.execute("SELECT * FROM system_prompts").fetchall() == [
            ("old-hash", "Old capabilities")
        ]
        db.execute("UPDATE sessions SET system_prompt='Rebuilt' WHERE id='active'")
    assert refresh_session_prompts(tmp_path) == 0
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute(
            "SELECT system_prompt FROM sessions WHERE id='active'"
        ).fetchone() == ("Rebuilt",)


@pytest.mark.parametrize("changed", ["SOUL.md", "config.yaml", "schema"])
def test_changed_capabilities_refresh_again(tmp_path, monkeypatch, changed):
    from team_builder.models import Operations

    setup_home(tmp_path)
    refresh_session_prompts(tmp_path)
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("UPDATE sessions SET system_prompt='Rebuilt' WHERE id='active'")
    if changed == "schema":
        monkeypatch.setattr(Operations, "json_schema", lambda: {"new_operation": True})
    else:
        (tmp_path / changed).write_text("Changed")
    assert refresh_session_prompts(tmp_path) == 1


def test_interrupted_marker_write_retries(tmp_path, monkeypatch):
    from team_builder import worker

    setup_home(tmp_path)
    with monkeypatch.context() as patch:

        def fail_write(*args):
            raise OSError("interrupted")

        patch.setattr(worker, "private_write", fail_write)
        with pytest.raises(OSError, match="interrupted"):
            refresh_session_prompts(tmp_path)
    assert not (tmp_path / ".team-builder-prompt-generation").exists()
    assert refresh_session_prompts(tmp_path) == 0
    assert (tmp_path / ".team-builder-prompt-generation").exists()


def test_fresh_home_and_incompatible_database(tmp_path):
    private_write(tmp_path / "SOUL.md", b"Instructions")
    private_write(tmp_path / "config.yaml", b"model: test\n")
    assert refresh_session_prompts(tmp_path) == 0
    assert not (tmp_path / "state.db").exists()
    (tmp_path / ".team-builder-prompt-generation").unlink()
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE incompatible (id TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        refresh_session_prompts(tmp_path)
    assert not (tmp_path / ".team-builder-prompt-generation").exists()
