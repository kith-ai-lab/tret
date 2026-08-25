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


class HarnessPreset(BaseModel):
    """A ready-to-run workspace `Harness` this pack creates at install time
    (`packs/loader.py::install_pack`), alongside its dataset seeding — see
    that function's own docstring for the idempotency rule (skip if a
    non-archived harness with this `name` already exists in the workspace).

    Cross-referencing checks — `tools` against the engine's builtin tools
    (exactly like `TaskType.tools`), `task_types` against this same
    manifest's own `task_types` slugs, and `suggested_cost_tier` against the
    router's cost-tier vocabulary — are validated in `loader.validate_pack`,
    not here: the same split `TaskType` already uses for its `tools`/
    `terminal_tool`/`doctrine` fields, since checking any of them needs
    either `get_builtin_tools()` (an engine import deliberately kept out of
    this schema module — see loader.py's own `TYPE_CHECKING` note) or the
    manifest's other fields, neither of which a single model's own
    `field_validator` can see.
    """

    # Stripped, and required non-empty (min length 1) *after* stripping: this
    # becomes the created `Harness.name` verbatim (loader.py::install_pack),
    # which is also the idempotency key a re-install or upgrade install
    # matches against — leading/trailing whitespace a pack author didn't
    # intend would otherwise make two presets that read as "the same name"
    # collide, or fail to collide, unpredictably. The 200-char ceiling
    # (checked post-strip too) mirrors the practical bound
    # `api/harnesses.py::HarnessBody.name` leaves implicit; a pack preset gets
    # the same ceiling explicitly since a manifest is less reviewed than an
    # operator's own form input.
    name: str
    description: str | None = None
    # Task type slug(s) this harness is scoped to. `Harness.task_profile`
    # (db/models.py) holds exactly one slug, so a preset may name at most
    # one — `loader.validate_pack` rejects more than one, rather than
    # silently picking one entry off a multi-entry list. Empty (the default)
    # installs with `task_profile="freeform"`, the same default ordinary
    # harness creation uses (see api/harnesses.py::HarnessBody).
    task_types: list[str] = Field(default_factory=list)
    tools: list[str]
    suggested_cost_tier: str | None = None

    @field_validator("name")
    @classmethod
    def _name_stripped_non_empty_and_bounded(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("harness preset name must not be blank")
        if len(v) > 200:
            raise ValueError("harness preset name must be at most 200 characters")
        return v


class PackManifest(BaseModel):
    pack: str
    version: str
    display_name: str
    description: str = ""
    # Provenance/marketplace metadata. All optional and free-text (`license` is
    # SPDX-shaped by convention, not validated as one — a marketplace review
    # step is where that gets enforced, not every self-hosted install). None of
    # this is read by the engine at run time; it exists to be shown back to a
    # human deciding whether to install or trust a pack. Absent on every
    # existing fixture, so nothing already installed needs updating.
    author: str | None = None
    license: str | None = None
    homepage: str | None = None
    tags: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    doctrine: list[str] = Field(default_factory=list)
    task_types: list[TaskType] = Field(default_factory=list)
    datasets: list[PackDataset] = Field(default_factory=list)
    methods: list[PackMethod] = Field(default_factory=list)
    harnesses: list[HarnessPreset] = Field(default_factory=list)
