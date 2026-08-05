"""SQLAlchemy models. This schema encodes the trust doctrine:

- datasets/dataset_rows are the deterministic lane — numbers the model may only
  retrieve via tools, never compute.
- findings only reach status='approved' through an approvals row whose approver
  is stamped from the session.
- runs.routing persists the complete model-routing decision for audit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, list: JSONB, datetime: TIMESTAMP(timezone=True)}


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(default=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)  # argon2; null once OIDC lands
    role: Mapped[str] = mapped_column(Text, nullable=False, default="analyst")  # admin|analyst|approver
    created_at: Mapped[datetime] = created_at_col()


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class Pack(Base):
    __tablename__ = "packs"
    __table_args__ = (UniqueConstraint("workspace_id", "slug", "version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    doctrine_sha: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)  # sha256 of all pack files (null: legacy)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    installed_at: Mapped[datetime] = created_at_col()


class Harness(Base):
    __tablename__ = "harnesses"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packs.id"))  # null = generic
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt_extra: Mapped[str | None] = mapped_column(Text)
    task_profile: Mapped[str] = mapped_column(Text, nullable=False, default="freeform")
    # {"mode":"auto","allowed":[...],"max_cost_tier":"standard"} | {"mode":"pinned","model":"..."}
    # Optional "max_run_output_tokens": soft per-run output budget the engine
    # enforces between iterations (see engine/harness.py).
    model_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {"mode": "auto"})
    tool_names: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    loop_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2},
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class Conversation(Base):
    """A chat thread. Each user turn executes as a Run (task_type='chat');
    messages here are the display/history record, with run ids per turn."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    harness_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("harnesses.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="New conversation")
    # entries: {role, content, run_id?, ts, activity?: [{tool, summary, child_run_id?, finding_ids?}]}
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    pack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packs.id"))  # pack-seeded data
    name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class DatasetRow(Base):
    __tablename__ = "dataset_rows"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    row_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("runs_project_created", "project_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    harness_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("harnesses.id"), nullable=False)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packs.id"))
    doctrine_sha: Mapped[str | None] = mapped_column(Text)  # snapshot at run time
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    document_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    # queued | running | completed | completed_without_output | failed | cancelled.
    # `completed_without_output` is a success of the guardrails, not of the task:
    # the run ended on its own but never recorded the terminal result its task
    # type requires (see engine/harness.py::_completion_status).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    routing: Mapped[dict | None] = mapped_column(JSONB)  # full RoutingDecision
    model_used: Mapped[str | None] = mapped_column(Text)
    provider_used: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # input_tokens excludes the cache buckets, matching providers.base.Usage.
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    # Estimated energy drawn by this run, in watt-hours, with the full derivation
    # in energy_accounting ({"energy_wh","co2e_g","energy_class",
    # "energy_wh_per_mtok","grid_co2e_g_per_kwh","weighted_tokens",...}).
    # Nullable rather than 0: runs that predate ecological accounting have no
    # figure, and a zero would read as "this run was free", which is a lie.
    # Estimates throughout — see docs/eco-accounting.md.
    energy_wh: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    energy_accounting: Mapped[dict | None] = mapped_column(JSONB)
    # Estimated token breakdown of the context assembled at run start:
    # {"estimator","total_est_tokens","total_chars","by_kind","blocks":[...]}.
    # Makes spend legible per component (preamble, each doctrine file, task
    # instructions, output schema, tool specs). See engine/context.py.
    context_composition: Mapped[dict | None] = mapped_column(JSONB)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = created_at_col()


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (Index("findings_subject", "subject", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packs.id"))
    schema_slug: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, default="1")
    subject: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_at: Mapped[datetime] = created_at_col()


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # approve|reject
    # Stamped server-side from the session. There is deliberately no API field for this.
    approver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class DataRequest(Base):
    __tablename__ = "data_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    subject: Mapped[dict] = mapped_column(JSONB, nullable=False)
    what_is_missing: Mapped[str] = mapped_column(Text, nullable=False)
    why_needed: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = created_at_col()


class MethodRun(Base):
    """One execution of a pack-authored deterministic method.

    The manifest of the deterministic lane: params in, code hash, input
    summary, output rows + hash. Findings cite method outputs via row refs
    of the form `method/<slug>/<method_run_id>:<row>` — this table is what
    those references resolve to.
    """

    __tablename__ = "method_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    pack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("packs.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))  # invoking agent run
    method_slug: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    code_sha: Mapped[str] = mapped_column(Text, nullable=False)
    input_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    output_hash: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="completed")  # completed|failed
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)  # anthropic|kimi|openrouter
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
