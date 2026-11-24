"""A seeded corpus for the evaluation engine tests: companies with an extraction of one module, the
module text in the blob store and a classification, so pools can be cut and tournaments played."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

from tret.db.models import (
    Batch,
    BatchTarget,
    CompanyClassification,
    DisclosureReport,
    Project,
    ReportExtraction,
    ReportModule,
)
from tret.evaluation.modules import load_module_set
from tret.services import peer_taxonomy as pt
from tret.storage import get_store, normalised_key
from sqlalchemy import select

MODULE = "physical_climate_risk"


def module_set() -> str:
    return load_module_set().ident


def text_for(name: str, ticker: str, strength: int, words: int) -> str:
    body = " ".join(f"measure{i}" for i in range(words))
    return f"{name} ({ticker}) describes its flood exposure. STRENGTH={strength}. {body} {name}'s plants are exposed."


def spec(i: int, *, div="10", cluster="food", section="C", country="US", fw=(), status="extracted", present=True,
         classify=True, batch=0, strength=None, words=None, workspace=None, cik=None, year=None, text=None,
         urls=(), company_name=None) -> dict:
    return dict(
        name=f"Zorblax{i} Industries", ticker=f"ZB{i}", cik=cik or str(900000 + i), year=year, text=text,
        urls=list(urls), company_name=company_name, div=div, cluster=cluster, section=section,
        country=country, fw=list(fw), status=status, present=present, classify=classify, batch=batch,
        strength=i if strength is None else strength, words=(30 + (i * 37) % 61) if words is None else words,
        workspace=workspace,
    )


def seed_corpus(world, specs: list[dict], *, batches: int = 1) -> SimpleNamespace:
    """Insert one report per spec. Returns the module ids (in spec order) and the batch ids."""

    async def go():
        out = SimpleNamespace(modules=[], batches=[], targets=[], extractions=[])
        async with world.factory() as db:
            project = (await db.execute(select(Project).where(Project.workspace_id == world.ws.id))).scalars().first()
            bs, classified = [], set()
            for b in range(batches):
                batch = Batch(id=uuid.uuid4(), workspace_id=world.ws.id, project_id=project.id, name=f"B{b}",
                              run_slug=f"b-{b}-{uuid.uuid4().hex[:6]}", profile="disclosure", status="completed",
                              settings={}, warnings=[])
                db.add(batch)
                bs.append(batch)
            await db.flush()
            for i, s in enumerate(specs):
                t = BatchTarget(id=uuid.uuid4(), collection_id=bs[s["batch"]].id, position=i, slug=f"zorblax{i}",
                                company=s["name"], market=s["country"], domicile_country=s["country"], label=s["name"],
                                status="written", identifiers={"cik": s["cik"], "ticker": s["ticker"]}, known_urls=s["urls"],
                                finding_ids=[], attempts=1, written_files=[], task_input={})
                db.add(t)
                await db.flush()
                doc = uuid.uuid4()
                key = normalised_key(doc, "1")
                await get_store().put(key, b"# report")
                rep = DisclosureReport(project_id=project.id, document_id=doc, batch_target_id=t.id, is_primary=True,
                                       status="normalised", markdown_key=key, markdown_chars=8, normaliser_version="1")
                db.add(rep)
                await db.flush()
                ext = ReportExtraction(workspace_id=world.ws.id, project_id=project.id, disclosure_report_id=rep.id,
                                       batch_target_id=t.id, extractor_version="1", module_set=module_set(),
                                       status=s["status"], metadata_json={"frameworks_cited": s["fw"], **({"report_year": s["year"]} if s["year"] else {})}, windows=1,
                                       issues=[], models=[])
                db.add(ext)
                await db.flush()
                tkey = f"eval-test/{ext.id}.txt"
                body = s["text"] or text_for(s["name"], s["ticker"], s["strength"], s["words"]).encode()
                await get_store().put(tkey, body)
                rm = ReportModule(extraction_id=ext.id, disclosure_report_id=rep.id, module=MODULE, present=s["present"],
                                  text_key=tkey, text_sha256=hashlib.sha256(body).hexdigest(), word_count=s["words"] + 12, claim_density=float(s["strength"]) % 5)
                db.add(rm)
                await db.flush()
                if s["classify"] and s["cik"] not in classified:
                    classified.add(s["cik"])
                    db.add(CompanyClassification(
                        workspace_id=world.ws.id, company_key_type="cik", company_key=pt.normalise_cik(s["cik"]),
                        nace_division=s["div"], cluster=s["cluster"], nace_section=s["section"], taxonomy_version="x",
                        source="manual", company_name=s["company_name"]))
                out.modules.append(rm.id)
                out.targets.append(t.id)
                out.extractions.append(ext.id)
            await db.commit()
            out.batches = [b.id for b in bs]
        return out

    return asyncio.run(go())
