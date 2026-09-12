"""Only these typed operations are accepted by the management service."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from .repositories import github_url

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Name = Annotated[str, Field(min_length=1, max_length=120)]


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateAgent(Operation):
    action: Literal["create_agent"]
    id: Slug
    name: Name
    instructions: Annotated[str, Field(min_length=1, max_length=24000)]
    channels: Annotated[list[Slug], Field(min_length=1, max_length=30)]
    model: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class UpdateAgent(Operation):
    action: Literal["update_agent"]
    id: Slug
    name: Name | None = None
    instructions: Annotated[str, Field(min_length=1, max_length=24000)] | None = None
    model: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class AgentState(Operation):
    action: Literal["start_agent", "stop_agent", "archive_agent"]
    id: Slug


class CreateChannel(Operation):
    action: Literal["create_channel"]
    id: Slug
    name: Name
    description: Annotated[str, Field(max_length=2000)] = ""
    visibility: Literal["private", "public"] = "private"


class UpdateChannel(Operation):
    action: Literal["update_channel"]
    id: Slug
    name: Name | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    visibility: Literal["private", "public"] | None = None


class Membership(Operation):
    action: Literal["add_member", "remove_member"]
    channel: Slug
    agent: Slug


class DeleteChannel(Operation):
    action: Literal["delete_channel"]
    id: Slug


class HumanMembership(Operation):
    action: Literal["add_human_member", "remove_human_member"]
    channel: Slug
    pubkey: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    role: Literal["member", "admin", "owner"] = "member"


class Invite(Operation):
    action: Literal["create_invite"]
    id: Slug
    ttl_secs: Annotated[int, Field(ge=60, le=2592000)] = 259200
    max_uses: Annotated[int, Field(ge=1, le=10000)] = 1


RepoCoordinate = Annotated[
    str, Field(pattern=r"^30617:[0-9a-f]{64}:[^\s]+$", max_length=512)
]


class CreateProject(Operation):
    action: Literal["create_project"]
    id: Slug
    name: Name
    description: Annotated[str, Field(max_length=2000)] = ""
    channel: Slug
    repositories: Annotated[list[RepoCoordinate], Field(max_length=64)] = []
    visibility: Literal["listed", "unlisted"] = "listed"


class UpdateProject(Operation):
    action: Literal["update_project"]
    id: Slug
    name: Name | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    channel: Slug | None = None
    repositories: Annotated[list[RepoCoordinate], Field(max_length=64)] | None = None
    visibility: Literal["listed", "unlisted"] | None = None


class DeleteProject(Operation):
    action: Literal["delete_project"]
    id: Slug


class LinkGitHubRepository(Operation):
    """Announce an existing GitHub repository and add it to a project, keeping existing links."""

    action: Literal["link_github_repository"]
    project: Slug
    url: Annotated[
        str,
        Field(
            max_length=512,
            description="GitHub repository URL, e.g. https://github.com/owner/repo; Buzz registration is automatic",
        ),
        AfterValidator(github_url),
    ]


class ConfigureProvider(Operation):
    action: Literal["configure_provider"]
    provider: Literal["openrouter", "openai", "custom"]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    credential: Slug
    base_url: Annotated[str, Field(max_length=2000)] | None = None


ManagementOperation = Annotated[
    CreateAgent
    | UpdateAgent
    | AgentState
    | CreateChannel
    | UpdateChannel
    | Membership
    | DeleteChannel
    | HumanMembership
    | Invite
    | CreateProject
    | UpdateProject
    | DeleteProject
    | LinkGitHubRepository
    | ConfigureProvider,
    Field(discriminator="action"),
]
Operations = TypeAdapter(list[ManagementOperation])


def validate(operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
        raise ValueError("Supply between 1 and 50 operations")
    return [
        op.model_dump(exclude_none=True)
        for op in Operations.validate_python(operations)
    ]
