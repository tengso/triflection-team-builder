"""Immutable named credentials; approval records contain only their names."""

import json
import re

from .storage import private_write


def store_credential(root, identifier, api_key):
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,47}", identifier
    ):
        raise ValueError("Credential ID must be a lowercase slug")
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 16384:
        raise ValueError("Invalid credential")
    path = root / "credentials" / (identifier + ".json")
    if path.is_symlink():
        raise ValueError("Credential path must not be a symbolic link")
    if path.exists():
        if json.loads(path.read_text())["api_key"] != api_key:
            raise ValueError("Credential ID already exists; use a new ID for rotation")
        return
    private_write(path, {"api_key": api_key})
