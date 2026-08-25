import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  api,
  ApiError,
  COST_TIERS,
  type DraftDetail,
  type DraftFileContent,
  type DraftManifest,
  type HarnessPreset,
  type InputFieldSchema,
  type MarketplaceSubmission,
  type PackDatasetRef,
  type TaskType,
} from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { formatDateTime } from '../components/shared/format'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { Modal } from '../components/shared/Modal'
import { type Column, MonoTable, QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

/** The in-app pack builder (Plan Phase D+E): drafts live in a `DraftPack` DB
 *  row, edited here as plain client state and PATCHed to the server on a
 *  750ms debounce. Validate/test-install/export all materialize the draft to
 *  a real directory server-side and run the unchanged CLI-grade `validate_pack`
 *  / `install_pack` against it — this editor never re-implements that logic,
 *  it only ever produces the same `manifest_json`/`files` shape a filesystem
 *  pack author would hand-write. */
export function PackBuilder() {
  const queryClient = useQueryClient()
  const draftsQuery = useQuery({ queryKey: ['pack-drafts'], queryFn: api.listDrafts })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const drafts = draftsQuery.data ?? []

  useEffect(() => {
    if (draftsQuery.data === undefined) return
    if (drafts.length > 0 && !drafts.some((d) => d.id === selectedId)) {
      setSelectedId(drafts[0].id)
    } else if (drafts.length === 0) {
      setSelectedId(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftsQuery.data, selectedId])

  const createMutation = useMutation({
    mutationFn: (slug: string) => api.createDraft(slug),
    onSuccess: (d) => {
      queryClient.invalidateQueries({ queryKey: ['pack-drafts'] })
      setSelectedId(d.id)
    },
  })

  const newDraft = () => {
    const slug = window.prompt('Slug for the new pack (lowercase, hyphenated — e.g. "grant-compliance")')
    if (slug && slug.trim()) createMutation.mutate(slug.trim())
  }

  return (
    <div className="stack" style={{ gap: 28 }}>
      {draftsQuery.isLoading ? (
        <div className="empty pulse">Loading drafts…</div>
      ) : draftsQuery.isError ? (
        <QueryError error={draftsQuery.error} what="your pack drafts" />
      ) : (
        <ListDetail
          listWidth={220}
          list={
            <>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                style={{ marginBottom: 10, width: '100%' }}
                onClick={newDraft}
                disabled={createMutation.isPending}
              >
                {createMutation.isPending ? 'Creating…' : '+ New draft'}
              </button>
              {createMutation.isError && (
                <div className="error-text" style={{ marginBottom: 8, fontSize: 11 }}>
                  {(createMutation.error as Error).message}
                </div>
              )}
              {drafts.length === 0 && (
                <div className="empty" style={{ padding: '8px 0' }}>
                  No drafts yet.
                </div>
              )}
              {drafts.map((d) => (
                <ListItem
                  key={d.id}
                  active={d.id === selectedId}
                  onClick={() => setSelectedId(d.id)}
                  title={d.display_name}
                  sub={`${d.slug}${d.version ? ` v${d.version}` : ''} · ${d.file_count} file${d.file_count === 1 ? '' : 's'}`}
                />
              ))}
            </>
          }
          detail={
            selectedId ? (
              <DraftEditorLoader key={selectedId} draftId={selectedId} onDeleted={() => setSelectedId(null)} />
            ) : (
              <div className="empty">Create a draft to start authoring a pack.</div>
            )
          }
        />
      )}

      <MySubmissionsPanel />
    </div>
  )
}

function DraftEditorLoader({ draftId, onDeleted }: { draftId: string; onDeleted: () => void }) {
  const draftQuery = useQuery({ queryKey: ['pack-draft', draftId], queryFn: () => api.getDraft(draftId) })
  if (draftQuery.isLoading) return <div className="empty pulse">Loading draft…</div>
  if (draftQuery.isError) return <div className="error-text">{(draftQuery.error as Error).message}</div>
  if (!draftQuery.data) return null
  return <DraftEditor draft={draftQuery.data} onDeleted={onDeleted} />
}

// ── Autosave ───────────────────────────────────────────────────────────────

type SaveState = 'saved' | 'pending' | 'saving' | 'error'

/** Diffs `manifest`/`files` against what was last confirmed saved and PATCHes
 *  the difference 750ms after the last edit. `manifest_json` is always sent
 *  whole (the backend replaces it wholesale — PatchDraftBody's own doc
 *  comment); `files` is sent as a sparse diff (`null` for a removed path),
 *  matching the backend's per-key merge exactly. */
function useDraftAutosave(
  draftId: string,
  manifest: DraftManifest,
  files: Record<string, DraftFileContent>,
): { state: SaveState; error: ApiError | null } {
  const queryClient = useQueryClient()
  const savedManifestJson = useRef(JSON.stringify(manifest))
  const savedFiles = useRef(files)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [state, setState] = useState<SaveState>('saved')
  const [error, setError] = useState<ApiError | null>(null)

  const saveMutation = useMutation({
    mutationFn: (body: { manifest_json?: DraftManifest; files?: Record<string, DraftFileContent | null> }) =>
      api.patchDraft(draftId, body),
  })

  useEffect(() => {
    const manifestJson = JSON.stringify(manifest)
    const manifestChanged = manifestJson !== savedManifestJson.current
    const fileDiff: Record<string, DraftFileContent | null> = {}
    for (const [path, content] of Object.entries(files)) {
      const prev = savedFiles.current[path]
      if (prev === undefined || JSON.stringify(prev) !== JSON.stringify(content)) {
        fileDiff[path] = content
      }
    }
    for (const path of Object.keys(savedFiles.current)) {
      if (!(path in files)) fileDiff[path] = null
    }
    const filesChanged = Object.keys(fileDiff).length > 0
    if (!manifestChanged && !filesChanged) return

    setState('pending')
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => {
      setState('saving')
      const body: { manifest_json?: DraftManifest; files?: Record<string, DraftFileContent | null> } = {}
      if (manifestChanged) body.manifest_json = manifest
      if (filesChanged) body.files = fileDiff
      saveMutation.mutate(body, {
        onSuccess: () => {
          savedManifestJson.current = manifestJson
          savedFiles.current = { ...files }
          setState('saved')
          setError(null)
          // Keeps the left-hand draft list's display_name/version/file_count
          // in sync without a full page refetch.
          queryClient.invalidateQueries({ queryKey: ['pack-drafts'] })
        },
        onError: (e) => {
          setState('error')
          setError(e as ApiError)
        },
      })
    }, 750)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manifest, files])

  return { state, error }
}

function SaveIndicator({ state, error }: { state: SaveState; error: ApiError | null }) {
  if (state === 'saved') return <span className="mono-label" style={{ color: 'var(--green)' }}>saved</span>
  if (state === 'pending') return <span className="mono-label">unsaved changes…</span>
  if (state === 'saving') return <span className="mono-label pulse">saving…</span>
  return <span className="error-text">save failed{error ? `: ${error.message}` : ''}</span>
}

// ── Draft editor shell ───────────────────────────────────────────────────

type BuilderSubTab = 'metadata' | 'doctrine' | 'tasks' | 'harnesses' | 'schemas' | 'datasets' | 'templates'

const SUB_TABS: { key: BuilderSubTab; label: string }[] = [
  { key: 'metadata', label: 'Metadata' },
  { key: 'doctrine', label: 'Doctrine' },
  { key: 'tasks', label: 'Task types' },
  { key: 'harnesses', label: 'Harnesses' },
  { key: 'schemas', label: 'Schemas' },
  { key: 'datasets', label: 'Datasets' },
  { key: 'templates', label: 'Templates' },
]

function DraftEditor({ draft, onDeleted }: { draft: DraftDetail; onDeleted: () => void }) {
  const queryClient = useQueryClient()
  // Owned locally from the moment the draft loads; `useDraftAutosave` below is
  // the only thing that ever talks to the server again for this draft, so
  // React Query re-fetching `['pack-draft', draft.id]` elsewhere can never
  // stomp on an in-progress edit.
  const [manifest, setManifest] = useState<DraftManifest>(draft.manifest_json)
  const [files, setFiles] = useState<Record<string, DraftFileContent>>(draft.files)
  const [subTab, setSubTab] = useState<BuilderSubTab>('metadata')
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { state: saveState, error: saveError } = useDraftAutosave(draft.id, manifest, files)

  // Doubles as the marketplace-submission capability probe: `/api/marketplace/*`
  // 404s wholesale where the cloud extension is not loaded, exactly like
  // `/api/billing/*` — a self-hosted build never shows "Submit". See
  // SubmitDraftResult's own doc comment for why the submit action itself
  // (POST-only) is not probed directly.
  const submissionsQuery = useQuery({ queryKey: ['my-submissions'], queryFn: api.mySubmissions, retry: false })

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteDraft(draft.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pack-drafts'] })
      onDeleted()
    },
  })

  const validateMutation = useMutation({ mutationFn: () => api.validateDraft(draft.id) })
  const testInstallMutation = useMutation({
    mutationFn: () => api.testInstallDraft(draft.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['packs'] }),
  })
  const submitMutation = useMutation({
    mutationFn: () => api.submitDraft(draft.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['my-submissions'] }),
  })

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="view-title" style={{ marginBottom: 2 }}>
            {manifest.display_name || draft.slug}
          </h2>
          <div className="view-sub" style={{ marginBottom: 0 }}>
            {manifest.pack} · v{manifest.version}
          </div>
        </div>
        <SaveIndicator state={saveState} error={saveError} />
        <button type="button" className="btn btn-sm btn-danger" onClick={() => setConfirmDelete(true)}>
          Delete draft
        </button>
      </div>

      <Modal
        open={confirmDelete}
        onClose={() => (deleteMutation.isPending ? undefined : setConfirmDelete(false))}
        title={`Delete draft "${draft.slug}"?`}
        footer={
          <>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setConfirmDelete(false)}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              {deleteMutation.isPending ? 'Deleting…' : 'Delete'}
            </button>
          </>
        }
      >
        <div className="mono-body">
          Permanently discards this draft's manifest and files. Anything already test-installed or
          submitted from it is unaffected.
        </div>
        {deleteMutation.isError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {(deleteMutation.error as Error).message}
          </div>
        )}
      </Modal>

      <div className="tabs">
        {SUB_TABS.map((t) => (
          <button
            key={t.key}
            className={`tab${subTab === t.key ? ' active' : ''}`}
            onClick={() => setSubTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {subTab === 'metadata' && <MetadataEditor manifest={manifest} setManifest={setManifest} />}
      {subTab === 'doctrine' && (
        <DoctrineEditor manifest={manifest} setManifest={setManifest} files={files} setFiles={setFiles} />
      )}
      {subTab === 'tasks' && <TaskTypesEditor manifest={manifest} setManifest={setManifest} files={files} />}
      {subTab === 'harnesses' && <HarnessesEditor manifest={manifest} setManifest={setManifest} />}
      {subTab === 'schemas' && <SchemasEditor files={files} setFiles={setFiles} />}
      {subTab === 'datasets' && (
        <DatasetsEditor manifest={manifest} setManifest={setManifest} files={files} setFiles={setFiles} />
      )}
      {subTab === 'templates' && <TemplatesEditor files={files} setFiles={setFiles} />}

      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Actions
        </div>
        <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
          <button
            type="button"
            className="btn"
            disabled={validateMutation.isPending}
            onClick={() => validateMutation.mutate()}
          >
            {validateMutation.isPending ? 'Validating…' : 'Validate'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={testInstallMutation.isPending}
            onClick={() => testInstallMutation.mutate()}
          >
            {testInstallMutation.isPending ? 'Installing…' : 'Test install'}
          </button>
          <a className="btn" href={api.draftExportUrl(draft.id)}>
            Export .tar.gz
          </a>
          {submissionsQuery.isSuccess && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={submitMutation.isPending}
              onClick={() => submitMutation.mutate()}
            >
              {submitMutation.isPending ? 'Submitting…' : 'Submit to marketplace'}
            </button>
          )}
        </div>

        {validateMutation.isError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {(validateMutation.error as Error).message}
          </div>
        )}
        {validateMutation.data && (
          <div
            className="panel"
            style={{
              marginTop: 10,
              borderColor: validateMutation.data.valid ? 'var(--green-border)' : 'var(--red-border)',
            }}
          >
            <div
              className="mono-body"
              style={{
                color: validateMutation.data.valid ? 'var(--green)' : 'var(--red)',
                marginBottom: validateMutation.data.errors.length > 0 ? 8 : 0,
              }}
            >
              {validateMutation.data.valid
                ? 'Valid'
                : `${validateMutation.data.errors.length} error${validateMutation.data.errors.length === 1 ? '' : 's'}`}
            </div>
            {validateMutation.data.errors.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18, fontFamily: 'var(--mono)', fontSize: 11.5 }}>
                {validateMutation.data.errors.map((e, i) => (
                  <li key={i} style={{ color: 'var(--red)' }}>
                    {e}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {testInstallMutation.isError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {(testInstallMutation.error as Error).message}
          </div>
        )}
        {testInstallMutation.data && (
          <div className="mono-body" style={{ marginTop: 10 }}>
            Installed as{' '}
            <code>
              {testInstallMutation.data.slug}@{testInstallMutation.data.version}
            </code>{' '}
            in this workspace — <Link to="/packs?tab=installed">view in Installed</Link>.
          </div>
        )}

        {submitMutation.isError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {(submitMutation.error as Error).message}
          </div>
        )}
        {submitMutation.data && (
          <div className="mono-body" style={{ marginTop: 10 }}>
            Submitted {submitMutation.data.pack_slug} v{submitMutation.data.version} —{' '}
            <StatusBadge status={submitMutation.data.state} /> (version{' '}
            {submitMutation.data.version_id.slice(0, 8)})
          </div>
        )}
      </div>
    </div>
  )
}

// ── My submissions ─────────────────────────────────────────────────────────

function MySubmissionsPanel() {
  const submissionsQuery = useQuery({ queryKey: ['my-submissions'], queryFn: api.mySubmissions, retry: false })
  // Cloud-only capability gate — any failure (404 on self-host, 403, a network
  // hiccup) hides this section entirely, same posture as BillingSection.
  if (!submissionsQuery.isSuccess) return null

  const columns: Column<MarketplaceSubmission>[] = [
    { key: 'pack', header: 'Pack', render: (s) => `${s.slug} v${s.version}` },
    { key: 'state', header: 'State', render: (s) => <StatusBadge status={s.state} /> },
    {
      key: 'notes',
      header: 'Review notes',
      render: (s) =>
        (s.state === 'changes_requested' || s.state === 'rejected') && s.review_notes ? (
          <span style={{ color: 'var(--text-muted)' }}>{s.review_notes}</span>
        ) : (
          '—'
        ),
    },
    { key: 'when', header: 'Submitted', render: (s) => formatDateTime(s.submitted_at) },
  ]

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        My submissions
      </div>
      <MonoTable columns={columns} rows={submissionsQuery.data} rowKey={(s) => s.id} empty="No submissions yet." />
    </div>
  )
}

// ── Metadata ───────────────────────────────────────────────────────────────

function MetadataEditor({
  manifest,
  setManifest,
}: {
  manifest: DraftManifest
  setManifest: (m: DraftManifest) => void
}) {
  const set = (patch: Partial<DraftManifest>) => setManifest({ ...manifest, ...patch })
  return (
    <div className="panel">
      <div className="field">
        <label className="mono-label">Pack slug</label>
        <input type="text" value={manifest.pack} onChange={(e) => set({ pack: e.target.value })} />
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mono-label">Version</label>
          <input type="text" value={manifest.version} onChange={(e) => set({ version: e.target.value })} placeholder="0.1.0" />
        </div>
        <div className="field" style={{ flex: 2 }}>
          <label className="mono-label">Display name</label>
          <input type="text" value={manifest.display_name} onChange={(e) => set({ display_name: e.target.value })} />
        </div>
      </div>
      <div className="field">
        <label className="mono-label">Description</label>
        <textarea rows={3} value={manifest.description} onChange={(e) => set({ description: e.target.value })} />
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mono-label">Author</label>
          <input
            type="text"
            value={manifest.author ?? ''}
            onChange={(e) => set({ author: e.target.value || null })}
          />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label className="mono-label">License</label>
          <input
            type="text"
            value={manifest.license ?? ''}
            onChange={(e) => set({ license: e.target.value || null })}
            placeholder="e.g. MIT, Apache-2.0"
          />
        </div>
      </div>
      <div className="field">
        <label className="mono-label">Homepage</label>
        <input
          type="text"
          value={manifest.homepage ?? ''}
          onChange={(e) => set({ homepage: e.target.value || null })}
          placeholder="https://…"
        />
      </div>
      <div className="row" style={{ marginBottom: 0 }}>
        <TagListField label="Tags" values={manifest.tags} onChange={(tags) => set({ tags })} />
        <TagListField label="Frameworks" values={manifest.frameworks} onChange={(frameworks) => set({ frameworks })} />
      </div>
    </div>
  )
}

function TagListField({
  label,
  values,
  onChange,
}: {
  label: string
  values: string[]
  onChange: (v: string[]) => void
}) {
  const [text, setText] = useState(values.join(', '))
  const commit = () => {
    const parsed = text
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    onChange(parsed)
    setText(parsed.join(', '))
  }
  return (
    <div className="field" style={{ flex: 1, marginBottom: 0 }}>
      <label className="mono-label">{label} (comma-separated)</label>
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          }
        }}
      />
    </div>
  )
}

