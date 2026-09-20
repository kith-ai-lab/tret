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
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
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
    # Instance-wide role: gates only /api/auth/users* (instance user management).
    # A user's role *within a workspace* lives on WorkspaceMember instead — see
    # api/auth.py::UserOut, where this becomes `global_role`.
    role: Mapped[str] = mapped_column(Text, nullable=False, default="analyst")  # admin|analyst|approver
    # OIDC subject claim (`sub`), unique per issuer-less-single-IdP deployment.
    # Null for a password-only user; set on first OIDC login/link (Phase B).
    oidc_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    # Bumped to invalidate every outstanding session without touching the
    # credential itself — the `POST /api/auth/users/{id}/revoke-sessions` lever,
    # and the second half of `credential_version` alongside `password_hash`. An
    # OIDC-only user has no password to rotate, so this is that account's only
    # revocation mechanism.
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Whether the account may authenticate at all. Replaces "password_hash is
    # None" as the deactivation signal (see api/auth.py) — an OIDC user can be
    # disabled without ever having had a password to clear.
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_col()


# Workspace membership roles, ranked weakest to strongest. Kept here (not just
# in api/workspace.py) because it is schema-adjacent vocabulary the membership
# table's `role` column is constrained to, the same way Run.status values are
# documented beside the column rather than only where they are checked.
WORKSPACE_ROLES = ("analyst", "approver", "admin", "owner")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 'team' (the default — any workspace with >1 potential member) or
    # 'personal' (auto-created per user in multi-tenant mode, permanently
    # single-member by founder decision — see docs/plan). Self-host's sole
    # workspace is 'team' with one member, which is exactly today's shape.
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="team")
    # Set only on a 'personal' workspace: the user it belongs to. UNIQUE enforces
    # one personal workspace per user. Null on every 'team' workspace.
    personal_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), unique=True
    )
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()


class WorkspaceMember(Base):
    """Who belongs to a workspace, and at what role.

    Composite PK rather than a surrogate id: membership is the (user, workspace)
    pair itself, there is at most one row per pair, and nothing else ever
    references a membership by its own identity.
    """

    __tablename__ = "workspace_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, default="analyst")  # WORKSPACE_ROLES
    created_at: Mapped[datetime] = created_at_col()


