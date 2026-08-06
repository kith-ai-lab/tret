/** A small, deliberately incomplete markdown renderer for bench's reference docs.
 *
 *  Why not a library: this repo takes no new dependencies, and a full CommonMark
 *  implementation is a large amount of parsing surface for one dialog. Why not
 *  the existing `Markdown` in views/Packs.tsx: it covers headings, paragraphs and
 *  lists only, and `docs/emissions-methodology.md` is mostly *tables* (88 rows of
 *  them), code fences and links — it would render as a wall of pipes.
 *
 *  **Safety.** Every piece of the document becomes a React text node or element.
 *  `dangerouslySetInnerHTML` appears nowhere in this file, so React escapes the
 *  content for us: the doc's own `BENCH_GRID_CO2E_G_PER_KWH=<your region>` shows
 *  up as that text rather than as an unknown element, and a `<script>` in a doc
 *  would render as visible characters. Link targets are additionally filtered to
 *  http/https/mailto, so a `javascript:` URL cannot become a live anchor.
 *
 *  **Scope is what the document actually uses**, checked against the file rather
 *  than guessed: ATX headings (levels 1-4), paragraphs with hard wraps, `-`
 *  bullets and `1.` numbered lists (both with indented continuation lines),
 *  GitHub pipe tables with a separator row, fenced code blocks, inline code,
 *  bold, `*italic*`, and inline links. Not supported, because the document
 *  contains none: nested lists, block quotes, images, HTML blocks, reference
 *  links, setext headings, and `_underscore italics_` — that last one is omitted
 *  on purpose, since the doc is full of `snake_case` identifiers that a naive
 *  underscore rule would silently turn into italics.
 */
import { type ReactNode, useMemo } from 'react'

// Ordered alternation, and the order is the precedence: a code span wins over
// everything inside it, a link wins over emphasis in its label, and `**bold**`
// must be tried before `*italic*` or the latter would eat the first asterisk.
//
// Kept as a *source string* rather than a shared RegExp on purpose: `inline`
// recurses into a link label and into emphasis, and a `/g` RegExp object carries
// `lastIndex` state. Sharing one instance across recursion levels rewinds the
// outer scan and loops forever — which it did, until a render test hung. Each
// call compiles its own.
const INLINE_SOURCE = '(`[^`]+`)|(\\[[^\\]\\n]+\\]\\([^)\\s]+\\))|(\\*\\*[^*]+\\*\\*)|(\\*[^*\\n]+\\*)'

/** Only these schemes may become a live anchor. A relative link (the doc points
 *  at its sibling docs) has no route in this app, so it renders as plain text
 *  naming the file instead of as a link that would 404. */
function safeHref(target: string): string | null {
  const trimmed = target.trim()
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  if (/^mailto:/i.test(trimmed)) return trimmed
  return null
}

/** Inline markup inside one line of text. Returns React children, never HTML. */
function inline(text: string, keyPrefix = ''): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  let match: RegExpExecArray | null
  // Own instance: recursion below must not disturb this scan's position.
  const re = new RegExp(INLINE_SOURCE, 'g')
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index))
    const token = match[0]
    const k = `${keyPrefix}i${key++}`
    if (token.startsWith('`')) {
      out.push(<code key={k}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('[')) {
      const split = token.indexOf('](')
      const label = token.slice(1, split)
      const href = safeHref(token.slice(split + 2, -1))
      out.push(
        href ? (
          // External by definition — every allowed scheme leaves the app.
          <a key={k} href={href} target="_blank" rel="noopener noreferrer">
            {inline(label, `${k}-`)}
          </a>
        ) : (
          <span key={k} title={`${token.slice(split + 2, -1)} — in the repository`}>
            {inline(label, `${k}-`)}
          </span>
        ),
      )
    } else if (token.startsWith('**')) {
      out.push(<strong key={k}>{inline(token.slice(2, -2), `${k}-`)}</strong>)
    } else {
      out.push(<em key={k}>{inline(token.slice(1, -1), `${k}-`)}</em>)
    }
    last = re.lastIndex
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

/** A pipe-table separator, e.g. `|---|---:|`. Its presence is what promotes the
 *  preceding line from a paragraph to a header row. */
function isTableSeparator(line: string): boolean {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line)
}