// ── Doctrine ───────────────────────────────────────────────────────────────

function DoctrineEditor({
  manifest,
  setManifest,
  files,
  setFiles,
}: {
  manifest: DraftManifest
  setManifest: (m: DraftManifest) => void
  files: Record<string, DraftFileContent>
  setFiles: (f: Record<string, DraftFileContent>) => void
}) {
  const [active, setActive] = useState<string | null>(manifest.doctrine[0] ?? null)
  const [preview, setPreview] = useState(false)

  useEffect(() => {
    if (active && !manifest.doctrine.includes(active)) setActive(manifest.doctrine[0] ?? null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manifest.doctrine])

  const addFile = () => {
    const name = window.prompt('Doctrine file name (e.g. "01-principles.md")')
    if (!name || !name.trim()) return
    const relpath = `doctrine/${name.trim().replace(/^\/+/, '')}`
    if (manifest.doctrine.includes(relpath)) {
      window.alert('That file already exists.')
      return
    }
    setManifest({ ...manifest, doctrine: [...manifest.doctrine, relpath] })
    setFiles({ ...files, [relpath]: `# ${name.trim().replace(/\.md$/i, '')}\n\n` })
    setActive(relpath)
  }

  const rename = (relpath: string) => {
    const base = relpath.replace(/^doctrine\//, '')
    const name = window.prompt('Rename doctrine file', base)
    if (!name || !name.trim() || name.trim() === base) return
    const next = `doctrine/${name.trim().replace(/^\/+/, '')}`
    if (manifest.doctrine.includes(next)) {
      window.alert('That name is already used.')
      return
    }
    const content = files[relpath]
    const nextFiles = { ...files }
    delete nextFiles[relpath]
    nextFiles[next] = content
    setFiles(nextFiles)
    setManifest({ ...manifest, doctrine: manifest.doctrine.map((d) => (d === relpath ? next : d)) })
    if (active === relpath) setActive(next)
  }

  const remove = (relpath: string) => {
    if (!window.confirm(`Delete ${relpath}? This cannot be undone.`)) return
    const nextFiles = { ...files }
    delete nextFiles[relpath]
    setFiles(nextFiles)
    setManifest({ ...manifest, doctrine: manifest.doctrine.filter((d) => d !== relpath) })
  }

  const move = (relpath: string, dir: -1 | 1) => {
    const idx = manifest.doctrine.indexOf(relpath)
    const next = idx + dir
    if (next < 0 || next >= manifest.doctrine.length) return
    const arr = [...manifest.doctrine]
    ;[arr[idx], arr[next]] = [arr[next], arr[idx]]
    setManifest({ ...manifest, doctrine: arr })
  }

  const content = active && typeof files[active] === 'string' ? (files[active] as string) : ''

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
      <div style={{ width: 240, flexShrink: 0 }}>
        <button type="button" className="btn btn-sm" style={{ marginBottom: 8, width: '100%' }} onClick={addFile}>
          + Add doctrine file
        </button>
        {manifest.doctrine.length === 0 && (
          <div className="empty" style={{ padding: '4px 0' }}>
            No doctrine files yet.
          </div>
        )}
        {manifest.doctrine.map((relpath, i) => (
          <FileListRow
            key={relpath}
            label={relpath.replace(/^doctrine\//, '')}
            active={active === relpath}
            onSelect={() => setActive(relpath)}
            onRename={() => rename(relpath)}
            onDelete={() => remove(relpath)}
            onMoveUp={i > 0 ? () => move(relpath, -1) : undefined}
            onMoveDown={i < manifest.doctrine.length - 1 ? () => move(relpath, 1) : undefined}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {active ? (
          <>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="mono-label">{active}</span>
              <span style={{ flex: 1 }} />
              <button type="button" className="btn btn-sm" onClick={() => setPreview((p) => !p)}>
                {preview ? 'Edit' : 'Preview'}
              </button>
            </div>
            {preview ? (
              <div className="panel" style={{ maxHeight: 480, overflowY: 'auto' }}>
                <MarkdownDoc source={content} />
              </div>
            ) : (
              <textarea
                rows={20}
                style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}
                value={content}
                onChange={(e) => setFiles({ ...files, [active]: e.target.value })}
              />
            )}
          </>
        ) : (
          <div className="empty">Add a doctrine file to begin.</div>
        )}
      </div>
    </div>
  )
}

/** One row in a file-list panel (doctrine/schemas/templates all share this
 *  shape): a click-to-select label plus small inline action buttons. Not
 *  `ListItem` — that component has no room for per-row buttons. */
function FileListRow({
  label,
  active,
  onSelect,
  onDelete,
  onRename,
  onMoveUp,
  onMoveDown,
}: {
  label: string
  active: boolean
  onSelect: () => void
  onDelete: () => void
  onRename?: () => void
  onMoveUp?: () => void
  onMoveDown?: () => void
}) {
  return (
    <div
      className={`list-item${active ? ' active' : ''}`}
      style={{ display: 'flex', alignItems: 'center', gap: 2, cursor: 'pointer' }}
      onClick={onSelect}
    >
      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
      {onMoveUp && (
        <button type="button" className="btn btn-sm" style={{ padding: '1px 5px' }} title="Move up" onClick={(e) => { e.stopPropagation(); onMoveUp() }}>
          ↑
        </button>
      )}
      {onMoveDown && (
        <button type="button" className="btn btn-sm" style={{ padding: '1px 5px' }} title="Move down" onClick={(e) => { e.stopPropagation(); onMoveDown() }}>
          ↓
        </button>
      )}
      {onRename && (
        <button type="button" className="btn btn-sm" style={{ padding: '1px 5px' }} title="Rename" onClick={(e) => { e.stopPropagation(); onRename() }}>
          ✎
        </button>
      )}
      <button
        type="button"
        className="btn btn-sm btn-danger"
        style={{ padding: '1px 5px' }}
        title="Delete"
        onClick={(e) => {
          e.stopPropagation()
          onDelete()
        }}
      >
        ×
      </button>
    </div>
  )
}

// ── Task types ─────────────────────────────────────────────────────────────

const TASK_SHAPES = ['verdict', 'extraction', 'drafting', 'qa_review', 'freeform'] as const
const FIELD_TYPES = ['string', 'number', 'boolean', 'array'] as const

function TaskTypesEditor({
  manifest,
  setManifest,
  files,
}: {
  manifest: DraftManifest
  setManifest: (m: DraftManifest) => void
  files: Record<string, DraftFileContent>
}) {
  const [activeIdx, setActiveIdx] = useState(0)
  // The live, canonical tool roster — GET /api/tools already reads exactly
  // what `packs/schema.py`'s own validation checks task tools against
  // (`engine/tools.py::get_builtin_tools`), so this fetches it rather than
  // hardcoding a list that would drift the moment a tool is added or renamed.
  const toolsQuery = useQuery({ queryKey: ['tools'], queryFn: api.listTools })
  const tools = toolsQuery.data ?? []
  const taskTypes = manifest.task_types
  const schemaOptions = Object.keys(files)
    .filter((p) => p.startsWith('schemas/') && p.endsWith('.json'))
    .sort()

  const setTaskTypes = (next: TaskType[]) => setManifest({ ...manifest, task_types: next })

  const addTaskType = () => {
    const slug = window.prompt('Task type slug (e.g. "risk_assessment")')
    if (!slug || !slug.trim()) return
    if (taskTypes.some((t) => t.slug === slug.trim())) {
      window.alert('That slug is already used.')
      return
    }
    const created: TaskType = {
      slug: slug.trim(),
      display_name: slug.trim(),
      shape: 'freeform',
      input_schema: {},
      tools: [],
      instructions: '',
      output_contract: '',
      doctrine: [],
    }
    setTaskTypes([...taskTypes, created])
    setActiveIdx(taskTypes.length)
  }

  const removeTaskType = (idx: number) => {
    if (!window.confirm(`Delete task type "${taskTypes[idx].slug}"?`)) return
    setTaskTypes(taskTypes.filter((_, i) => i !== idx))
    setActiveIdx(0)
  }

  const patch = (idx: number, p: Partial<TaskType>) => {
    setTaskTypes(taskTypes.map((t, i) => (i === idx ? { ...t, ...p } : t)))
  }

  const active = taskTypes[activeIdx]

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
      <div style={{ width: 220, flexShrink: 0 }}>
        <button type="button" className="btn btn-sm" style={{ marginBottom: 8, width: '100%' }} onClick={addTaskType}>
          + Add task type
        </button>
        {taskTypes.length === 0 && (
          <div className="empty" style={{ padding: '4px 0' }}>
            No task types yet.
          </div>
        )}
        {taskTypes.map((t, i) => (
          <ListItem
            key={`${t.slug}-${i}`}
            active={i === activeIdx}
            onClick={() => setActiveIdx(i)}
            title={t.display_name || t.slug}
            sub={t.slug}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {!active ? (
          <div className="empty">Add a task type to begin.</div>
        ) : (
          <div className="panel stack" style={{ gap: 14 }}>
            <div className="row">
              <span className="mono-label">{active.slug}</span>
              <span style={{ flex: 1 }} />
              <button type="button" className="btn btn-sm btn-danger" onClick={() => removeTaskType(activeIdx)}>
                Delete
              </button>
            </div>
            <div className="row">
              <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                <label className="mono-label">Slug</label>
                <input type="text" value={active.slug} onChange={(e) => patch(activeIdx, { slug: e.target.value })} />
              </div>
              <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                <label className="mono-label">Display name</label>
                <input
                  type="text"
                  value={active.display_name ?? ''}
                  onChange={(e) => patch(activeIdx, { display_name: e.target.value })}
                />
              </div>
            </div>
            <div className="row">
              <div className="field" style={{ width: 160, marginBottom: 0 }}>
                <label className="mono-label">Shape</label>
                <select value={active.shape ?? 'freeform'} onChange={(e) => patch(activeIdx, { shape: e.target.value })}>
                  {TASK_SHAPES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ width: 220, marginBottom: 0 }}>
                <label className="mono-label">Terminal tool</label>
                <select
                  value={active.terminal_tool ?? ''}
                  onChange={(e) => patch(activeIdx, { terminal_tool: e.target.value || undefined })}
                >
                  <option value="">— none —</option>
                  {(active.tools ?? []).map((tn) => (
                    <option key={tn} value={tn}>
                      {tn}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Tools</label>
              {tools.length === 0 ? (
                <div className="empty" style={{ padding: '4px 0' }}>
                  Loading tool list…
                </div>
              ) : (
                <div
                  style={{
                    maxHeight: 170,
                    overflowY: 'auto',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 5,
                    padding: '6px 10px',
                  }}
                >
                  {tools.map((tl) => {
                    const checked = (active.tools ?? []).includes(tl.name)
                    return (
                      <label key={tl.name} className="check-row">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            const nextTools = e.target.checked
                              ? [...(active.tools ?? []), tl.name]
                              : (active.tools ?? []).filter((n) => n !== tl.name)
                            const nextTerminal =
                              active.terminal_tool && !nextTools.includes(active.terminal_tool)
                                ? undefined
                                : active.terminal_tool
                            patch(activeIdx, { tools: nextTools, terminal_tool: nextTerminal })
                          }}
                        />
                        <span>{tl.name}</span>
                        <span className="desc">{tl.available ? tl.description : `off here — ${tl.unavailable_reason}`}</span>
                      </label>
                    )
                  })}
                </div>
              )}
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Output contract</label>
              <input
                type="text"
                value={active.output_contract ?? ''}
                onChange={(e) => patch(activeIdx, { output_contract: e.target.value })}
                placeholder="One-line summary shown to the router"
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Instructions</label>
              <textarea
                rows={6}
                value={active.instructions ?? ''}
                onChange={(e) => patch(activeIdx, { instructions: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Output schema</label>
              <select
                value={active.output_schema ?? ''}
                onChange={(e) => patch(activeIdx, { output_schema: e.target.value || undefined })}
              >
                <option value="">— none —</option>
                {schemaOptions.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              {schemaOptions.length === 0 && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                  Add a schema in the Schemas tab first.
                </div>
              )}
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Doctrine used (empty = every doctrine file)</label>
              <div
                style={{
                  maxHeight: 140,
                  overflowY: 'auto',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 5,
                  padding: '6px 10px',
                }}
              >
                {manifest.doctrine.length === 0 && (
                  <div className="empty" style={{ padding: '2px 0' }}>
                    No doctrine files in this pack yet.
                  </div>
                )}
                {manifest.doctrine.map((d) => (
                  <label key={d} className="check-row">
                    <input
                      type="checkbox"
                      checked={(active.doctrine ?? []).includes(d)}
                      onChange={(e) => {
                        const nextD = e.target.checked
                          ? [...(active.doctrine ?? []), d]
                          : (active.doctrine ?? []).filter((x) => x !== d)
                        patch(activeIdx, { doctrine: nextD })
                      }}
                    />
                    <span>{d.replace(/^doctrine\//, '')}</span>
                  </label>
                ))}
              </div>
            </div>
            <InputSchemaEditor value={active.input_schema ?? {}} onChange={(v) => patch(activeIdx, { input_schema: v })} />
          </div>
        )}
      </div>
    </div>
  )
}

function InputSchemaEditor({
  value,
  onChange,
}: {
  value: Record<string, InputFieldSchema>
  onChange: (v: Record<string, InputFieldSchema>) => void
}) {
  const entries = Object.entries(value)
  const addField = () => {
    const name = window.prompt('Input field name')
    if (!name || !name.trim()) return
    if (value[name.trim()]) {
      window.alert('That field already exists.')
      return
    }
    onChange({ ...value, [name.trim()]: { type: 'string' } })
  }
  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label className="mono-label">Input fields (the run form)</label>
      {entries.length === 0 && (
        <div className="empty" style={{ padding: '2px 0' }}>
          No input fields yet.
        </div>
      )}
      <div className="stack" style={{ gap: 8 }}>
        {entries.map(([name, spec]) => (
          <div key={name} className="row" style={{ gap: 8 }}>
            <span className="mono-body" style={{ width: 140, overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {name}
            </span>
            <select
              value={spec.type ?? 'string'}
              onChange={(e) => onChange({ ...value, [name]: { ...spec, type: e.target.value } })}
              style={{ width: 110 }}
            >
              {FIELD_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <input
              style={{ flex: 1 }}
              type="text"
              placeholder="description"
              value={spec.description ?? ''}
              onChange={(e) => onChange({ ...value, [name]: { ...spec, description: e.target.value } })}
            />
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={() => {
                const next = { ...value }
                delete next[name]
                onChange(next)
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <button type="button" className="btn btn-sm" style={{ marginTop: 8 }} onClick={addField}>
        + Add field
      </button>
    </div>
  )
}

// ── Harnesses ──────────────────────────────────────────────────────────────
// Shape verified against packs/schema.py::HarnessPreset (see client.ts).
// Installing a pack that ships these turns each into a real, editable
// `Harness` row (Packs.tsx's Installed pane says so); this editor only ever
// writes the same small manifest slice every other sub-tab writes to.

function HarnessesEditor({
  manifest,
  setManifest,
}: {
  manifest: DraftManifest
  setManifest: (m: DraftManifest) => void
}) {
  const [activeIdx, setActiveIdx] = useState(0)
  // Same live tool roster the Task types tab reads from — one source of
  // truth for "what tools exist", not a second hardcoded list that could drift.
  const toolsQuery = useQuery({ queryKey: ['tools'], queryFn: api.listTools })
  const tools = toolsQuery.data ?? []
  const presets = manifest.harnesses ?? []
  const taskTypeOptions = manifest.task_types.map((t) => t.slug)

  const setPresets = (next: HarnessPreset[]) => setManifest({ ...manifest, harnesses: next })

  const addPreset = () => {
    const name = window.prompt('Harness name (e.g. "Risk reviewer")')
    if (!name || !name.trim()) return
    if (presets.some((p) => p.name === name.trim())) {
      window.alert('That name is already used.')
      return
    }
    const created: HarnessPreset = { name: name.trim(), description: '', task_types: [], tools: [] }
    setPresets([...presets, created])
    setActiveIdx(presets.length)
  }

  const removePreset = (idx: number) => {
    if (!window.confirm(`Delete harness preset "${presets[idx].name}"?`)) return
    setPresets(presets.filter((_, i) => i !== idx))
    setActiveIdx(0)
  }

  const patch = (idx: number, p: Partial<HarnessPreset>) => {
    setPresets(presets.map((preset, i) => (i === idx ? { ...preset, ...p } : preset)))
  }

  const active = presets[activeIdx]

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
      <div style={{ width: 220, flexShrink: 0 }}>
        <button type="button" className="btn btn-sm" style={{ marginBottom: 8, width: '100%' }} onClick={addPreset}>
          + Add harness
        </button>
        {presets.length === 0 && (
          <div className="empty" style={{ padding: '4px 0' }}>
            No harness presets yet — optional; installing this pack works fine without any.
          </div>
        )}
        {presets.map((p, i) => (
          <ListItem
            key={`${p.name}-${i}`}
            active={i === activeIdx}
            onClick={() => setActiveIdx(i)}
            title={p.name}
            sub={`${p.tools.length} tool${p.tools.length === 1 ? '' : 's'}`}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {!active ? (
          <div className="empty">Add a harness preset to begin.</div>
        ) : (
          <div className="panel stack" style={{ gap: 14 }}>
            <div className="row">
              <span className="mono-label">{active.name}</span>
              <span style={{ flex: 1 }} />
              <button type="button" className="btn btn-sm btn-danger" onClick={() => removePreset(activeIdx)}>
                Delete
              </button>
            </div>
            <div className="row">
              <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                <label className="mono-label">Name</label>
                <input type="text" value={active.name} onChange={(e) => patch(activeIdx, { name: e.target.value })} />
              </div>
              <div className="field" style={{ width: 200, marginBottom: 0 }}>
                <label className="mono-label">Suggested cost tier</label>
                <select
                  value={active.suggested_cost_tier ?? ''}
                  onChange={(e) => patch(activeIdx, { suggested_cost_tier: e.target.value || undefined })}
                >
                  <option value="">— none —</option>
                  {COST_TIERS.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Description</label>
              <textarea
                rows={3}
                value={active.description ?? ''}
                onChange={(e) => patch(activeIdx, { description: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Task type (this pack's own — leave unset for freeform)</label>
              {taskTypeOptions.length === 0 ? (
                <div className="empty" style={{ padding: '4px 0' }}>
                  Add a task type in the Task types tab first.
                </div>
              ) : (
                <select
                  value={active.task_types?.[0] ?? ''}
                  onChange={(e) => patch(activeIdx, { task_types: e.target.value ? [e.target.value] : [] })}
                >
                  <option value="">— freeform —</option>
                  {taskTypeOptions.map((slug) => (
                    <option key={slug} value={slug}>
                      {slug}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="mono-label">Tools</label>
              {tools.length === 0 ? (
                <div className="empty" style={{ padding: '4px 0' }}>
                  Loading tool list…
                </div>
              ) : (
                <div
                  style={{
                    maxHeight: 170,
                    overflowY: 'auto',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 5,
                    padding: '6px 10px',
                  }}
                >
                  {tools.map((tl) => {
                    const checked = active.tools.includes(tl.name)
                    return (
                      <label key={tl.name} className="check-row">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            const nextTools = e.target.checked
                              ? [...active.tools, tl.name]
                              : active.tools.filter((n) => n !== tl.name)
                            patch(activeIdx, { tools: nextTools })
                          }}
                        />
                        <span>{tl.name}</span>
                        <span className="desc">{tl.available ? tl.description : `off here — ${tl.unavailable_reason}`}</span>
                      </label>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Schemas ────────────────────────────────────────────────────────────────

function SchemasEditor({
  files,
  setFiles,
}: {
  files: Record<string, DraftFileContent>
  setFiles: (f: Record<string, DraftFileContent>) => void
}) {
  const schemaPaths = Object.keys(files)
    .filter((p) => p.startsWith('schemas/') && p.endsWith('.json'))
    .sort()
  const [active, setActive] = useState<string | null>(schemaPaths[0] ?? null)

  useEffect(() => {
    if (active && !schemaPaths.includes(active)) setActive(schemaPaths[0] ?? null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schemaPaths.join(',')])

  const addSchema = () => {
    const name = window.prompt('Schema name, no extension (e.g. "risk_verdict")')
    if (!name || !name.trim()) return
    const relpath = `schemas/${name.trim().replace(/[^a-zA-Z0-9_-]/g, '_')}.schema.json`
    if (files[relpath] !== undefined) {
      window.alert('That schema already exists.')
      return
    }
    const starter = JSON.stringify(
      { $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', properties: {}, required: [] },
      null,
      2,
    )
    setFiles({ ...files, [relpath]: starter })
    setActive(relpath)
  }

  const remove = (relpath: string) => {
    if (!window.confirm(`Delete ${relpath}?`)) return
    const next = { ...files }
    delete next[relpath]
    setFiles(next)
  }

  const content = active && typeof files[active] === 'string' ? (files[active] as string) : ''
  let parseError: string | null = null
  if (active) {
    try {
      JSON.parse(content)
    } catch (e) {
      parseError = e instanceof Error ? e.message : String(e)
    }
  }

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
      <div style={{ width: 240, flexShrink: 0 }}>
        <button type="button" className="btn btn-sm" style={{ marginBottom: 8, width: '100%' }} onClick={addSchema}>
          + Add schema
        </button>
        {schemaPaths.length === 0 && (
          <div className="empty" style={{ padding: '4px 0' }}>
            No schemas yet.
          </div>
        )}
        {schemaPaths.map((p) => (
          <FileListRow
            key={p}
            label={p.replace(/^schemas\//, '')}
            active={active === p}
            onSelect={() => setActive(p)}
            onDelete={() => remove(p)}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {active ? (
          <>
            <div className="mono-label" style={{ marginBottom: 6 }}>
              {active}
            </div>
            <textarea
              rows={20}
              style={{ fontFamily: 'var(--mono)', fontSize: 12 }}
              value={content}
              onChange={(e) => setFiles({ ...files, [active]: e.target.value })}
            />
            {parseError ? (
              <div className="error-text" style={{ marginTop: 6 }}>
                Invalid JSON: {parseError}
              </div>
            ) : (
              <div className="mono-body" style={{ marginTop: 6, color: 'var(--green)' }}>
                Valid JSON — full JSON Schema 2020-12 conformance is checked on Validate.
              </div>
            )}
          </>
        ) : (
          <div className="empty">Add a schema to begin.</div>
        )}
      </div>
    </div>
  )
}

// ── Datasets ───────────────────────────────────────────────────────────────

function DatasetsEditor({
  manifest,
  setManifest,
  files,
  setFiles,
}: {
  manifest: DraftManifest
  setManifest: (m: DraftManifest) => void
  files: Record<string, DraftFileContent>
  setFiles: (f: Record<string, DraftFileContent>) => void
}) {
  const fileInputRef = useRef<HTMLInputElement>(null)

  const onFile = (file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      const text = String(reader.result ?? '')
      const base = file.name.replace(/\.csv$/i, '').replace(/[^a-zA-Z0-9_-]/g, '_') || 'dataset'
      let relpath = `datasets/${base}.csv`
      let n = 1
      while (files[relpath] !== undefined) {
        relpath = `datasets/${base}-${n}.csv`
        n += 1
      }
      let name = base
      let n2 = 1
      while (manifest.datasets.some((d) => d.name === name)) {
        name = `${base}-${n2}`
        n2 += 1
      }
      setFiles({ ...files, [relpath]: text })
      setManifest({ ...manifest, datasets: [...manifest.datasets, { name, file: relpath }] })
    }
    reader.readAsText(file)
  }

  const removeDataset = (name: string) => {
    const ds = manifest.datasets.find((d) => d.name === name)
    if (!ds) return
    if (!window.confirm(`Remove dataset "${name}"?`)) return
    const nextFiles = { ...files }
    delete nextFiles[ds.file]
    setFiles(nextFiles)
    setManifest({ ...manifest, datasets: manifest.datasets.filter((d) => d.name !== name) })
  }

  return (
    <div className="stack">
      <div className="dropzone" onClick={() => fileInputRef.current?.click()}>
        Click to upload a CSV — seeded as a dataset on install
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,text/csv"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onFile(f)
            e.target.value = ''
          }}
        />
      </div>
      {manifest.datasets.length === 0 ? (
        <div className="empty">No datasets yet.</div>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          {manifest.datasets.map((ds) => (
            <DatasetPreview
              key={ds.name}
              dataset={ds}
              content={typeof files[ds.file] === 'string' ? (files[ds.file] as string) : ''}
              onRemove={() => removeDataset(ds.name)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function DatasetPreview({
  dataset,
  content,
  onRemove,
}: {
  dataset: PackDatasetRef
  content: string
  onRemove: () => void
}) {
  // A quick eyeball preview, not a real CSV parser — no quoted-comma handling.
  // `validate_pack` (server-side, on Validate) is the actual parser.
  const rows = content
    .split(/\r?\n/)
    .filter((l) => l.length > 0)
    .slice(0, 6)
    .map((l) => l.split(','))

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="mono-body" style={{ fontWeight: 600 }}>
          {dataset.name}
        </span>
        <span className="mono-label">{dataset.file}</span>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn btn-sm btn-danger" onClick={onRemove}>
          Remove
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="empty">Empty file.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="mono-table">
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {r.map((c, j) => (
                    <td key={j}>{c}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Templates ──────────────────────────────────────────────────────────────
// `templates/` is a documented, optional convention (deliverable skeletons,
// docs/pack-authoring.md) — unlike doctrine/datasets/task_types, nothing in
// `PackManifest` lists or references them, so there is no array to keep in
// sync here: just markdown files under a shared prefix.

function TemplatesEditor({
  files,
  setFiles,
}: {
  files: Record<string, DraftFileContent>
  setFiles: (f: Record<string, DraftFileContent>) => void
}) {
  const paths = Object.keys(files)
    .filter((p) => p.startsWith('templates/'))
    .sort()
  const [active, setActive] = useState<string | null>(paths[0] ?? null)
  const [preview, setPreview] = useState(false)

  useEffect(() => {
    if (active && !paths.includes(active)) setActive(paths[0] ?? null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paths.join(',')])

  const addTemplate = () => {
    const name = window.prompt('Template file name (e.g. "summary.md")')
    if (!name || !name.trim()) return
    const relpath = `templates/${name.trim().replace(/^\/+/, '')}`
    if (files[relpath] !== undefined) {
      window.alert('That file already exists.')
      return
    }
    setFiles({ ...files, [relpath]: '' })
    setActive(relpath)
  }

  const remove = (relpath: string) => {
    if (!window.confirm(`Delete ${relpath}?`)) return
    const next = { ...files }
    delete next[relpath]
    setFiles(next)
  }

  const content = active && typeof files[active] === 'string' ? (files[active] as string) : ''

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
      <div style={{ width: 240, flexShrink: 0 }}>
        <button type="button" className="btn btn-sm" style={{ marginBottom: 8, width: '100%' }} onClick={addTemplate}>
          + Add template
        </button>
        {paths.length === 0 && (
          <div className="empty" style={{ padding: '4px 0' }}>
            No templates yet — optional deliverable skeletons.
          </div>
        )}
        {paths.map((p) => (
          <FileListRow
            key={p}
            label={p.replace(/^templates\//, '')}
            active={active === p}
            onSelect={() => setActive(p)}
            onDelete={() => remove(p)}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {active ? (
          <>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="mono-label">{active}</span>
              <span style={{ flex: 1 }} />
              <button type="button" className="btn btn-sm" onClick={() => setPreview((p) => !p)}>
                {preview ? 'Edit' : 'Preview'}
              </button>
            </div>
            {preview ? (
              <div className="panel" style={{ maxHeight: 480, overflowY: 'auto' }}>
                <MarkdownDoc source={content} />
              </div>
            ) : (
              <textarea
                rows={20}
                style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}
                value={content}
                onChange={(e) => setFiles({ ...files, [active]: e.target.value })}
              />
            )}
          </>
        ) : (
          <div className="empty">Add a template to begin.</div>
        )}
      </div>
    </div>
  )
}
