"""Announce existing GitHub repositories in Buzz; no GitHub credentials required."""

import hashlib
import re

from .nostr import tags


def github_url(value: str) -> str:
    # Match the whole input before parsing: reject credentials, query tokens,
    # subpaths and control characters instead of silently removing them.
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?",
        value,
        flags=re.IGNORECASE | re.ASCII,
    )
    if not match:
        raise ValueError("Use a GitHub repository URL: https://github.com/owner/repo")
    owner, repo = match.groups()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if owner in (".", "..") or repo in ("", ".", ".."):
        raise ValueError("Invalid GitHub repository path")
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def link_github_repository(manager, op):
    from .community import project

    item = manager.resource(op["project"], "project")
    if item.get("state") == "deleted":
        raise ValueError("Cannot link a repository to a deleted project")
    channel = manager.resource(item["channel"], "channel")
    if channel.get("state") == "deleted":
        raise ValueError("Cannot link a repository to a project with a deleted channel")
    url = github_url(op["url"])
    identifier = "github-" + hashlib.sha256(url.encode()).hexdigest()[:40]
    actor = manager.buzz
    coordinate = f"30617:{actor.pubkey}:{identifier}"
    project_head = actor.head(30621, actor.pubkey, item["id"])
    if not project_head:
        raise ValueError("Project announcement was not found in Buzz")
    repositories = sorted({t[0] for t in tags(project_head, "a")} | {coordinate})
    if len(repositories) > 64:
        raise ValueError("A Buzz project supports at most 64 repositories")
    head = actor.head(30617, actor.pubkey, identifier)
    expected = {
        "buzz-hosting": [["external"]],
        "web": [[url]],
        "clone": [[url + ".git"]],
    }
    if head and any(tags(head, name) != value for name, value in expected.items()):
        raise ValueError("Repository announcement conflicts with the GitHub URL")
    if not head:
        actor.replace(
            30617,
            [
                ["d", identifier],
                ["name", url.removeprefix("https://github.com/")],
                ["buzz-hosting", "external"],
                ["web", url],
                ["clone", url + ".git"],
            ],
            "",
        )
    # Verify the record even when retrying after a publish/response interruption.
    actual = actor.head(30617, actor.pubkey, identifier)
    if not actual or any(
        tags(actual, name) != value for name, value in expected.items()
    ):
        raise RuntimeError("Repository announcement failed authoritative readback")
    repository = {
        "id": identifier,
        "coordinate": coordinate,
        "url": url,
        "clone_url": url + ".git",
    }
    manager.registry.put("repository/" + identifier, "repository", repository)
    result = project(
        manager,
        {"action": "update_project", "id": item["id"], "repositories": repositories},
    )
    return {"project": result["id"], "repository": repository, "state": "linked"}