function tableCells(line: string): string[] {
  return line
    .replace(/^\s*\|/, '')
    .replace(/\|\s*$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

interface ListState {
  ordered: boolean
  items: string[]
}

/** Parse the document into blocks. One pass, no backtracking. */
function render(source: string): ReactNode[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let list: ListState | null = null
  let paragraph: string[] = []
  let key = 0

  const flushList = () => {
    if (!list) return
    const items = list.items.map((item, i) => <li key={i}>{inline(item, `l${key}-${i}-`)}</li>)
    blocks.push(
      list.ordered ? <ol key={`b${key++}`}>{items}</ol> : <ul key={`b${key++}`}>{items}</ul>,
    )
    list = null
  }

  const flushParagraph = () => {
    if (paragraph.length === 0) return
    // Hard wraps inside a paragraph are joins, as markdown says.
    blocks.push(<p key={`b${key}`}>{inline(paragraph.join(' '), `p${key++}-`)}</p>)
    paragraph = []
  }

  const flushAll = () => {
    flushList()
    flushParagraph()
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i]
    const line = raw.trimEnd()

    // ── fenced code ──
    const fence = /^\s*```+\s*([\w+-]*)\s*$/.exec(line)
    if (fence) {
      flushAll()
      const body: string[] = []
      i++
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) {
        body.push(lines[i])
        i++
      }
      blocks.push(
        // Scrolls on its own axis so a wide line cannot widen the dialog.
        <pre className="md-pre" key={`b${key++}`}>
          <code>{body.join('\n')}</code>
        </pre>,
      )
      continue
    }

    // ── tables: current line is a header row iff the next line is a separator ──
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      flushAll()
      const header = tableCells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(tableCells(lines[i]))
        i++
      }
      i-- // the loop's own i++ consumes the terminating line
      blocks.push(
        <div className="md-table-wrap" key={`b${key}`}>
          <table className="mono-table md-table">
            <thead>
              <tr>
                {header.map((cell, c) => (
                  <th key={c}>{inline(cell, `th${key}-${c}-`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {/* Pad short rows rather than dropping cells: the doc has a
                      few rows with an intentionally empty trailing cell. */}
                  {header.map((_, c) => (
                    <td key={c}>{inline(row[c] ?? '', `td${key}-${r}-${c}-`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      key++
      continue
    }

    // ── headings ──
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    if (heading) {
      flushAll()
      const level = heading[1].length
      const content = inline(heading[2], `h${key}-`)
      const k = `b${key++}`
      if (level === 1) blocks.push(<h1 key={k}>{content}</h1>)
      else if (level === 2) blocks.push(<h2 key={k}>{content}</h2>)
      else if (level === 3) blocks.push(<h3 key={k}>{content}</h3>)
      else blocks.push(<h4 key={k}>{content}</h4>)
      continue
    }

    // ── list items ──
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line)
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      flushParagraph()
      const ordered = Boolean(numbered)
      if (!list || list.ordered !== ordered) {
        flushList()
        list = { ordered, items: [] }
      }
      list.items.push((bullet ?? numbered)![1])
      continue
    }

    // ── blank line ends whatever is open ──
    if (line.trim() === '') {
      flushAll()
      continue
    }

    // ── continuation of an open list item, or paragraph text ──
    // The doc wraps long list items with a two- or three-space indent; treating
    // those as new paragraphs would break every bulleted section in it.
    if (list && list.items.length > 0 && /^\s+\S/.test(raw)) {
      list.items[list.items.length - 1] += ` ${line.trim()}`
      continue
    }
    flushList()
    paragraph.push(line.trim())
  }
  flushAll()
  return blocks
}

/** Renders markdown as escaped React elements. `className` defaults to the app's
 *  existing `.md` typography so this matches the doctrine viewer. */
export function MarkdownDoc({ source, className = 'md' }: { source: string; className?: string }) {
  const blocks = useMemo(() => render(source), [source])
  return <div className={className}>{blocks}</div>
}
