import { useQuery } from '@tanstack/react-query'
import { Fragment, type ReactNode, useEffect, useState } from 'react'

import { api } from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'

export function Packs() {
  const packsQuery = useQuery({ queryKey: ['packs'], queryFn: api.listPacks })
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const packs = packsQuery.data ?? []

  useEffect(() => {
    if (packs.length > 0 && !packs.some((p) => p.id === selectedId)) {
      setSelectedId(packs[0].id)
    }
  }, [packs, selectedId])

  return (
    <div>
      <h1 className="view-title">Packs</h1>
      <div className="view-sub">Installed domain packs: doctrine, task types, and schemas.</div>

      {packsQuery.isLoading ? (
        <div className="empty pulse">Loading packs…</div>
      ) : packs.length === 0 ? (
        <div className="empty">No packs installed — install one via the CLI or packs API.</div>
      ) : (
        <ListDetail
          listWidth={220}
          list={packs.map((p) => (
            <ListItem
              key={p.id}
              active={p.id === selectedId}
              onClick={() => setSelectedId(p.id)}
              title={p.display_name}
              sub={`${p.slug} v${p.version}`}
            />
          ))}
          detail={selectedId ? <PackDetailPane packId={selectedId} /> : null}
        />
      )}
    </div>
  )
}

function PackDetailPane({ packId }: { packId: string }) {
  const packQuery = useQuery({ queryKey: ['pack', packId], queryFn: () => api.getPack(packId) })
  const [doctrineTab, setDoctrineTab] = useState<string | null>(null)

  const pack = packQuery.data

  useEffect(() => {
    if (pack && pack.doctrine_files.length > 0) {
      setDoctrineTab((curr) => (curr && pack.doctrine_files.includes(curr) ? curr : pack.doctrine_files[0]))
    }
  }, [pack])

  if (packQuery.isLoading) return <div className="empty pulse">Loading pack…</div>
  if (packQuery.isError) return <div className="error-text">{(packQuery.error as Error).message}</div>
  if (!pack) return null

  return (
    <div className="stack">
      <div>
        <h2 className="view-title" style={{ marginBottom: 2 }}>
          {pack.display_name}
        </h2>
        <div className="view-sub" style={{ marginBottom: 0 }}>
          {pack.description}
        </div>
      </div>

      <div className="config-stats">
        <div className="config-stat">
          <div className="mono-label">Version</div>
          <div className="mono-body">{pack.version}</div>
        </div>
        <div className="config-stat">
          <div className="mono-label">Doctrine SHA</div>
          <div className="mono-body" title={pack.doctrine_sha}>
            {pack.doctrine_sha.slice(0, 12)}
          </div>
        </div>
        <div className="config-stat">
          <div className="mono-label">Frameworks</div>
          <div className="row" style={{ gap: 6 }}>
            {pack.frameworks.length === 0
              ? '—'
              : pack.frameworks.map((f) => (
                  <span key={f} className="chip">
                    {f}
                  </span>
                ))}
          </div>
        </div>
        <div className="config-stat">
          <div className="mono-label">Schemas</div>
          <div className="mono-body">{Object.keys(pack.schemas).length}</div>
        </div>
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Task types
        </div>
        <table className="mono-table">
          <thead>
            <tr>
              <th>Slug</th>
              <th>Shape</th>
              <th>Terminal tool</th>
              <th>Tools</th>
            </tr>
          </thead>
          <tbody>
            {pack.task_types.map((t) => (
              <tr key={t.slug}>
                <td>{t.slug}</td>
                <td>
                  <span className="chip">{t.shape ?? '—'}</span>
                </td>
                <td>{t.terminal_tool ?? '—'}</td>
                <td style={{ color: 'var(--text-muted)' }}>{(t.tools ?? []).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Doctrine
        </div>
        {pack.doctrine_files.length === 0 ? (
          <div className="empty">This pack ships no doctrine files.</div>
        ) : (
          <>
            <div className="tabs">
              {pack.doctrine_files.map((rel) => (
                <button
                  key={rel}
                  className={`tab${doctrineTab === rel ? ' active' : ''}`}
                  onClick={() => setDoctrineTab(rel)}
                >
                  {rel.replace(/^doctrine\//, '')}
                </button>
              ))}
            </div>
            {doctrineTab && (
              <div className="panel md" style={{ maxHeight: 520, overflowY: 'auto' }}>
                <Markdown source={pack.doctrine_contents[doctrineTab] ?? '(file unavailable)'} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ── Minimal hand-rolled markdown: headings, lists, bold, inline code ─────

function inline(text: string): ReactNode[] {
  // Split on **bold** and `code` spans.
  const out: ReactNode[] = []
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let match: RegExpExecArray | null
  let key = 0
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index))
    const token = match[0]
    if (token.startsWith('**')) {
      out.push(<strong key={key++}>{token.slice(2, -2)}</strong>)
    } else {
      out.push(<code key={key++}>{token.slice(1, -1)}</code>)
    }
    last = regex.lastIndex
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export function Markdown({ source }: { source: string }) {
  const lines = source.split('\n')
  const blocks: ReactNode[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  let paragraph: string[] = []
  let key = 0

  const flushList = () => {
    if (!list) return
    const items = list.items.map((item, i) => <li key={i}>{inline(item)}</li>)
    blocks.push(list.ordered ? <ol key={key++}>{items}</ol> : <ul key={key++}>{items}</ul>)
    list = null
  }

  const flushParagraph = () => {
    if (paragraph.length === 0) return
    blocks.push(<p key={key++}>{inline(paragraph.join(' '))}</p>)
    paragraph = []
  }

  for (const raw of lines) {
    const line = raw.trimEnd()
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)

    if (heading) {
      flushList()
      flushParagraph()
      const level = heading[1].length
      const content = inline(heading[2])
      if (level === 1) blocks.push(<h1 key={key++}>{content}</h1>)
      else if (level === 2) blocks.push(<h2 key={key++}>{content}</h2>)
      else if (level === 3) blocks.push(<h3 key={key++}>{content}</h3>)
      else blocks.push(<h4 key={key++}>{content}</h4>)
    } else if (bullet) {
      flushParagraph()
      if (!list || list.ordered) {
        flushList()
        list = { ordered: false, items: [] }
      }
      list.items.push(bullet[1])
    } else if (numbered) {
      flushParagraph()
      if (!list || !list.ordered) {
        flushList()
        list = { ordered: true, items: [] }
      }
      list.items.push(numbered[1])
    } else if (line.trim() === '') {
      flushList()
      flushParagraph()
    } else {
      flushList()
      paragraph.push(line.trim())
    }
  }
  flushList()
  flushParagraph()

  return <Fragment>{blocks}</Fragment>
}
