import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type DragEvent, useRef, useState } from 'react'

import { api, type BenchDocument, type Dataset } from '../api/client'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'
import { formatDateTime } from './Runs'

const PREVIEW_CHARS = 4000

export function Documents() {
  const queryClient = useQueryClient()
  const documentsQuery = useQuery({ queryKey: ['documents'], queryFn: api.listDocuments })
  const datasetsQuery = useQuery({ queryKey: ['datasets'], queryFn: api.listDatasets })

  const [previewId, setPreviewId] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadDocument(file),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['documents'] }),
  })

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    for (const file of Array.from(e.dataTransfer.files)) {
      uploadMutation.mutate(file)
    }
  }

  const documents = documentsQuery.data ?? []

  const columns: Column<BenchDocument>[] = [
    { key: 'name', header: 'Filename', render: (d) => d.filename },
    { key: 'type', header: 'Type', render: (d) => d.content_type },
    {
      key: 'size',
      header: 'Size',
      align: 'right',
      render: (d) => `${(d.byte_size / 1024).toFixed(1)} KB`,
    },
    {
      key: 'status',
      header: 'Extraction',
      render: (d) => <StatusBadge status={d.extraction_status} />,
    },
    {
      key: 'chars',
      header: 'Chars',
      align: 'right',
      render: (d) => d.text_chars.toLocaleString(),
    },
    {
      key: 'uploaded',
      header: 'Uploaded',
      render: (d) => (d.created_at ? formatDateTime(d.created_at) : '—'),
    },
  ]

  return (
    <div className="stack" style={{ gap: 24 }}>
      <div>
        <h1 className="view-title">Documents</h1>
        <div className="view-sub">Evidence uploads and pack-seeded datasets.</div>

        <div
          className={`dropzone${dragging ? ' drag' : ''}`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          {uploadMutation.isPending
            ? 'Uploading…'
            : 'Drop files here or click to upload (25 MB max)'}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => {
              for (const file of Array.from(e.target.files ?? [])) {
                uploadMutation.mutate(file)
              }
              e.target.value = ''
            }}
          />
        </div>
        {uploadMutation.isError && (
          <div className="error-text" style={{ marginTop: 8 }}>
            {(uploadMutation.error as Error).message}
          </div>
        )}

        <div style={{ marginTop: 16 }}>
          {documentsQuery.isLoading ? (
            <div className="empty pulse">Loading documents…</div>
          ) : (
            <MonoTable
              columns={columns}
              rows={documents}
              rowKey={(d) => d.id}
              onRowClick={(d) => setPreviewId(previewId === d.id ? null : d.id)}
              empty="No documents yet — upload evidence to attach to runs."
            />
          )}
        </div>

        {previewId && <DocumentPreview documentId={previewId} onClose={() => setPreviewId(null)} />}
      </div>

      <div>
        <h2 className="view-title">Datasets</h2>
        <div className="view-sub">
          The deterministic lane — values models may only retrieve, never compute.
        </div>
        {datasetsQuery.isLoading ? (
          <div className="empty pulse">Loading datasets…</div>
        ) : (datasetsQuery.data ?? []).length === 0 ? (
          <div className="empty">No datasets — install a pack that seeds sample data.</div>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {(datasetsQuery.data ?? []).map((ds) => (
              <DatasetRowPeek key={ds.id} dataset={ds} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function DocumentPreview({ documentId, onClose }: { documentId: string; onClose: () => void }) {
  const docQuery = useQuery({
    queryKey: ['document', documentId],
    queryFn: () => api.getDocument(documentId),
  })

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="mono-label">
          Preview{docQuery.data ? ` — ${docQuery.data.filename}` : ''}
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
      {docQuery.isLoading ? (
        <div className="empty pulse">Extracting preview…</div>
      ) : docQuery.isError ? (
        <div className="error-text">{(docQuery.error as Error).message}</div>
      ) : docQuery.data ? (
        docQuery.data.extracted_text ? (
          <pre className="code-block" style={{ maxHeight: 320 }}>
            {docQuery.data.extracted_text.slice(0, PREVIEW_CHARS)}
            {docQuery.data.extracted_text.length > PREVIEW_CHARS ? '\n… (truncated preview)' : ''}
          </pre>
        ) : (
          <div className="empty">No text extracted ({docQuery.data.extraction_status}).</div>
        )
      ) : null}
    </div>
  )
}

function DatasetRowPeek({ dataset }: { dataset: Dataset }) {
  const [open, setOpen] = useState(false)
  const rowsQuery = useQuery({
    queryKey: ['dataset-rows', dataset.id],
    queryFn: () => api.getDatasetRows(dataset.id, 10),
    enabled: open,
  })

  return (
    <div className="panel" style={{ padding: '10px 14px' }}>
      <div
        className="row"
        style={{ cursor: 'pointer' }}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="mono-body" style={{ fontWeight: 600 }}>
          {dataset.name}
        </span>
        <span className="mono-label">{dataset.row_count} rows</span>
        <span className="mono-label" style={{ textTransform: 'none', letterSpacing: 0 }}>
          {dataset.columns.join(', ')}
        </span>
        <span style={{ flex: 1 }} />
        {dataset.pack_seeded && <span className="badge badge-violet">pack</span>}
        <span className="mono-label">{open ? 'hide' : 'peek'}</span>
      </div>
      {open && (
        <div style={{ marginTop: 10, overflowX: 'auto' }}>
          {rowsQuery.isLoading ? (
            <div className="empty pulse">Loading rows…</div>
          ) : rowsQuery.isError ? (
            <div className="error-text">{(rowsQuery.error as Error).message}</div>
          ) : (rowsQuery.data ?? []).length === 0 ? (
            <div className="empty">No rows.</div>
          ) : (
            <table className="mono-table">
              <thead>
                <tr>
                  {dataset.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(rowsQuery.data ?? []).map((row, i) => (
                  <tr key={i}>
                    {dataset.columns.map((c) => (
                      <td key={c}>{String(row[c] ?? '')}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
