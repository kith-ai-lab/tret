/** A small, deliberately incomplete markdown renderer.
 *
 *  It is the app's only markdown renderer, used for three kinds of source: tret's
 *  own reference docs (the methodology dialog), pack-authored doctrine (the Packs
 *  viewer), and **model-authored** deliverable sections (the Deliverables
 *  preview). Written for the first; the other two replaced a second, weaker
 *  renderer that covered headings, paragraphs and lists only and printed a
 *  deliverable's section rules as literal `---` and any table as a wall of pipes.
 *
 *  Why not a library: this repo takes no new dependencies, and a full CommonMark
 *  implementation is a large amount of parsing surface.
 *
 *  **Safety — the property that must not regress.** Every piece of the document
 *  becomes a React text node or element. `dangerouslySetInnerHTML` appears nowhere
 *  in this file, so React escapes the content for us: the methodology doc's own
 *  `TRET_GRID_CO2E_G_PER_KWH=<your region>` shows up as that text rather than as
 *  an unknown element, and a `<script>` in a doc — or in a model-drafted section
 *  built from an uploaded PDF — renders as visible characters. Link targets are
 *  additionally filtered to http/https/mailto, so a `javascript:` URL cannot
 *  become a live anchor. This matters more, not less, now that the input can be
 *  model-authored: it is the same guarantee the backend's export path makes with
 *  `services/html_sanitize`.
 *
 *  **Scope**, checked against the sources rather than guessed: ATX headings
 *  (levels 1-4) and `---` setext H2, paragraphs with hard wraps, `-` bullets and
 *  `1.` numbered lists (both with indented continuation lines), GitHub pipe tables
 *  with a separator row, thematic breaks, single-level block quotes, fenced code
 *  blocks, inline code, bold, `*italic*`, and inline links. Not supported: nested
 *  lists, images, HTML blocks, reference links, and `_underscore italics_` — that
 *  last one is omitted on purpose, since these documents are full of `snake_case`
 *  identifiers that a naive underscore rule would silently turn into italics.
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

/** A thematic break: a line of nothing but three or more `-`, `*` or `_`. */
function isThematicBreak(line: string): boolean {
  return /^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)
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
  let counter = 0

  /** One block key, taken *before* the element is built.
   *
   *  Every block must call this exactly once, and must not read the counter
   *  again while assembling its children. Inlining `key++` into JSX does not
   *  work: the JSX transform passes `key` as an argument *after* the props
   *  object, so a `key++` inside the children is evaluated first and the two
   *  forms (`key={\`b${key++}\`}` vs `key={\`b${key}\`}` with the increment in
   *  the children) hand out the same number. That collided in practice — a
   *  paragraph followed by a heading both came out as `b7` — and React's
   *  duplicate-key warning is the least of it: it reuses one element's state for
   *  the other. */
  const nextKey = () => `b${counter++}`

  const flushList = () => {
    if (!list) return
    const k = nextKey()
    const items = list.items.map((item, i) => <li key={i}>{inline(item, `${k}-l${i}-`)}</li>)
    blocks.push(list.ordered ? <ol key={k}>{items}</ol> : <ul key={k}>{items}</ul>)
    list = null
  }

  const flushParagraph = () => {
    if (paragraph.length === 0) return
    const k = nextKey()
    // Hard wraps inside a paragraph are joins, as markdown says.
    blocks.push(<p key={k}>{inline(paragraph.join(' '), `${k}-p`)}</p>)
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
        <pre className="md-pre" key={nextKey()}>
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
      const tk = nextKey()
      blocks.push(
        <div className="md-table-wrap" key={tk}>
          <table className="mono-table md-table">
            <thead>
              <tr>
                {header.map((cell, c) => (
                  <th key={c}>{inline(cell, `${tk}-th${c}-`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {/* Pad short rows rather than dropping cells: the doc has a
                      few rows with an intentionally empty trailing cell. */}
                  {header.map((_, c) => (
                    <td key={c}>{inline(row[c] ?? '', `${tk}-td${r}-${c}-`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // ── thematic break, or the setext heading it would otherwise swallow ──
    // The deliverable export separates every section with `\n---\n`, so this is
    // not optional decoration: without it the assembled preview printed literal
    // dashes between sections. A dash rule directly under paragraph text is a
    // setext H2 in markdown, and that is honoured rather than turned into a rule
    // that eats the title.
    if (isThematicBreak(line)) {
      if (paragraph.length > 0 && /^\s{0,3}-{3,}\s*$/.test(line)) {
        const text = paragraph.join(' ')
        paragraph = []
        flushList()
        const sk = nextKey()
        blocks.push(<h2 key={sk}>{inline(text, `${sk}-h`)}</h2>)
        continue
      }
      flushAll()
      blocks.push(<hr key={nextKey()} />)
      continue
    }

    // ── block quote ──
    // Model-authored deliverable sections quote source documents, and this
    // renderer now shows their content (not just tret's own reference docs), so
    // a `>` line renders as a quote instead of as a visible angle bracket.
    // Consecutive quote lines join into one; nesting is not supported.
    const quote = /^\s{0,3}>\s?(.*)$/.exec(line)
    if (quote) {
      flushAll()
      const quoted: string[] = [quote[1]]
      while (i + 1 < lines.length) {
        const next = /^\s{0,3}>\s?(.*)$/.exec(lines[i + 1].trimEnd())
        if (!next) break
        quoted.push(next[1])
        i++
      }
      const qk = nextKey()
      blocks.push(
        <blockquote key={qk}>{inline(quoted.join(' ').trim(), `${qk}-q`)}</blockquote>,
      )
      continue
    }

    // ── headings ──
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    if (heading) {
      flushAll()
      const level = heading[1].length
      const k = nextKey()
      const content = inline(heading[2], `${k}-h`)
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
