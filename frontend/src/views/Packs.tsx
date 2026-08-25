import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import {
  api,
  ApiError,
  type InputFieldSchema,
  type Pack,
  type PackMethodRef,
  type RegistryPackVersion,
} from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { Modal } from '../components/shared/Modal'
import { type Column, MonoTable, QueryError } from '../components/shared/MonoTable'
import { PackBuilder } from './PackBuilder'

type PackTab = 'installed' | 'find' | 'create'
const TABS: { key: PackTab; label: string }[] = [
  { key: 'installed', label: 'Installed' },
  { key: 'find', label: 'Find' },
  { key: 'create', label: 'Create' },
]

export function Packs() {
  // `?tab=` carries the active tab in the URL — the closest this view gets to
  // deep-linkability without a real sub-route (router.tsx has no precedent for
  // one, and three fixed tabs do not earn a param-taking route each). A bad or
  // absent value falls back to Installed rather than 404ing on a bookmark.
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab')
  const tab: PackTab = TABS.some((t) => t.key === requested) ? (requested as PackTab) : 'installed'

  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canManage = ['owner', 'admin'].includes(meQuery.data?.role ?? '')

  const setTab = (next: PackTab) => setParams(next === 'installed' ? {} : { tab: next }, { replace: false })

  return (
    <div>
      <h1 className="view-title">Packs</h1>
      <div className="view-sub">
        Install, browse, and author domain packs: doctrine, task types, schemas, and methods.
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab${tab === t.key ? ' active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'installed' && <InstalledPacks canManage={canManage} />}
      {tab === 'find' && <FindPacks canManage={canManage} onInstalled={() => setTab('installed')} />}
      {tab === 'create' && <PackBuilder />}
    </div>
  )
}

// ── Shared: the "contains executable code" banner ────────────────────────
// Method-bearing packs get elevated scrutiny at review (Plan's own framing:
// "safety.py is a deterrent, not a sandbox"), so both surfaces that let
// someone decide to install or trust a pack carry the same loud callout —
// never folded quietly into a details table.

function MethodsBanner({ count, author }: { count: number | null; author?: string | null }) {
  if (count === 0) return null
  return (
    <div className="callout callout-warn">
      <span className="callout-title">Executable code</span>
      This pack contains {count === null ? '' : `${count} `}executable method
      {count === 1 ? '' : 's'} — vetted deterministic scripts, run only when the agent invokes them,
      never sandboxed, only deterred by a static scan.
      {author && <> Published by <strong>{author}</strong>.</>}
    </div>
  )
}

// ── Installed ──────────────────────────────────────────────────────────────

function InstalledPacks({ canManage }: { canManage: boolean }) {
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
      {packsQuery.isLoading ? (
        <div className="empty pulse">Loading packs…</div>
      ) : packsQuery.isError ? (
        <QueryError error={packsQuery.error} what="the installed packs" />
      ) : packs.length === 0 ? (
        <div className="empty">
          No packs installed — find one in the Find tab, or build one in Create.
        </div>
      ) : (
        <ListDetail
          listWidth={220}
          list={packs.map((p) => (
            <ListItem
              key={p.id}
              active={p.id === selectedId}
              onClick={() => setSelectedId(p.id)}
              title={p.display_name}
              sub={
                <>
                  {p.slug} v{p.version} <UpdateBadge slug={p.slug} installedVersion={p.version} compact />
                </>
              }
            />
          ))}
          detail={selectedId ? <PackDetailPane packId={selectedId} canManage={canManage} /> : null}
        />
      )}
    </div>
  )
}

/** "vN available" — computed client-side from the registry's own
 *  `latest_listed_version`, fetched lazily (only while the Installed tab, and
 *  so this component, is mounted) and cached per-slug. A registry that is
 *  disabled or unreachable (`_require_registry_enabled`'s 503, a network
 *  failure) fails this query silently — an installed pack's own page must
 *  never break because Find/Install happens to be down. */
