"""Child process of the Stage 1 normaliser (`tret/services/normalise.py`).

Run as `python -I -m tret.services.normalise_worker <mode> ... --out <json file>`.
The parent starts it with a wall-clock timeout and an RSS guard and kills it past
either, so what lives here is exactly the work that can be big or hostile:

* `docling --pdf P --pages 3,9,12`: copy those physical pages into a small PDF (pypdf),
  convert it with docling (OCR off, CPU, 2 threads) and write `{"pages": {"3": markdown,
  ...}, "docling_version": ...}`. Importing docling is lazy and only happens here.
* `pypdf --pdf P`: the pypdf text of every page (the fallback when Poppler is missing or
  disagrees with the page tree), here so an untrusted PDF never opens in the server process.
* `esef --zip P`: read the report inside an ESEF package with
  `esef_filing.extract_filing_text` and write its Markdown.

Nothing here touches the database or the network; the environment the parent hands
over is an allowlist (PATH, HOME and the model-cache variables), never credentials.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path


def convert_pages(pdf_path: str, pages: list[int]) -> dict:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        reader.decrypt("")
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])

    import importlib.metadata

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.accelerator_options = AcceleratorOptions(num_threads=2, device=AcceleratorDevice.CPU)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    with tempfile.TemporaryDirectory(prefix="tret-chunk-") as tmp:
        chunk = Path(tmp) / "chunk.pdf"
        with open(chunk, "wb") as fh:
            writer.write(fh)
        document = converter.convert(str(chunk)).document
    return {"pages": export_pages(document, pages), "docling_version": importlib.metadata.version("docling")}


def export_pages(document, pages: list[int]) -> dict[str, str]:
    """Physical page number -> Markdown. The chunk PDF holds `pages` in order, so docling's
    page 1 is `pages[0]`; `export_to_markdown(page_no=i)` renders only that page."""
    return {str(physical): document.export_to_markdown(page_no=i) for i, physical in enumerate(pages, start=1)}


def read_pypdf(pdf_path: str) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        reader.decrypt("")
    return {"pages": [(page.extract_text() or "") for page in reader.pages]}


def read_esef(zip_path: str) -> dict:
    from tret.services import esef_filing as ef
    from tret.services.normalise import render_filing_markdown

    with zipfile.ZipFile(zip_path) as zf:
        ef.refuse_oversized_archive(zf)
        choice = ef.select_report_member(zf)
        stream = ef.MemberStream(zf, choice.name)
        # No unit cap: Document.extracted_text stops at 2,000,000 characters, this does not.
        filing = ef.extract_filing_text(stream, max_chars=10**10)
    return {
        "markdown": render_filing_markdown(filing.units),
        "layout": filing.layout,
        "units_total": filing.units_total,
        "unit_texts": [u.text for u in filing.units],
        "source_text_capped": bool(filing.structure.get("source_text_capped")),
        "report_member": choice.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalise_worker")
    sub = parser.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("docling")
    d.add_argument("--pdf", required=True)
    d.add_argument("--pages", required=True, help="comma-separated physical page numbers")
    y = sub.add_parser("pypdf")
    y.add_argument("--pdf", required=True)
    e = sub.add_parser("esef")
    e.add_argument("--zip", required=True)
    for p in (d, y, e):
        p.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.mode == "docling":
        pages = [int(x) for x in args.pages.split(",") if x.strip()]
        result = convert_pages(args.pdf, pages)
    elif args.mode == "pypdf":
        result = read_pypdf(args.pdf)
    else:
        result = read_esef(args.zip)
    Path(args.out).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
