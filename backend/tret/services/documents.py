"""Document text extraction: PDF (pypdf), DOCX (python-docx), CSV/MD/TXT."""
from __future__ import annotations

import csv
import io


def extract_text(filename: str, data: bytes) -> tuple[str, dict]:
    """Returns (text, meta). Raises ValueError for unsupported types."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages), {"pages": len(pages)}
    if lower.endswith(".docx"):
        import docx

        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts), {"paragraphs": len(document.paragraphs)}
    if lower.endswith((".csv", ".tsv")):
        delim = "\t" if lower.endswith(".tsv") else ","
        text = data.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        rendered = "\n".join(" | ".join(row) for row in rows)
        return rendered, {"rows": len(rows), "columns": rows[0] if rows else []}
    if lower.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
        return data.decode("utf-8", errors="replace"), {}
    raise ValueError(f"Unsupported file type: {filename} (supported: pdf, docx, csv, tsv, md, txt)")
