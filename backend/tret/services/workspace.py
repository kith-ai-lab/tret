"""Workspace creation: the one place a new workspace's starting shape is
decided, whether that is self-host's boot-time Default workspace, a
multi-tenant user's personal workspace, or a team workspace (Phase C).

Extracted from services/bootstrap.py, which becomes a thin caller of this
plus a safety net (see bootstrap.py's docstring). Every call seeds the same
non-negotiable minimum — one project, the Chat Assistant and General
Assistant harnesses — so a workspace is always immediately usable. Demo
content (the climate-risk pack and its harness) is additional and opt-in by
default in multi-tenant mode: a paying tenant's fresh workspace should not
open on a stranger's sample project.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import Harness, Pack, Project, User, Workspace, WorkspaceMember
from tret.packs.loader import PackValidationError, install_pack

log = logging.getLogger("tret.workspace")

WORKSPACE_KINDS = ("team", "personal")


async def create_workspace(
    db: AsyncSession,
    name: str,
    *,
    kind: str = "team",
    owner: User | None = None,
    seed_demo_content: bool | None = None,
) -> Workspace:
    """Create a workspace, seed it, and (if `owner` is given) add them as its
    `owner` member.

    `kind='personal'` requires `owner` (personal workspaces are always tied to
    one user — `workspaces.personal_owner_id`, UNIQUE, is what makes "at most
    one personal workspace per user" a database guarantee rather than an
    application promise).

    `seed_demo_content`: None (the default) follows `TRET_MULTI_TENANT` — demo
    content ships unless multi-tenant mode is on, matching every self-hosted
    install today. True/False overrides that for a specific call (Phase C's
    "create a team workspace" endpoint always passes False; a future "give me
    a sample project" action could pass True even in multi-tenant mode).
    """
    if kind not in WORKSPACE_KINDS:
        raise ValueError(f"kind must be one of {WORKSPACE_KINDS}, got {kind!r}")
    if kind == "personal" and owner is None:
        raise ValueError("a personal workspace requires an owner")

    workspace = Workspace(
        name=name, kind=kind, personal_owner_id=owner.id if kind == "personal" else None
    )
    db.add(workspace)
    await db.flush()

    if owner is not None:
        db.add(WorkspaceMember(user_id=owner.id, workspace_id=workspace.id, role="owner"))

    demo = get_settings().multi_tenant is False if seed_demo_content is None else seed_demo_content

    project = Project(
        workspace_id=workspace.id,
        name="Sample Engagement" if demo else "General",
        description="Seeded demo project with the climate-risk sample data."
        if demo
        else "Default project.",
    )
    db.add(project)
    await db.flush()

    await seed_workspace_content(db, workspace.id, project.id, seed_demo_content=demo)
    await db.commit()
    return workspace


async def seed_workspace_content(
    db: AsyncSession, workspace_id, project_id, *, seed_demo_content: bool
) -> None:
    """The idempotent part of workspace seeding: packs + the standard
    harnesses. Safe to call on every boot against an already-seeded
    workspace (that is exactly what `bootstrap.bootstrap()` does for
    self-host's existing Default workspace — a pack added to
    `TRET_PACKS_DIR` after first boot still gets installed, and an upgraded
    install's chat harness still gains a newly added builtin tool), and it is
    what `create_workspace` calls right after creating a workspace's project.
    """
    if seed_demo_content:
        await _install_configured_packs(db, workspace_id, project_id)
    await _seed_chat_harness(db, workspace_id)
    await _seed_default_harnesses(db, workspace_id, seed_demo_content=seed_demo_content)


async def _install_configured_packs(db: AsyncSession, workspace_id, project_id) -> None:
    """Auto-install every pack found in TRET_PACKS_DIR into this workspace."""
    settings = get_settings()
    for packs_root in settings.packs_dir.split(":"):
        root = Path(packs_root)
        if not root.is_dir():
            continue
        for pack_dir in sorted(root.iterdir()):
            if not (pack_dir / "pack.yaml").exists():
                continue
            try:
                pack = await install_pack(db, pack_dir, workspace_id, project_id)
                log.info(
                    "pack ready: %s@%s (content %s)",
                    pack.slug,
                    pack.version,
                    (pack.content_hash or "unpinned")[:16],
                )
            except PackValidationError as e:
                log.error("pack %s failed validation and was NOT installed:", pack_dir.name)
                for error in e.errors:
                    log.error("  - %s", error)


async def _seed_chat_harness(db: AsyncSession, workspace_id) -> None:
    """The conversational front door. Every workspace gets exactly one."""
    chat_harness = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.task_profile == "chat"
            )
        )
    ).scalars().first()
    if chat_harness is not None:
        if "run_method" not in (chat_harness.tool_names or []):
            chat_harness.tool_names = [*chat_harness.tool_names, "run_method"]
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            pack_id=None,
            name="Chat Assistant",
            description="Conversational front door: answers directly from documents and "
            "datasets, and delegates structured work to specialist harnesses.",
            task_profile="chat",
            model_policy={"mode": "auto", "max_cost_tier": "standard"},
            tool_names=[
                "run_harness_task",
                "run_method",
                "read_document",
                "search_documents",
                "lookup_dataset",
                "list_prior_findings",
                "file_data_request",
            ],
            loop_config={"max_iterations": 16, "max_output_tokens": 4096, "temperature": 0.3},
        )
    )


async def _seed_default_harnesses(db: AsyncSession, workspace_id, *, seed_demo_content: bool) -> None:
    """General Assistant always; Climate Analyst only alongside demo content
    (it needs the climate-risk pack, which `_install_configured_packs` only
    installs when `seed_demo_content` is true)."""
    existing = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.task_profile != "chat"
            )
        )
    ).scalars().first()
    if existing is not None:
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            pack_id=None,
            name="General Assistant",
            description="Freeform analyst assistant with document and dataset tools.",
            task_profile="freeform",
            model_policy={"mode": "auto", "max_cost_tier": "standard"},
            tool_names=["read_document", "search_documents", "lookup_dataset", "list_prior_findings"],
        )
    )
    if not seed_demo_content:
        return
    climate = (
        await db.execute(
            select(Pack).where(Pack.workspace_id == workspace_id, Pack.slug == "climate-risk")
        )
    ).scalars().first()
    if climate is not None:
        db.add(
            Harness(
                workspace_id=workspace_id,
                pack_id=climate.id,
                name="Climate Analyst",
                description="Doctrine-driven climate risk assessment (divergence verdicts, "
                "evidence extraction, TCFD drafting, QA).",
                task_profile="divergence_assessment",
                model_policy={"mode": "auto", "max_cost_tier": "premium"},
            )
        )
