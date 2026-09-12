import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .nostr import wire


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data if isinstance(data, bytes) else wire(data))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Registry:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, source TEXT NOT NULL, body TEXT NOT NULL, event TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS authorizations (event TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)

    def get(self, identifier):
        row = self.db.execute(
            "SELECT body FROM resources WHERE id=?", (identifier,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, identifier, kind, body):
        with self.db:
            self.db.execute(
                "INSERT INTO resources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (identifier, kind, wire(body).decode()),
            )

    def list(self, kind=None):
        rows = (
            self.db.execute(
                "SELECT body FROM resources WHERE kind=? ORDER BY id", (kind,)
            )
            if kind
            else self.db.execute("SELECT body FROM resources ORDER BY id")
        )
        return [json.loads(row[0]) for row in rows]

    def bind(self, event, operations):
        body = wire(operations).decode()
        with self.db:
            row = self.db.execute(
                "SELECT body FROM authorizations WHERE event=?", (event,)
            ).fetchone()
            if row and row[0] != body:
                raise ValueError(
                    "This owner message is already bound to different operations"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO authorizations VALUES(?,?)", (event, body)
            )

    def operation(self, identifier, operation):
        body = wire(operation).decode()
        with self.db:
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (identifier,)
            ).fetchone()
            if row and row["body"] != body:
                raise ValueError("Operation identifier conflict")
            self.db.execute(
                "INSERT OR IGNORE INTO operations VALUES(?,?,?,NULL)",
                (identifier, body, "pending"),
            )
        return dict(row) if row else {"state": "pending"}

    def outcome(self, identifier, state, result):
        with self.db:
            self.db.execute(
                "UPDATE operations SET state=?,result=? WHERE id=?",
                (state, wire(result).decode(), identifier),
            )
