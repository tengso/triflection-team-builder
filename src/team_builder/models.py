"""Only these typed operations are accepted by the management service."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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
    name: Name
    description: Annotated[str, Field(max_length=2000)] = ""


class Membership(Operation):
    action: Literal["add_member", "remove_member"]
    channel: Slug
    agent: Slug


Operations = TypeAdapter(
    list[
        Annotated[
            CreateAgent
            | UpdateAgent
            | AgentState
            | CreateChannel
            | UpdateChannel
            | Membership,
            Field(discriminator="action"),
        ]
    ]
)


def validate(operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
        raise ValueError("Supply between 1 and 50 operations")
    return [
        op.model_dump(exclude_none=True)
        for op in Operations.validate_python(operations)
    ]