function UpdateBadge({
  slug,
  installedVersion,
  compact = false,
}: {
  slug: string
  installedVersion: string
  compact?: boolean
}) {
  const summaryQuery = useQuery({
    queryKey: ['registry-summary', slug],
    queryFn: () => api.registryPackSummary(slug),
    retry: false,
    staleTime: 60_000,
  })
  if (!summaryQuery.isSuccess) return null
  // `latest_listed_version` is the listed MarketplacePackVersion row's id,
  // not a version string — resolve the actual "vN" against `versions`
  // (present on this per-slug summary) before comparing/showing it.
  const latestId = summaryQuery.data.latest_listed_version
  const latest = summaryQuery.data.versions?.find((v) => v.id === latestId)?.version
  if (!latest || latest === installedVersion) return null
  return (
    <span
      className="badge badge-blue"
      style={compact ? { fontSize: 9.5, padding: '1px 6px', marginLeft: 6 } : undefined}
      title={`The marketplace registry lists v${latest} of this pack — you have v${installedVersion} installed.`}
    >
      v{latest} available
    </span>
  )
}

function PackDetailPane({ packId, canManage }: { packId: string; canManage: boolean }) {
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
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="view-title" style={{ marginBottom: 2 }}>
            {pack.display_name}
          </h2>
          <div className="view-sub" style={{ marginBottom: 0 }}>
            {pack.description}
          </div>
        </div>
        <UninstallButton pack={pack} canManage={canManage} />
      </div>

      <PackMethodsBanner packId={packId} author={pack.author} />

      <div className="config-stats">
        <div className="config-stat">
          <div className="mono-label">Version</div>
          <div className="mono-body">
            {pack.version} <UpdateBadge slug={pack.slug} installedVersion={pack.version} />
          </div>
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
          <div className="mono-label">Author / license</div>
          <div className="mono-body">
            {pack.author ?? '—'}
            {pack.license ? ` · ${pack.license}` : ''}
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

      {pack.homepage && (
        <div className="mono-body">
          {/* Only an http/https homepage becomes a live link — see MarkdownDoc's
              own safeHref for the same allowlist-not-blocklist rule. */}
          {/^https?:\/\//i.test(pack.homepage.trim()) ? (
            <a href={pack.homepage.trim()} target="_blank" rel="noopener noreferrer">
              {pack.homepage}
            </a>
          ) : (
            <span title="Not an http/https link — shown as text only">{pack.homepage}</span>
          )}
        </div>
      )}

      {pack.tags.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          {pack.tags.map((t) => (
            <span key={t} className="chip">
              {t}
            </span>
          ))}
        </div>
      )}

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Task types
        </div>
        <div style={{ overflowX: 'auto' }}>
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

/** The methods-count half of `MethodsBanner`, reading the same
 *  `['pack-methods']` cache `PackMethods` below already populates — so an
 *  installed pack's banner counts its *real*, actually-installed methods
 *  rather than repeating a boolean flag. */
function PackMethodsBanner({ packId, author }: { packId: string; author: string | null }) {
  const methodsQuery = useQuery({ queryKey: ['pack-methods'], queryFn: api.listPackMethods })
  if (!methodsQuery.isSuccess) return null
  const count = methodsQuery.data.filter((m) => m.pack_id === packId).length
  return <MethodsBanner count={count} author={author} />
}

