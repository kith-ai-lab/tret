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
    # Where these bytes came from: 'upload' (a human put them here) or 'web' (an
    # agent fetched them). A column rather than a key in `meta` because it is a
    # trust tier, and a tier that only exists inside a JSON blob is a tier nobody
    # can filter on, index, or notice. Web documents are third-party text nobody
    # vetted: readable and quotable with attribution, never a source of numbers
    # (they are not registered in `retrieved_values`, so the cited-values check in
    # engine/validation.py still refuses anything that came from one).
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, default="upload")
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
    # Every time this run had to shrink what it sends the model to stay inside
    # its context window: [{iteration, before_est_tokens, after_est_tokens,
    # elided_messages, elided_tools, summarized, summarizer_model, ...}].
    # `messages` above is always the COMPLETE transcript — compaction changes
    # only what the provider was sent, never what was recorded — so this column
    # is what states the gap between the two. Null for runs that never compacted.
    compactions: Mapped[list | None] = mapped_column(JSONB)
    # Every model this run used, in order: [{model, provider, from_iteration,
    # to_iteration, tokens, cost_usd, energy_wh, energy_accounting, reason}].
    # Null for the ordinary single-model run.
    #
    # `model_used` above therefore means *the model that produced the final
    # answer*, not the only model involved. It has always been read that way by
    # the runs list and the chat chip; this column is what makes the fuller
    # story available, and what keeps `energy_accounting` honest — that block is
    # a roll-up across these segments (services/emissions.combine_accountings),
    # and its per-model factors are null wherever the segments disagreed.
    model_timeline: Mapped[list | None] = mapped_column(JSONB)
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


class RunOutcome(Base):
    """How a finished run turned out — the evidence the router learns from.

    Derived, never authoritative: every column here is a summary of something
    already recorded on `runs`, `findings` and `approvals`, and this table can be
    dropped and rebuilt from those at any time (`bench outcomes backfill`). It
    exists because routing needs to ask "how has this model done on this shape of
    task" cheaply, and answering that from raw transcripts means parsing every
    run on every route.

    One row per run, written when the run reaches a terminal status and rewritten
    when an approval later lands on one of its findings — a human verdict arrives
    minutes or days after the run ends, and it is the strongest signal there is
    (see router_llm/outcomes.py). Cancelled and still-running runs are not
    recorded at all: an operator pressing stop is not evidence about a model.

    The routing key is denormalised onto the row (shape, objective, cost tier,
    size band, model) rather than read back out of `runs.routing`, because that
    is exactly the tuple the priors group by, and a JSON predicate per group is
    the thing this table exists to avoid.
    """

    __tablename__ = "run_outcomes"
    __table_args__ = (
        Index("run_outcomes_routing_key", "task_shape", "objective", "model_id"),
        Index("run_outcomes_observed", "observed_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    # One row per model the run used, in order. A run that changed model
    # part-way produced evidence about *both* — and the pair is the strongest
    # label bench can generate, because it is a within-task comparison rather
    # than an average across different tasks: this model stalled on this
    # problem at this iteration, and that one finished it.
    #
    # Keyed on the position rather than on the model id, because a run may
    # return to a model it already used and the two stints are separate
    # evidence.
    segment_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    harness_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("harnesses.id"))

    # ── the routing key this outcome is evidence about ───────────────────────
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_shape: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    max_cost_tier: Mapped[str] = mapped_column(Text, nullable=False)
    size_band: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    # Whether this route came from the LLM router, the deterministic fallback, or
    # an override. A pinned model's record says nothing about the router's
    # judgment, so priors can exclude overrides from what they learn.
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override: Mapped[str | None] = mapped_column(Text)  # user_pin | run_override | null

    # ── the verdict ──────────────────────────────────────────────────────────
    # delivered | no_output | failed | handed_off | handed_off_capacity
    outcome_class: Mapped[str] = mapped_column(Text, nullable=False)
    quality_score: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    score_version: Mapped[str] = mapped_column(Text, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(Text)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # ── what it cost to get there ────────────────────────────────────────────
    # Kept separate from quality_score on purpose: what a route is worth is a
    # trade-off between these and the score, and the routing objective is what
    # makes that trade. Baking cost into the score would take the choice away.
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    energy_wh: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── the human record ─────────────────────────────────────────────────────
    findings_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_approved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The run's own creation time, copied so time-decay can weight evidence
    # without joining back to `runs` on every routing decision.
    observed_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


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


class EgressCall(Base):
    """One outbound request in the `research` class: what a run reached for.

    Only research-class calls land here. Provider and catalog calls are counted
    in-process instead (`bench/net/audit.py` explains why: a row per model call
    would double a run's write volume to re-record what the run already persists
    in full, and the provider stream has no session to write it with).

    Denials are rows too, and the interesting ones. `reason` is the policy code
    from `bench/net/policy.py` — `host_not_allowed`, `private_address`,
    `class_disabled` — so a rising count of one of them is legible without
    reading prose.

    Query strings are never stored (see audit.py): they carry API keys and the
    private half of a search term.
    """

    __tablename__ = "egress_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    egress_class: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_code: Mapped[int | None] = mapped_column(Integer)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(Text, nullable=False)  # allowed | denied
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_egress_calls_created_at", "created_at"),)
