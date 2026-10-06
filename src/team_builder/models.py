"""Only these typed operations are accepted by the management service."""

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from .agent_config import Settings
from .repositories import github_url

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Harness = Literal["hermes", "pi", "codex", "devin"]


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateAgent(Operation):
    action: Literal["create_agent"]
    id: Slug
    name: Name
    instructions: Annotated[str, Field(min_length=1, max_length=24000)]
    channels: Annotated[list[Slug], Field(min_length=1, max_length=30)]
    model: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    github_credential: Slug | None = None
    harness: Harness = Field(
        default="hermes",
        description="Agent runtime: hermes (default, full Buzz features), pi, codex, or devin. devin requires harness_credential naming a stored Devin account key.",
    )
    harness_credential: Slug | None = None

    @model_validator(mode="after")
    def check_harness_credential(self):
        if (self.harness == "devin") != (self.harness_credential is not None):
            raise ValueError(
                "harness_credential is required for devin and not accepted for other harnesses"
            )
        return self


class UpdateAgent(Operation):
    action: Literal["update_agent"]
    id: Slug
    name: Name | None = None
    instructions: Annotated[str, Field(min_length=1, max_length=24000)] | None = None
    model: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class ConfigureAgent(Operation):
    action: Literal["configure_agent"]
    id: Slug
    expected_revision: Annotated[int, Field(ge=0)]
    settings: Settings


class ApplyAgentConfig(Operation):
    action: Literal["apply_agent_config"]
    id: Slug


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


class ConfigureGitHubAccess(Operation):
    """Grant a named GitHub credential to one agent; omit credential to revoke."""

    action: Literal["configure_github_access"]
    agent: Slug
    credential: Slug | None = None


class ExecuteDeployment(Operation):
    action: Literal["execute_deployment"]
    plan_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ConfigureDeploymentAccess(Operation):
    action: Literal["configure_deployment_access"]
    agent: Slug
    application: Slug
    environment: Literal["staging", "production"] = "production"
    allowed: bool = True


Environment = Literal["staging", "production"]


class RegisterApplication(Operation):
    """Register an immutable application service specification for one environment."""

    action: Literal["register_application"]
    spec: dict


class RegisterProfile(Operation):
    """Register an immutable environment profile: values and credential references only."""

    action: Literal["register_profile"]
    application: Slug
    environment: Environment
    profile: dict


class GenerateCredential(Operation):
    """Create a generated application credential; supplied values stay operator-only."""

    action: Literal["generate_credential"]
    application: Slug
    environment: Environment
    id: Slug
    rotate: bool = False


class AttachDependency(Operation):
    """Attach an existing host container (for example a database) to an application network."""

    action: Literal["attach_dependency"]
    application: Slug
    environment: Environment
    container: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
    alias: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class ConfigureReleaseSync(Operation):
    """Let the manager import verified CI releases for an application."""

    action: Literal["configure_release_sync"]
    application: Slug
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    credential: Slug
    workflow: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+\.ya?ml$")] = "release.yml"
    branch: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_./-]+$")] = "main"
    environments: list[Environment] = ["staging", "production"]
    services: Annotated[list[Slug], Field(min_length=1, max_length=8)]
    enabled: bool = True


class ConfigureReleasePolicy(Operation):
    """Register, change, enable or pause an automatic UAT/production release policy.

    The policy is replaced as a whole, so agent proposals name the live policy
    version they were written against ("" when none exists yet). A proposal
    approved after the policy changed is rejected instead of silently undoing
    the newer change.
    """

    action: Literal["configure_release_policy"]
    policy: dict
    expected_version: Annotated[str, Field(max_length=64)] | None = None


class ConfigureReleaseAgent(Operation):
    """Give an agent deployment tools to propose new applications before any grant exists."""

    action: Literal["configure_release_agent"]
    agent: Slug
    enabled: bool = True


ManagementOperation = Annotated[
    CreateAgent
    | UpdateAgent
    | AgentState
    | ConfigureAgent
    | ApplyAgentConfig
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
    | ConfigureProvider
    | ConfigureGitHubAccess
    | ExecuteDeployment
    | ConfigureDeploymentAccess
    | RegisterApplication
    | RegisterProfile
    | GenerateCredential
    | AttachDependency
    | ConfigureReleaseSync
    | ConfigureReleasePolicy
    | ConfigureReleaseAgent,
    Field(discriminator="action"),
]
# Operations a release agent may propose (owner approval executes them).
RELEASE_CHANGES = {
    "register_application",
    "register_profile",
    "generate_credential",
    "attach_dependency",
    "configure_release_sync",
    "configure_release_policy",
    "configure_deployment_access",
}
Operations = TypeAdapter(list[ManagementOperation])


def validate(operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
        raise ValueError("Supply between 1 and 50 operations")
    return [
        op.model_dump(exclude_none=True)
        for op in Operations.validate_python(operations)
    ]
