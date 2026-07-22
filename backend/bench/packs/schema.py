"""pack.yaml manifest models. `bench packs validate <dir>` uses these too."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

VALID_SHAPES = {"verdict", "extraction", "drafting", "qa_review", "freeform"}


class TaskType(BaseModel):
    slug: str
    display_name: str
    shape: str
    input_schema: dict = Field(default_factory=dict)  # flat {field: {type,...}} for form generation
    output_schema: str | None = None  # path to schemas/*.json, relative to pack dir
    terminal_tool: str | None = None
    tools: list[str] = Field(default_factory=list)
    instructions: str = ""
    output_contract: str = ""  # one-line summary shown to the router

    @field_validator("shape")
    @classmethod
    def _shape_valid(cls, v: str) -> str:
        if v not in VALID_SHAPES:
            raise ValueError(f"shape must be one of {sorted(VALID_SHAPES)}")
        return v


class PackDataset(BaseModel):
    name: str
    file: str  # CSV path relative to pack dir


class PackManifest(BaseModel):
    pack: str
    version: str
    display_name: str
    description: str = ""
    frameworks: list[str] = Field(default_factory=list)
    doctrine: list[str] = Field(default_factory=list)
    task_types: list[TaskType] = Field(default_factory=list)
    datasets: list[PackDataset] = Field(default_factory=list)
