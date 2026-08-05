/** Where a run's context tokens went: a compact stacked bar plus a per-component
 *  table. Token counts are the backend's own estimate (estimator is named), not
 *  provider-reported usage. */
import type { ContextComposition as Composition } from '../../api/client'
import { formatTokens } from './format'

// Stable colour per component kind, so the bar and the table always agree and
// the same kind reads the same across runs.
const KIND_COLORS: Record<string, string> = {
  platform_preamble: 'var(--blue)',
  doctrine: 'var(--violet)',
  task_instructions: 'var(--green)',
  output_schema: 'var(--amber)',
  tool_specs: 'var(--accent)',
  extra_context: 'var(--gray)',
  harness_extra: 'var(--red)',
}

const FALLBACK_COLOR = 'var(--gray)'

export function ContextComposition({ composition }: { composition: Composition }) {
  const total = composition.total_est_tokens
  const kinds = Object.entries(composition.by_kind).sort((a, b) => b[1] - a[1])
  const pct = (tokens: number) => (total > 0 ? (100 * tokens) / total : 0)

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="mono-label">Context composition</div>
        <span style={{ flex: 1 }} />
        <div className="mono-label" title={`Token estimator: ${composition.estimator}`}>
          {formatTokens(total)} est. tokens · {composition.estimator}
        </div>
      </div>

      {/* Stacked bar */}
      <div
        style={{
          display: 'flex',
          height: 8,
          borderRadius: 4,
          overflow: 'hidden',
          background: 'var(--bg-input)',
          marginBottom: 10,
        }}
      >
        {kinds.map(([kind, tokens]) => (
          <div
            key={kind}
            title={`${kind}: ${formatTokens(tokens)} est. tokens (${pct(tokens).toFixed(1)}%)`}
            style={{
              width: `${pct(tokens)}%`,
              background: KIND_COLORS[kind] ?? FALLBACK_COLOR,
              opacity: 0.75,
            }}
          />
        ))}
      </div>

      <table className="mono-table">
        <thead>
          <tr>
            <th>Component</th>
            <th className="num">Est. tokens</th>
            <th className="num">Share</th>
          </tr>
        </thead>
        <tbody>
          {kinds.map(([kind, tokens]) => (
            <tr key={kind}>
              <td>
                <span
                  style={{
                    display: 'inline-block',
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: KIND_COLORS[kind] ?? FALLBACK_COLOR,
                    marginRight: 6,
                  }}
                />
                {kind}
              </td>
              <td className="num">{formatTokens(tokens)}</td>
              <td className="num">{pct(tokens).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>

      {composition.blocks.length > 0 && (
        <details className="tool-row" style={{ marginTop: 10 }}>
          <summary>
            <span className="tool-name">blocks</span>
            <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
              {composition.blocks.length} accounted · {formatTokens(composition.total_chars)} chars
            </span>
          </summary>
          <div style={{ padding: '6px 10px' }}>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Label</th>
                <th className="num">Est. tokens</th>
                <th className="num">Chars</th>
                <th>Pin</th>
              </tr>
            </thead>
            <tbody>
              {composition.blocks.map((b, i) => (
                <tr key={`${b.kind}-${b.label}-${i}`}>
                  <td>{b.kind}</td>
                  <td title={b.note ?? b.sections?.join('; ')}>{b.label}</td>
                  <td className="num">{formatTokens(b.est_tokens)}</td>
                  <td className="num">{formatTokens(b.chars)}</td>
                  <td title={b.sha256}>{b.sha256 ? b.sha256.slice(0, 10) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </details>
      )}
    </div>
  )
}
