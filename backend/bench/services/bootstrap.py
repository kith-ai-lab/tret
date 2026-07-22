"""First-boot seeding: admin user, default workspace + sample project, packs.

Idempotent — safe to run on every startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import bootstrap_admin
from bench.config import get_settings
from bench.db.models import Harness, Pack, Project, Workspace
from bench.packs.loader import PackValidationError, install_pack

log = logging.getLogger("bench.bootstrap")


async def bootstrap(db: AsyncSession) -> None:
    await bootstrap_admin(db)

    workspace = (await db.execute(select(Workspace))).scalars().first()
    if workspace is None:
        workspace = Workspace(name="Default")
        db.add(workspace)
        await db.flush()

    project = (await db.execute(select(Project))).scalars().first()
    if project is None:
        project = Project(
            workspace_id=workspace.id,
            name="Sample Engagement",
            description="Seeded demo project with the climate-risk sample data.",
        )
        db.add(project)
        await db.flush()
    await db.commit()

    # Auto-install packs found in the packs dir.
    settings = get_settings()
    for packs_root in settings.packs_dir.split(":"):
        root = Path(packs_root)
        if not root.is_dir():
            continue
        for pack_dir in sorted(root.iterdir()):
            if not (pack_dir / "pack.yaml").exists():
                continue
            try:
                pack = await install_pack(db, pack_dir, workspace.id, project.id)
                log.info("pack ready: %s@%s", pack.slug, pack.version)
            except PackValidationError as e:
                log.error("pack %s failed validation: %s", pack_dir.name, e.errors)

    # Seed default harnesses.
    existing = (await db.execute(select(Harness))).scalars().first()
    if existing is None:
        climate = (
            await db.execute(select(Pack).where(Pack.slug == "climate-risk"))
        ).scalars().first()
        db.add(
            Harness(
                workspace_id=workspace.id,
                pack_id=None,
                name="General Assistant",
                description="Freeform analyst assistant with document and dataset tools.",
                task_profile="freeform",
                model_policy={"mode": "auto", "max_cost_tier": "standard"},
                tool_names=["read_document", "search_documents", "lookup_dataset", "list_prior_findings"],
            )
        )
        if climate is not None:
            db.add(
                Harness(
                    workspace_id=workspace.id,
                    pack_id=climate.id,
                    name="Climate Analyst",
                    description="Doctrine-driven climate risk assessment (divergence verdicts, "
                    "evidence extraction, TCFD drafting, QA).",
                    task_profile="divergence_assessment",
                    model_policy={"mode": "auto", "max_cost_tier": "premium"},
                )
            )
        await db.commit()
