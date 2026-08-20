"""pack.yaml manifest models. `tret packs validate <dir>` uses these too."""
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
    # Doctrine this task actually needs: entries are files from the pack's own
    # `doctrine:` list, optionally narrowed to a `#`/`##` heading
    # ("02-procedure.md#Step 5 — Compare on the overlap only"). Empty (the
    # default) loads every doctrine file, so existing packs are unaffected.
    doctrine: list[str] = Field(default_factory=list)

    @field_validator("shape")
    @classmethod
    def _shape_valid(cls, v: str) -> str:
        if v not in VALID_SHAPES:
            raise ValueError(f"shape must be one of {sorted(VALID_SHAPES)}")
        return v


class PackDataset(BaseModel):
    name: str
    file: str  # CSV path relative to pack dir


class PackMethod(BaseModel):
    """A vetted deterministic analytics script the agent may invoke (never write).

    Contract: the entrypoint reads JSON {"params": {...}, "inputs": {name: [rows]}}
    on stdin and writes JSON {"rows": [...]} on stdout. Pure function; stdlib only.
    """

    slug: str
    display_name: str
    description: str = ""
    entrypoint: str  # path relative to pack dir, e.g. methods/foo.py
    params_schema: dict = Field(default_factory=dict)  # flat fields, like input_schema
    # Inputs the runner materializes and passes in: dataset names, or
    # "findings:<schema_slug>" for recorded findings flattened to rows.
    inputs: list[str] = Field(default_factory=list)
    timeout_seconds: float = 60.0


class PackManifest(BaseModel):
    pack: str
    version: str
    display_name: str
    description: str = ""
    frameworks: list[str] = Field(default_factory=list)
    doctrine: list[str] = Field(default_factory=list)
    task_types: list[TaskType] = Field(default_factory=list)
    datasets: list[PackDataset] = Field(default_factory=list)
    methods: list[PackMethod] = Field(default_factory=list)
