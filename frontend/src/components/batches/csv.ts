// Small client-side helpers for the prompt batch form: an RFC-4180-ish CSV
// parser and the `{column}` template scanner. No dependencies.

export const MAX_ROWS = 500

export interface ParsedCsv {
  columns: string[]
  rows: Record<string, string>[]
  /** Problems that make the file unusable (shown in red). */
  problems: string[]
}

/** Parse CSV text: header row first; quoted fields may hold commas, newlines
 *  and doubled quotes (`""`); CRLF, LF and bare CR all end a record; a leading
 *  BOM is dropped; completely blank lines are skipped. Short rows are padded
 *  with empty strings and extra cells are ignored. */
export function parseCsv(text: string): ParsedCsv {
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
  const records: string[][] = []
  let record: string[] = []
  let field = ''
  let inQuotes = false
  let quotedField = false
  let unterminated = false

  const endField = () => {
    record.push(field)
    field = ''
    quotedField = false
  }
  const endRecord = () => {
    endField()
    // A line with a single empty, unquoted field is blank: skip it.
    if (!(record.length === 1 && record[0] === '')) records.push(record)
    record = []
  }

  for (let i = 0; i < src.length; i++) {
    const ch = src[i]
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        field += ch
      }
      continue
    }
    if (ch === '"' && field === '' && !quotedField) {
      inQuotes = true
      quotedField = true
    } else if (ch === ',') {
      endField()
    } else if (ch === '\r') {
      if (src[i + 1] === '\n') i++
      endRecord()
    } else if (ch === '\n') {
      endRecord()
    } else {
      field += ch
    }
  }
  if (inQuotes) unterminated = true
  if (field !== '' || quotedField || record.length > 0) endRecord()

  const problems: string[] = []
  if (unterminated) problems.push('A quoted field is never closed (missing closing ").')
  if (records.length === 0) {
    problems.push('The file is empty: the first row must be a header of column names.')
    return { columns: [], rows: [], problems }
  }

  const columns = records[0].map((h) => h.trim())
  if (columns.some((c) => c === '')) problems.push('The header row has an empty column name.')
  const dupes = columns.filter((c, i) => c !== '' && columns.indexOf(c) !== i)
  if (dupes.length > 0) {
    problems.push(`Duplicate column names: ${[...new Set(dupes)].join(', ')}.`)
  }

  const rows = records.slice(1).map((r) => {
    const obj: Record<string, string> = {}
    columns.forEach((c, i) => {
      if (c !== '') obj[c] = r[i] ?? ''
    })
    return obj
  })
  if (rows.length === 0) problems.push('The file has a header but no data rows.')
  if (rows.length > MAX_ROWS) {
    problems.push(`${rows.length} rows is over the limit of ${MAX_ROWS} per batch.`)
  }
  return { columns, rows, problems }
}

type Token =
  | { kind: 'text'; text: string }
  | { kind: 'field'; name: string }

/** Scan a template with `str.format` brace rules: `{{` and `}}` are literal
 *  braces, `{name}` is a placeholder. Anything else (a lone brace, `{}`) is a
 *  syntax problem. Names are taken verbatim between the braces, so
 *  `{a.b}` / `{a:x}` / `{a!r}` are reported as missing columns rather than
 *  silently accepted. */
function scanTemplate(template: string): { tokens: Token[]; problems: string[] } {
  const tokens: Token[] = []
  const problems: string[] = []
  let text = ''
  const flush = () => {
    if (text) tokens.push({ kind: 'text', text })
    text = ''
  }
  for (let i = 0; i < template.length; i++) {
    const ch = template[i]
    if (ch === '{') {
      if (template[i + 1] === '{') {
        text += '{'
        i++
        continue
      }
      const end = template.indexOf('}', i + 1)
      const inner = end === -1 ? null : template.slice(i + 1, end)
      if (inner === null || inner.includes('{')) {
        problems.push('A "{" is never closed; write {{ for a literal brace.')
        text += ch
        continue
      }
      if (inner.trim() === '') {
        problems.push('An empty placeholder {} is not allowed; name a column.')
        i = end
        continue
      }
      flush()
      tokens.push({ kind: 'field', name: inner.trim() })
      i = end
    } else if (ch === '}') {
      if (template[i + 1] === '}') {
        text += '}'
        i++
      } else {
        problems.push('A lone "}" is not allowed; write }} for a literal brace.')
        text += ch
      }
    } else {
      text += ch
    }
  }
  flush()
  return { tokens, problems: [...new Set(problems)] }
}

export function templatePlaceholders(template: string): string[] {
  const names = scanTemplate(template).tokens.flatMap((t) => (t.kind === 'field' ? [t.name] : []))
  return [...new Set(names)]
}

export function templateProblems(template: string): string[] {
  return scanTemplate(template).problems
}

/** The template with `{column}` replaced from `row` (missing -> empty string)
 *  and `{{` / `}}` collapsed to literal braces. */
export function renderTemplate(template: string, row: Record<string, string> | undefined): string {
  return scanTemplate(template)
    .tokens.map((t) => (t.kind === 'text' ? t.text : (row?.[t.name] ?? '')))
    .join('')
}

export function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
}
