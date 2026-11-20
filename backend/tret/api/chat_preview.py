"""R19: serve one HTML block of a chat reply as its own sandboxed document.

The app's global CSP gives a `srcdoc` iframe `script-src 'self'`, so the tabs and
buttons in a member's HTML deck never run inline. Instead the SPA frames (or
opens) this URL:

    GET /api/chat/{conversation_id}/preview?message=<int>&block=<int>

The response carries its own CSP with the `sandbox` directive (opaque origin,
scripts allowed, no network, no forms), so the deliverable's scripts never run
with the app's origin even when the URL is opened top-level. The block is read
server-side from the stored assistant message; nothing from the query string is
ever echoed into the document.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, project_in_workspace
from tret.db.engine import get_db
from tret.db.models import Conversation, User

router = APIRouter(prefix="/api/chat", tags=["chat"])

PREVIEW_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)

# The renderer (frontend/src/components/shared/MarkdownDoc.tsx) is the source of
# truth for which fences are HTML blocks, and the preview/save endpoints must
# index blocks the way it does. Its rules, mirrored exactly:
#   open   `^\s*```+\s*([\w+-]*)\s*$` on the right-trimmed line: any indent, 3+
#          backticks, and an info string that is ONE word of [A-Za-z0-9_+-]
#          (so "```html title=x" is not a fence at all);
#   close  the next line that is only backticks (3+, any indent, any length,
#          whatever the opener's length was);
#   an unclosed final fence runs to the end of the text;
#   a block is HTML when its word is html/htm, case-insensitively;
#   a reply that begins with a full HTML document is one block (its trimmed text).
_FENCE_OPEN = re.compile(r"\s*```+\s*([A-Za-z0-9_+-]*)\s*")
_FENCE_CLOSE = re.compile(r"\s*```+\s*")
_DOC_START = re.compile(r"\s*(<!doctype\s+html[^>]*>|<html[\s>])", re.IGNORECASE)


def html_blocks(content: str) -> list[str]:
    """The HTML blocks of a reply, in order: fenced ```html/```htm blocks, or the
    whole content as block 0 when the reply is itself an HTML document."""
    content = content or ""
    if _DOC_START.match(content):
        return [content.strip()]
    lines = re.sub(r"\r\n?", "\n", content).split("\n")
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.fullmatch(lines[i].rstrip())
        i += 1
        if not m:
            continue
        is_html = m.group(1).lower() in ("html", "htm")
        body: list[str] = []
        while i < len(lines) and not _FENCE_CLOSE.fullmatch(lines[i].rstrip()):
            body.append(lines[i])
            i += 1
        i += 1  # the closing fence (or past the end for an unclosed one)
        if is_html:
            blocks.append("\n".join(body))
    return blocks


@router.get("/{conversation_id}/preview")
async def preview_html_block(
    conversation_id: uuid.UUID,
    message: int = Query(ge=0),
    block: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    messages = conv.messages or []
    if message >= len(messages) or messages[message].get("role") != "assistant":
        raise HTTPException(404, "Message not found")
    blocks = html_blocks(messages[message].get("content") or "")
    if block >= len(blocks):
        raise HTTPException(404, "HTML block not found")
    return Response(
        content=blocks[block],
        media_type="text/html",
        headers={
            "Content-Security-Policy": PREVIEW_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            # SecurityHeadersMiddleware adds `X-Frame-Options: DENY` to any
            # response lacking one, which would stop the app framing this
            # page. Setting our own keeps it (and the CSP) from being touched.
            "X-Frame-Options": "SAMEORIGIN",
        },
    )
