"""Named GitHub credentials and a host-scoped Git credential helper."""

import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from pathlib import Path

from .credentials import store_credential


def read_token(key_file=None, env_file=None):
    if key_file:
        token = Path(key_file).read_text().strip()
    elif env_file:
        # Parse only GITHUB_TOKEN; never source shell code or interpolate values.
        matches = []
        for line in Path(env_file).read_text().splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?GITHUB_TOKEN\s*=\s*(.*?)\s*", line)
            if match:
                value = match[1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                matches.append(value)
        if len(matches) != 1:
            raise ValueError("The env file must contain exactly one GITHUB_TOKEN")
        token = matches[0]
    else:
        import getpass

        token = getpass.getpass("GitHub access token: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{10,255}", token):
        raise ValueError("Invalid GitHub token format")
    return token


def store(root, identifier, token):
    store_credential(root / "github", identifier, token)


def token_for(root, identifier):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", identifier):
        raise ValueError("Invalid GitHub credential name")
    path = root / "github" / "credentials" / (identifier + ".json")
    if not path.is_file() or path.is_symlink():
        raise ValueError(
            "GitHub credential not found; provision it with team-builder github-credential"
        )
    return json.loads(path.read_text())["api_key"]


def configure(manager, op):
    agent = manager.resource(op["agent"], "agent")
    if agent["state"] == "archived":
        raise ValueError("Cannot grant credentials to an archived agent")
    credential = op.get("credential")
    if credential:
        token_for(manager.root, credential)
    agent["github_credential"] = credential
    manager.save_agent(agent)
    # Reconcile the secret file even for a stopped worker; revocation removes it
    # immediately without starting the worker or discarding its workspace.
    from .runtime import write_github_token

    write_github_token(manager.root, agent)
    if agent["state"] == "running":
        manager.launch(agent)
    return {
        "agent": agent["id"],
        "github_credential": credential,
        "state": agent["state"],
    }


def operator_token(secrets):
    # COA's MCP bearer token cannot authorize local-operator maintenance.
    return hmac.new(
        secrets["admin"].encode(),
        b"team-builder-local-github-access-v1",
        hashlib.sha256,
    ).hexdigest()


def operator_apply(manager, operation, request_id):
    from .models import ConfigureGitHubAccess

    op = ConfigureGitHubAccess.model_validate(operation).model_dump(exclude_none=True)
    identifier = "operator-github/" + str(uuid.UUID(request_id))
    manager.registry.bind(identifier, [op])
    previous = manager.registry.operation(identifier, op)
    if previous["state"] == "complete":
        return json.loads(previous["result"])
    try:
        result = configure(manager, op)
    except Exception:
        manager.registry.outcome(
            identifier,
            "failed",
            {"error": "GitHub access reconciliation failed; retry"},
        )
        raise
    manager.registry.outcome(identifier, "complete", result)
    return result


def credential_helper():
    if sys.argv[1:] != ["get"]:
        return
    fields = {}
    for line in sys.stdin:
        if not line.strip():
            break
        key, separator, value = line.rstrip("\n").partition("=")
        if separator:
            fields[key] = value
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return
    try:
        token = Path(os.environ["GITHUB_TOKEN_FILE"]).read_text().strip()
    except (KeyError, OSError):
        return
    if not token or "\n" in token or "\r" in token:
        return
    # stdout is Git's credential pipe, never a chat/tool result.
    sys.stdout.write("username=x-access-token\npassword=" + token + "\n\n")


if __name__ == "__main__":
    credential_helper()
