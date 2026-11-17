/** One card per finding a chat turn produced, under the assistant message.
 *  The whole card is a button that opens the finding in the document pane. */
import type { ChatActivity, Finding } from '../../api/client'
import type { useRunStream } from '../../api/useRunStream'
import { useDocumentPane } from './documentPaneContext'
import { ResultStatusChip, resultLabel, resultSubtitle, resultTitle } from './resultMeta'
import { useTurnFindings } from './useTurnFindings'

export function ResultCard({
  finding,
  auto,
  active,
  onOpen,
}: {
  finding: Finding
  auto: boolean
  active: boolean
  onOpen: () => void
}) {
  const subtitle = resultSubtitle(finding)
  return (
    <button
      type="button"
      className={`rc-card${active ? ' active' : ''}`}
      onClick={onOpen}
      aria-current={active ? 'true' : undefined}
      aria-label={`Open ${resultLabel(finding).toLowerCase()}: ${resultTitle(finding)}`}
    >
      <span className="rc-main">
        <span className="rc-kind">{resultLabel(finding)}</span>
        <span className="rc-title">{resultTitle(finding)}</span>
        {subtitle && <span className="rc-sub">{subtitle}</span>}
      </span>
      <span className="rc-side">
        <ResultStatusChip status={finding.status} auto={auto} />
        <span className="rc-open" aria-hidden="true">
          Open
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
            <path d="M6 3l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </span>
    </button>
  )
}

/** The cards for one turn: pass `activity` for a finished turn or `live` for
 *  the one streaming. Renders nothing when the turn produced no result. */
export function TurnResults({
  runId,
  activity,
  live,
}: {
  runId: string | null
  activity?: ChatActivity[]
  live?: ReturnType<typeof useRunStream>
}) {
  const results = useTurnFindings({ runId, activity, live })
  const pane = useDocumentPane()
  if (results.length === 0) return null
  return (
    <div className="rc-list" role="list" aria-label="Results from this turn">
      {results.map(({ finding, auto }) => (
        <div role="listitem" key={finding.id}>
          <ResultCard
            finding={finding}
            auto={auto}
            active={pane.openId === finding.id}
            onOpen={() => pane.open(finding.id)}
          />
        </div>
      ))}
    </div>
  )
}