function UninstallButton({ pack, canManage }: { pack: Pack; canManage: boolean }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const deleteMutation = useMutation({
    mutationFn: () => api.deletePack(pack.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['packs'] })
      setOpen(false)
    },
  })

  if (!canManage) return null

  return (
    <>
      <button type="button" className="btn btn-sm btn-danger" onClick={() => setOpen(true)}>
        Uninstall
      </button>
      <Modal
        open={open}
        onClose={() => (deleteMutation.isPending ? undefined : setOpen(false))}
        title={`Uninstall ${pack.display_name}?`}
        footer={
          <>
            <button type="button" className="btn btn-sm" onClick={() => setOpen(false)} disabled={deleteMutation.isPending}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              {deleteMutation.isPending ? 'Uninstalling…' : 'Uninstall'}
            </button>
          </>
        }
      >
        <div className="mono-body">
          Removes the <code>{pack.slug}@{pack.version}</code> row and its extracted files from this
          workspace. Pack-seeded datasets are left in place. This is blocked while any harness, run,
          finding, or method run still references it.
        </div>
        {deleteMutation.isError && (
          // Verbatim: a 409 here names exactly what still references the pack.
          <div className="error-text" style={{ marginTop: 10 }}>
            {(deleteMutation.error as Error).message}
          </div>
        )}
      </Modal>
    </>
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

// ── Find ────────────────────────────────────────────────────────────────────

function FindPacks({ canManage, onInstalled }: { canManage: boolean; onInstalled: () => void }) {
  const [q, setQ] = useState('')
  const [tags, setTags] = useState('')
  const [framework, setFramework] = useState('')
  const [query, setQuery] = useState({ q: '', tags: '', framework: '' })
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)

  const searchQuery = useQuery({
    queryKey: ['registry-search', query],
    queryFn: () => api.registrySearch(query),
    retry: false,
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setQuery({ q: q.trim(), tags: tags.trim(), framework: framework.trim() })
  }

  const results = searchQuery.data?.items ?? []

  useEffect(() => {
    if (results.length > 0 && !results.some((p) => p.slug === selectedSlug)) {
      setSelectedSlug(results[0].slug)
    } else if (results.length === 0) {
      setSelectedSlug(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery.data])

  return (
    <div>
      <form onSubmit={submit} className="row" style={{ flexWrap: 'wrap', marginBottom: 16 }}>
        <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
          <label className="mono-label">Search</label>
          <input type="text" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. climate risk" />
        </div>
        <div className="field" style={{ marginBottom: 0, width: 200 }}>
          <label className="mono-label">Tags (comma-separated)</label>
          <input type="text" value={tags} onChange={(e) => setTags(e.target.value)} />
        </div>
        <div className="field" style={{ marginBottom: 0, width: 160 }}>
          <label className="mono-label">Framework</label>
          <input type="text" value={framework} onChange={(e) => setFramework(e.target.value)} />
        </div>
        <button type="submit" className="btn btn-primary">
          Search
        </button>
      </form>

      {searchQuery.isLoading ? (
        <div className="empty pulse">Searching the marketplace…</div>
      ) : searchQuery.isError ? (
        // Registry disabled (TRET_PACK_REGISTRY_URL unset -> 503, the
        // opt-in default for every self-hosted deployment) or unreachable
        // (502/timeout) — a friendly empty state either way, never a crash,
        // and never rendered as if Find were broken by user error. The two
        // read differently on purpose: "disabled" is an operator's own
        // choice, not an outage, so it gets the enabling hint instead of
        // "not reachable" phrasing that would read as something being down.
        <div className="empty">
          {(searchQuery.error as ApiError).status === 503 ? (
            <>The pack marketplace is disabled for this deployment — {(searchQuery.error as Error).message}</>
          ) : (
            <>The pack marketplace is not reachable right now — {(searchQuery.error as Error).message}</>
          )}
        </div>
      ) : results.length === 0 ? (
        <div className="empty">
          {query.q || query.tags || query.framework ? 'No packs matched that search.' : 'Search the marketplace to find packs to install.'}
        </div>
      ) : (
        <ListDetail
          listWidth={240}
          list={results.map((p) => (
            <ListItem
              key={p.slug}
              active={p.slug === selectedSlug}
              onClick={() => setSelectedSlug(p.slug)}
              title={p.display_name}
              // `latest_listed_version` is a version row id, not a "vN"
              // string a search row can show cheaply — the detail pane
              // resolves and shows the real version once selected.
              sub={p.slug}
            />
          ))}
          detail={
            selectedSlug ? (
              <FindDetailPane slug={selectedSlug} canManage={canManage} onInstalled={onInstalled} />
            ) : null
          }
        />
      )}
    </div>
  )
}

function FindDetailPane({
  slug,
  canManage,
  onInstalled,
}: {
  slug: string
  canManage: boolean
  onInstalled: () => void
}) {
  const summaryQuery = useQuery({
    queryKey: ['registry-summary', slug],
    queryFn: () => api.registryPackSummary(slug),
    retry: false,
  })
  // `latest_listed_version` is the listed version row's id, not a version
  // string — resolve it against this summary's own `versions` list.
  const latestId = summaryQuery.data?.latest_listed_version ?? null
  const version = summaryQuery.data?.versions?.find((v) => v.id === latestId)?.version ?? null

  const versionQuery = useQuery({
    queryKey: ['registry-version', slug, version],
    queryFn: () => api.registryPackVersion(slug, version!),
    enabled: !!version,
    retry: false,
  })

  const queryClient = useQueryClient()
  const installMutation = useMutation({
    mutationFn: () => api.registryInstall(slug, version!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['packs'] })
      onInstalled()
    },
  })

  if (summaryQuery.isLoading) return <div className="empty pulse">Loading pack…</div>
  if (summaryQuery.isError)
    return <div className="error-text">{(summaryQuery.error as Error).message}</div>
  if (!summaryQuery.data) return null
  if (!version) return <div className="empty">This pack has no listed version.</div>

  const detail: RegistryPackVersion | undefined = versionQuery.data

  return (
    <div className="stack">
      <div>
        <h2 className="view-title" style={{ marginBottom: 2 }}>
          {summaryQuery.data.display_name}
        </h2>
        <div className="view-sub" style={{ marginBottom: 0 }}>
          {summaryQuery.data.description}
        </div>
      </div>

      {/* Only `has_methods` is known before the version detail loads — the exact
          count (as shown on an already-installed pack) is not something the
          catalog promises, so this banner speaks generically until install. */}
      {detail?.has_methods && <MethodsBanner count={null} author={detail.manifest.author} />}

      {versionQuery.isLoading ? (
        <div className="empty pulse" style={{ padding: '8px 0' }}>
          Loading version {version}…
        </div>
      ) : versionQuery.isError ? (
        <div className="error-text">{(versionQuery.error as Error).message}</div>
      ) : detail ? (
        <>
          <div className="config-stats">
            <div className="config-stat">
              <div className="mono-label">Version</div>
              <div className="mono-body">{detail.version}</div>
            </div>
            <div className="config-stat">
              <div className="mono-label">Author / license</div>
              <div className="mono-body">
                {detail.manifest.author ?? '—'}
                {detail.manifest.license ? ` · ${detail.manifest.license}` : ''}
              </div>
            </div>
            <div className="config-stat">
              <div className="mono-label">Downloads</div>
              <div className="mono-body">{summaryQuery.data.download_count ?? '—'}</div>
            </div>
          </div>

          {detail.manifest.homepage && (
            <div className="mono-body">
              {/^https?:\/\//i.test(detail.manifest.homepage.trim()) ? (
                <a href={detail.manifest.homepage.trim()} target="_blank" rel="noopener noreferrer">
                  {detail.manifest.homepage}
                </a>
              ) : (
                <span title="Not an http/https link — shown as text only">{detail.manifest.homepage}</span>
              )}
            </div>
          )}

          <div className="row" style={{ gap: 16, flexWrap: 'wrap' }}>
            {(detail.manifest.frameworks ?? []).length > 0 && (
              <div className="row" style={{ gap: 6 }}>
                {detail.manifest.frameworks.map((f) => (
                  <span key={f} className="chip">
                    {f}
                  </span>
                ))}
              </div>
            )}
            {(detail.manifest.tags ?? []).length > 0 && (
              <div className="row" style={{ gap: 6 }}>
                {detail.manifest.tags.map((t) => (
                  <span key={t} className="chip" style={{ opacity: 0.75 }}>
                    {t}
                  </span>
                ))}
              </div>
            )}
          </div>

          {(detail.manifest.doctrine ?? []).length > 0 && (
            <div>
              <div className="mono-label" style={{ marginBottom: 6 }}>
                Doctrine preview
              </div>
              <div className="panel" style={{ maxHeight: 420, overflowY: 'auto' }}>
                {detail.manifest.doctrine.map((rel) => (
                  <div key={rel} style={{ marginBottom: 14 }}>
                    <div className="mono-label" style={{ marginBottom: 4, opacity: 0.7 }}>
                      {rel}
                    </div>
                    <MarkdownDoc
                      source={detail.manifest.doctrine_contents?.[rel] ?? '(not included in this listing)'}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {canManage ? (
            <div>
              <button
                type="button"
                className="btn btn-primary"
                disabled={installMutation.isPending}
                onClick={() => installMutation.mutate()}
              >
                {installMutation.isPending ? 'Installing…' : `Install v${detail.version}`}
              </button>
              {installMutation.isError && (
                <div className="error-text" style={{ marginTop: 8 }}>
                  {(() => {
                    const err = installMutation.error as ApiError
                    if (err.status === 409) return `Already installed: ${err.message}`
                    return err.message
                  })()}
                </div>
              )}
            </div>
          ) : (
            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              Installing requires the admin role in this workspace.
            </div>
          )}
        </>
      ) : null}
    </div>
  )
}
