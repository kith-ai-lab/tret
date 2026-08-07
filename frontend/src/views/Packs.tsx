import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, type InputFieldSchema, type PackMethodRef } from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { type Column, MonoTable, QueryError } from '../components/shared/MonoTable'

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
      ) : packsQuery.isError ? (
        <QueryError error={packsQuery.error} what="the installed packs" />
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
        {/* The integrity pin, so the tamper-evidence signal exists somewhere other
            than the HTTP response. Deliberately labelled "at install": it is a
            stored value, not a live re-hash of the directory, so it answers "is
            this the pack the author published?" and not "has anything changed
            since?". A pack installed before pinning landed says so — absence is
            not reassurance. */}
        <div className="config-stat">
          <div className="mono-label">Content hash (at install)</div>
          {pack.content_hash ? (
            <div
              className="mono-body"
              style={{ userSelect: 'all' }}
              title={`sha256 ${pack.content_hash} — recorded when this pack was installed. Compare it against the hash the pack author published (docs/pack-authoring.md). Not recomputed on this request, so it does not detect later edits on disk.`}
            >
              {pack.content_hash.slice(0, 12)}
            </div>
          ) : (
            <div
              className="mono-body"
              style={{ color: 'var(--amber)' }}
              title="This pack was installed before integrity pinning existed, so there is no recorded hash to compare against. Reinstall it to pin one."
            >
              not pinned
            </div>
          )}
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

      <PackMethods packId={packId} />

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
              <div className="panel" style={{ maxHeight: 520, overflowY: 'auto' }}>
                <MarkdownDoc source={pack.doctrine_contents[doctrineTab] ?? '(file unavailable)'} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ── Deterministic methods ─────────────────────────────────────────────────

function compactParams(schema: Record<string, InputFieldSchema> | undefined): string {
  const entries = Object.entries(schema ?? {})
  if (entries.length === 0) return '—'
  return entries
    .map(([field, spec]) => `${field}: ${spec.enum ? spec.enum.join('|') : (spec.type ?? 'string')}`)
    .join(' · ')
}

function PackMethods({ packId }: { packId: string }) {
  const methodsQuery = useQuery({ queryKey: ['pack-methods'], queryFn: api.listPackMethods })
  const methods = (methodsQuery.data ?? []).filter((m) => m.pack_id === packId)

  const columns: Column<PackMethodRef>[] = [
    { key: 'slug', header: 'Slug', render: (m) => <span style={{ whiteSpace: 'nowrap' }}>{m.slug}</span> },
    {
      key: 'name',
      header: 'Name',
      render: (m) => <span style={{ whiteSpace: 'nowrap' }}>{m.display_name ?? m.slug}</span>,
    },
    {
      key: 'desc',
      header: 'Description',
      render: (m) => (
        <span
          title={(m.description ?? '').trim()}
          style={{
            color: 'var(--text-muted)',
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
            minWidth: 220,
            maxWidth: 420,
          }}
        >
          {(m.description ?? '').trim()}
        </span>
      ),
    },
    {
      key: 'params',
      header: 'Params',
      render: (m) => (
        <span style={{ whiteSpace: 'nowrap' }}>
          {compactParams(m.params_schema)
            .split(' · ')
            .map((p, i) => (
              <span key={i} style={{ display: 'block' }}>
                {p}
              </span>
            ))}
        </span>
      ),
    },
    {
      key: 'inputs',
      header: 'Inputs',
      render: (m) => (
        <span className="row" style={{ gap: 4, display: 'inline-flex', flexWrap: 'wrap' }}>
          {(m.inputs ?? []).map((inp) => (
            <span key={inp} className="chip" style={{ fontSize: 10, padding: '1px 7px' }}>
              {inp}
            </span>
          ))}
        </span>
      ),
    },
  ]

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Methods
      </div>
      {methodsQuery.isLoading ? (
        <div className="empty pulse" style={{ padding: '6px 0' }}>
          Loading methods…
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <MonoTable
            columns={columns}
            rows={methods}
            rowKey={(m) => m.slug}
            error={methodsQuery.error}
            empty="This pack ships no deterministic methods."
          />
        </div>
      )}
      <div
        style={{
          marginTop: 8,
          fontFamily: 'var(--mono)',
          fontSize: 11,
          color: 'var(--text-muted)',
        }}
      >
        Vetted deterministic analytics — the agent invokes these with parameters; it never writes
        code. Every execution is manifest-pinned (params, code hash, output hash).
      </div>
    </div>
  )
}

// The hand-rolled renderer that used to live here is gone: it covered headings,
// paragraphs, bold and inline code only, so a pack doctrine table rendered as
// pipes and a deliverable's `---` section rules as literal dashes.
// components/shared/MarkdownDoc.tsx is the one renderer now — same no-innerHTML,
// scheme-allowlisted safety, plus tables, code fences, quotes and rules.