class Invite(Base):
    """A pending (or resolved) invitation to join a workspace.

    Phase A ships the table so the migration and the model exist together;
    Phase C (workspaces.py) is what creates, lists, revokes and redeems rows
    here. `token` is the redemption secret — opaque, unguessable, and never the
    row's id, so listing invites never leaks the value a link carries.
    """

    __tablename__ = "invites"
    __table_args__ = (
        Index("ix_invites_workspace_id", "workspace_id"),
        Index("ix_invites_email", "email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="analyst")  # WORKSPACE_ROLES
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # pending | accepted | revoked | expired
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
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


class DraftPack(Base):
    """An in-app-built pack before it is a real, installed `Pack` — the
    workspace-scoped scratch state behind the builder (Plan Phase D, in-app
    pack builder). Never read by the engine at run time: a draft only ever
    becomes a directory on disk (`tret.packs.draft.materialize_draft`) for
    the three actions that need one — validate, test-install, export — and is
    otherwise a plain JSONB row.

    `manifest_json` mirrors `PackManifest.model_dump()` (the same shape
    `pack.yaml` parses to), authored directly by the builder's editors rather
    than read off a file. `methods` must always be empty here — a draft may
    never carry executable method code; `api/pack_builder.py`'s PATCH handler
    is where that is enforced, by rejecting a non-empty `manifest_json["methods"]`.
    That check alone would not be enough, though: `files` maps a pack-relative
    path to its content — a `str` for text (markdown doctrine, JSON schemas,
    CSV datasets) or `{"b64": "..."}` for binary content, `tret.packs.draft`
    is the one place both shapes are read — and a `files["pack.yaml"]` entry
    could otherwise smuggle a whole replacement manifest, methods included,
    straight past the `manifest_json` check. `tret.packs.draft.validate_draft_relpath`
    rejects that key by name (`RESERVED_ROOT_NAMES`) for exactly this reason,
    and `materialize_draft`/`build_draft_archive` refuse to let it win even if
    one somehow reached this row some other way.

    `test_install_seq` is the per-draft counter behind `{version}+draft.{n}`
    (`api/pack_builder.py`'s test-install action) — incremented, not reset,
    on every test-install so two successive ones never collide on the
    `(workspace_id, slug, version)` unique constraint `Pack` already has.
    """

    __tablename__ = "draft_packs"
    __table_args__ = (Index("ix_draft_packs_workspace_id", "workspace_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    files: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    test_install_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class Harness(Base):
    __tablename__ = "harnesses"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    # Which packs this harness can draw on lives in `harness_packs`, not here —
    # a harness may link zero (generic), one, or several packs. Deliberately no
    # ORM relationship onto that table: every reader goes through
    # `tret.packs.links` (`packs_for_harness`/`pack_map_for_harnesses`), which
    # loads in position order and batches across many harnesses — a lazy
    # relationship here is exactly the per-harness N+1 query that helper exists
    # to prevent, and async SQLAlchemy raises on an unawaited lazy load anyway.
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
    # Set once — the moment `services.workspace.seed_chat_harness` first
    # links any pack to this harness (on creation, or on a one-time
    # backfill) — and never touched again after that. Its presence, not the
    # harness's current link count, is what tells the seed "the default has
    # already been applied here": a NULL means it never has (backfill it),
    # a set value means an operator's own link state — including a
    # deliberate unlink-all — must be left alone. Only meaningful for a
    # `task_profile == "chat"` harness; NULL forever on every other one.
    packs_linked_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class HarnessPack(Base):
    """One (harness, pack) link, ordered. Replaces the old single nullable
    `Harness.pack_id` — a harness may now draw on several packs at once.

    `position` orders the links ascending; position 0 is the harness's
    *primary* pack (`tret.packs.links.resolve_pack_for_task`'s fallback for
    chat/freeform, and what a legacy single-`pack_id` API caller gets back).
    Composite PK rather than a surrogate id: the link itself is the (harness,
    pack) pair, and nothing references a link by its own identity. `ON DELETE
    CASCADE` on `harness_id` so archiving-then-deleting a harness never leaves
    orphaned link rows; a pack delete goes through `api/packs.py`'s own
    reference check instead (a pack still linked by an active harness 409s
    before any row is touched), so there is deliberately no cascade on
    `pack_id`.
    """

    __tablename__ = "harness_packs"
    __table_args__ = (Index("ix_harness_packs_pack_id", "pack_id"),)

    harness_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("harnesses.id", ondelete="CASCADE"), primary_key=True
    )
    pack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("packs.id"), primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class Conversation(Base):
    """A chat thread. Each user turn executes as a Run (task_type='chat');
    messages here are the display/history record, with run ids per turn."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    harness_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("harnesses.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="New conversation")
    # entries: {role, content, run_id?, ts, activity?: [{tool, summary}]}. Note
    # what is NOT here: a delegated tool call's `activity` entry carries only
    # `tool`/`summary` (api/chat.py::_assistant_message) — the child run's id
    # never lands in this JSONB. It exists only in the transient
    # `delegation_started`/`delegation_finished` SSE events and in the
    # delegating run's OWN `runs.messages` (the tool result `run_harness_task`
    # returns, engine/tools.py). Do not add a `child_run_id` field here without
    # also writing it in `_assistant_message` — a doc comment promising a shape
    # nothing writes is how the 9ba228f09f91 migration's dead backfill branch
    # happened.
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


class DocumentChunk(Base):
    """One structure-aware retrieval unit sliced out of `Document.extracted_text`
    by `tret.services.retrieval` — the chunked, ranked replacement for scanning
    the whole document with `str.find()`.

    Written lazily by `services/retrieval.py::ensure_chunks` the first time a
    document with no chunks yet is searched — an older document, or one
    ingested moments ago, look identical here. Rows are write-once: nothing is
    ever updated in place, only inserted (and cascade-deleted with their
    document), which is what lets `search_vector` below be populated at insert
    time instead of needing a real trigger-maintained generated column.

    `locator` is what makes a hit resolvable back into the source document
    without re-searching: `{"heading_path": [str, ...], "page": int|None}` for
    prose split on headings, `{"table_index": int, "row_start": int, "row_end":
    int, "page": int|None}` for a table kept whole or split into row blocks,
    `{"sheet": str, "row_start": int, "row_end": int}` for a spreadsheet row
    block. `context` is the short contextual prefix (document title + section/
    sheet/page) prepended per Anthropic's contextual-retrieval pattern, kept as
    its own column rather than baked into `body` so ranking can weight it and a
    citation can show it without repeating it inside the body text. `token_est`
    is a `len(text) // 4`-style estimate, not a real tokenizer count — good
    enough to size a chunk against the ~250-400 target, not for billing.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
        Index("ix_document_chunks_document_id", "document_id"),
        # A plain-column GIN index (not a functional/expression one) so it
        # reflects and autogenerate-compares identically to the JSONB GIN index
        # on `findings.subject` above — `postgresql_using` is a dialect-specific
        # kwarg that other dialects' DDL compiler simply drops, so sqlite's
        # `create_all` (the test/dev fallback) still creates a plain index here.
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # text | table | sheet_block
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    token_est: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Postgres only, in effect: `Text` is the type on every other dialect (via
    # `with_variant`) so sqlite's `create_all` never sees a type it cannot
    # create, but it is never read or written outside the postgresql branch of
    # `services/retrieval.py` — sqlite's ranking path is a small in-Python BM25
    # over `context`/`body` instead. Populated at chunk-insert time with
    # `to_tsvector('english', context || ' ' || body)`, not a DDL `GENERATED
    # ALWAYS AS` column: chunk rows are write-once (see class docstring), so an
    # app-populated column can never go stale the way it could on a table with
    # in-place updates, and staying a plain column keeps it fully representable
    # in `Base.metadata` — a real generated column's expression lives only in
    # the database and would show up as permanent drift against the models in
    # tests/test_migrations_postgres.py's autogenerate check.
    search_vector: Mapped[str | None] = mapped_column(Text().with_variant(TSVECTOR(), "postgresql"))
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
    __table_args__ = (
        Index("runs_project_created", "project_id", "created_at"),
        # The spend-by-conversation rollup (api/analytics.py::_named_conversation_rows,
        # ::_null_conversation_row, ::_conversation_spend_totals) filters on
        # (project_id, created_at) exactly like the index above and groups by
        # conversation_id — this composite serves the filter and the grouping
        # directly instead of falling back to the bare index above plus a sort.
        # Not an index-only scan: the rollup also selects input_tokens,
        # output_tokens, cost_usd and reported_cost_usd, none of which are in
        # this index, so Postgres still visits the heap for those. Leading on
        # project_id, not conversation_id, because every caller of this rollup
        # already has a project/workspace to scope by and none looks up a
        # conversation's runs without one.
        Index("runs_project_conversation_created", "project_id", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    harness_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("harnesses.id"), nullable=False)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packs.id"))
    # The chat turn (or delegation chain off one) that produced this run — see
    # Conversation.messages' docstring for how a turn's run_id and a delegated
    # tool call's child_run_id both land here. Nullable *permanently*, not a
    # migration artifact awaiting a backfill-everything follow-up: a workbench
    # run started from `POST /api/runs`, a run kicked off by a scheduled task,
    # and any other run that never began as a chat turn genuinely belongs to no
    # conversation, and NOT NULL would have nothing honest to put there. Do not
    # "tighten" this later — read a null here as "not a chat run", never as
    # missing data. `ondelete="SET NULL"`: nothing deletes a Conversation
    # today, but a run outliving the conversation that caused it is exactly
    # the same "not a chat run" state as never having one — deleting the
    # conversation must not cascade into deleting (or blocking deletion of)
    # the runs it caused.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # Lineage for a run spawned by `run_harness_task` (engine/tools.py). Both
    # FKs point back into this same table, so both `ondelete="SET NULL"`: a
    # deleted ancestor must not cascade into deleting (or blocking deletion
    # of) the runs it delegated to — the same "outliving its cause" posture
    # `conversation_id` above takes toward a deleted Conversation. Indexed:
    # both are the join key for "this run's whole delegation tree" (see
    # api/runs.py's `/{run_id}/children` and the `tree` block on run detail).
    #
    # The run whose delegation tool call created this run. Null for a run a
    # person, a schedule, or the plain `POST /api/runs` API started directly.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    # The top-level ancestor of a delegated run, denormalized off the parent
    # chain so a whole tree's totals are one indexed query instead of a
    # recursive walk. Null on the root itself — a root's tree is therefore
    # `id == X OR root_run_id == X`, never `root_run_id == X` alone.
    root_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    # "task" (a specialist pack task) or "subagent" (an ad-hoc brief) — which
    # shape of `run_harness_task` call created this run. Null for a
    # non-delegated run.
    delegation_kind: Mapped[str | None] = mapped_column(Text)
    # Shared by every child spawned from one parallel-delegation call, so
    # those siblings can be grouped without relying on `created_at` proximity.
    # No FK: this identifies a batch, not a row — there is no single `runs`
    # row it points at. Null for a non-delegated run, or a delegation made
    # one at a time rather than in parallel.
    delegation_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
    # Best-known actual cost, summed per turn: the provider-reported actual
    # where the provider gives one (OpenRouter only today, via its
    # `cost`/`cost_details.upstream_inference_cost` usage fields — see
    # providers/openai_compat.py — including a genuine 0 for :free models),
    # otherwise that turn's catalog price. A run that switches providers
    # mid-run (e.g. starts on OpenRouter, falls back to Anthropic) is not
    # under-counted just because the second provider stays silent. Nullable
    # with no default: null means this run has accrued no cost at all yet,
    # not that the actual was zero. `cost_usd` above remains the pure
    # catalog-priced figure used for routing and cost caps; this column is
    # the one billing should charge against.
    reported_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # What this run has caused OTHER runs to spend through delegation
    # (`run_harness_task`, engine/tools.py): for each finished child,
    # `child.cost_usd + child.delegated_cost_usd`, added on as that child
    # finishes. Recursive by construction, so it is correct at any delegation
    # depth without walking the tree. Kept as its own column rather than
    # folded into `cost_usd` above because `cost_usd` must stay this run's
    # own model spend and nothing else — `api/analytics.py` sums `cost_usd`
    # straight across every row in a window, and a parent that already
    # carried its children's spend in that column would double-count it the
    # moment analytics scanned both rows. A run's actual budget consumption
    # is `cost_usd + delegated_cost_usd`, checked against `loop_config.
    # max_cost_usd` in the engine.
    delegated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
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
    # Model calls this run made *about itself*: choosing its model (the router)
    # and summarizing what compaction elided. Both cost money and electricity and
    # were previously unmetered entirely — `complete_json` discarded the usage
    # block. Deliberately NOT folded into `cost_usd` / `energy_wh` above: these
    # calls run on a different model, often at a different provider, so their
    # energy class and grid basis are their own, and `cost_usd` is already
    # exposed through the runs API, exports and deliverable provenance — changing
    # what it means would rewrite the meaning of every historical value.
    # {"calls": [...], "total_cost_usd": ..., "accounting": {...}}
    overhead: Mapped[dict | None] = mapped_column(JSONB)
    # The prose grounding check's verdict on this run's final reply
    # (engine/grounding.py): {"checked", "status" ("clean"|"repaired"|
    # "unresolved"|"skipped"), "attempts", "unsupported", "first_unsupported"}.
    # "skipped" (checked: False) means the check applies to this task type but
    # never ran: no tool call was made and nothing was retrieved, so there was
    # no evidence to check the reply against (`run_has_retrieval_evidence`) —
    # distinct from "clean", which means it ran and found nothing wrong. Null
    # for any run the check does not apply to at all — anything but a
    # chat/freeform run with no terminal tool (a terminal tool's numbers are
    # already held to engine/validation.py's cited-values cross-check).
    grounding: Mapped[dict | None] = mapped_column(JSONB)
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


class PackLesson(Base):
    """A pack's durable "lessons" memory, scoped per workspace: what an agent
    learned running this pack here, curated by a human before it ever
    reaches a prompt again — the same blessing gate `findings`/`approvals`
    use, applied to the pack's own working notes instead of an output.

    Written two ways: `propose_pack_lesson` (engine/tools.py) inserts a
    `proposed` row tied to the run that suggested it; a workspace approver
    (or higher) then reviews it via `api/lessons.py`, which is the only path
    to `approved` or `rejected` — a model can never bless its own proposal.
    `retired` is a separate, admin-only step for a lesson that was once
    approved but no longer holds; it never reverts to `proposed`.

    Keyed on `(workspace_id, pack_slug)`, not `(workspace_id, pack_id)`: a new
    pack version is a new `Pack` row (`packs.slug`/`version` is the unique
    key), and a harness's link is repointed to it on every install — a lesson
    tied to the old row's id would silently vanish from every run's prompt on
    the next version bump, which defeats the entire point of a *durable*
    memory. `pack_slug` is what a lesson is actually about; `pack_id` is kept
    only as provenance — which install first produced it — and is nullable
    with `ondelete="SET NULL"` so deleting a superseded `Pack` row clears that
    provenance without cascading away the lessons themselves.

    `ordinal` is assignment order within a (workspace, pack_slug) — a
    monotonic counter, not a priority — and is what
    `services/lessons.py::approved_lessons` sorts and caps by, so the
    numbered list a run sees is stable across reads even as later lessons are
    proposed. `text` is capped at 600 characters (enforced in the service,
    not here) to keep the whole memory small enough to stay in the doctrine
    block's stable prefix — see that module's docstring for the cap on the
    assembled list itself.
    """

    __tablename__ = "pack_lessons"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "pack_slug", "ordinal", name="uq_pack_lessons_workspace_slug_ordinal"
        ),
        Index("ix_pack_lessons_workspace_slug", "workspace_id", "pack_slug"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    pack_slug: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance only — see the class docstring. Never joined on to resolve
    # "this pack's lessons"; `pack_slug` is what every query keys on.
    pack_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("packs.id", ondelete="SET NULL")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL")
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    reviewed_at: Mapped[datetime | None] = mapped_column()


class RunOutcome(Base):
    """How a finished run turned out — the evidence the router learns from.

    Derived, never authoritative: every column here is a summary of something
    already recorded on `runs`, `findings` and `approvals`, and this table can be
    dropped and rebuilt from those at any time (`tret outcomes backfill`). It
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
    # label tret can generate, because it is a within-task comparison rather
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
    # The upstream endpoint that actually served this segment
    # (`ModelSegment.served_by`, read from OpenRouter's `openrouter_metadata`
    # or, for Anthropic direct, the constant `SERVED_BY = "anthropic"` set on
    # every `TurnComplete`; see engine/harness.py and providers/anthropic.py).
    # Null only for a local run (no upstream to disambiguate) and every row
    # recorded before this column existed — never guessed. Exists so priors
    # can tell a bad quantized endpoint apart from a bad model instead of
    # averaging the two together.
    served_by: Mapped[str | None] = mapped_column(Text)
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


class WorkspaceConnection(Base):
    """A workspace's OAuth link to an external file provider (Google Drive,
    Microsoft 365) — one stored refresh token per (workspace, provider), used
    by `services/connections.py::get_access_token` to mint short-lived access
    tokens for the picker/browse/import surfaces layered on top of this.

    `status='error'` freezes the connection the moment a refresh comes back
    `invalid_grant` (the user revoked access at the provider, or the refresh
    token expired) — `error_detail` carries why, and a workspace admin has to
    reconnect via `POST /api/connections/{provider}/authorize` to clear it,
    the same shape `ProviderCredential`-adjacent flows use elsewhere.
    """

    __tablename__ = "workspace_connections"
    __table_args__ = (UniqueConstraint("workspace_id", "provider"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # gdrive | m365
    account_label: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    granted_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Reserved for the import/browse UI (Phase 1): folders/sites the user
    # scoped this connection to. Always {} until that layer writes to it.
    selected_resources: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")  # active | error
    error_detail: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
    refreshed_at: Mapped[datetime | None] = mapped_column()


class ConnectionActivity(Base):
    """One user-legible event on a workspace's connection: a search, a
    materialize ("read"), a write-back upload (or its failure), a connect/
    disconnect, or an admin's resources change — `services/connections.py::
    record_connection_activity` is the sole writer, called from both the
    read side (`search_connected_files`, `materialize_connected_file`) and
    the write side (`upload_connected_file`), plus `api/connections.py`'s
    callback/disconnect/resources routes.

    Distinct from `EgressCall` below: that table is one row per outbound
    HTTP request in the `research` egress class (including denials) — the
    network-layer audit trail. This is the connections *feature's* own
    activity log, one row per action a person would recognise ("someone
    searched SharePoint", "a run wrote a file back"), regardless of how
    many Graph calls that action took underneath (the tret-subfolder
    list/create calls inside an upload get no row of their own — only the
    upload/upload_failed outcome does). No JSON column here on purpose:
    every field a caller might want to filter or display is its own
    column, not a payload some future reader has to know the shape of.
    """

    __tablename__ = "connection_activity"
    __table_args__ = (
        Index("ix_connection_activity_workspace_id", "workspace_id"),
        Index("ix_connection_activity_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # gdrive | m365
    # search | read | upload | upload_failed | connect | disconnect | resources
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL")
    )
    target: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class EgressCall(Base):
    """One outbound request in the `research` class: what a run reached for.

    Only research-class calls land here. Provider and catalog calls are counted
    in-process instead (`tret/net/audit.py` explains why: a row per model call
    would double a run's write volume to re-record what the run already persists
    in full, and the provider stream has no session to write it with).

    Denials are rows too, and the interesting ones. `reason` is the policy code
    from `tret/net/policy.py` — `host_not_allowed`, `private_address`,
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
