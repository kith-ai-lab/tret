/** The methodology, readable in-product, next to the numbers it qualifies.
 *
 *  Several surfaces used to print the string `docs/emissions-methodology.md` and
 *  stop there, which is only useful to someone with a checkout. Those filenames
 *  are now the trigger for this dialog.
 *
 *  The split that matters:
 *
 *  - **Prose** comes from `GET /api/docs/emissions-methodology`, which reads the
 *    repository's own file on every request. There is no copy in the frontend
 *    bundle and no copy in the Python package, so the prose in the product cannot
 *    drift from the prose in the repo.
 *  - **Numbers** never come from that prose. The "live factors" tab is rendered
 *    from the run's own `energy_accounting.factors` — the same records the
 *    arithmetic used — so a constant changing in `emissions.py` changes this
 *    dialog immediately, and no transcription can leave the UI stating a stale
 *    value. Where the document and this tab disagree, the tab is right and the
 *    document is out of date; that is why they are shown side by side.
 */
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'

import { EMISSIONS_METHODOLOGY_SLUG, type EnergyAccounting, api } from '../../api/client'
import { CaveatList, FactorTable, SensitivityTable } from './FactorProvenance'
import { MarkdownDoc } from './MarkdownDoc'
import { Modal } from './Modal'
import {
  BAND_SHORT,
  ESTIMATE_NOTE,
  METHODOLOGY_DOC,
  METHODOLOGY_TRIGGER,
  METHODOLOGY_TRIGGER_HINT,
  bandFactorText,
} from './emissions'
import { formatTokens } from './format'

type Tab = 'factors' | 'document'

export function MethodologyDialog({
  open,
  onClose,
  energy,
  factorsSlot,
}: {
  open: boolean
  onClose: () => void
  /** The run whose factors to show. Null on window-scale views, which have no
   *  single run behind them. */
  energy?: EnergyAccounting | null
  /** Replaces the live-factors tab where there is no run in context — e.g. the
   *  Emissions view passes the window's recording bases instead. */
  factorsSlot?: ReactNode
}) {
  const doc = useQuery({
    queryKey: ['doc', EMISSIONS_METHODOLOGY_SLUG],
    queryFn: () => api.doc(EMISSIONS_METHODOLOGY_SLUG),
    // Only fetch when the dialog is actually opened: 32 KB of markdown is not
    // worth pulling on every page load for a panel most readers never open.
    enabled: open,
    staleTime: 5 * 60 * 1000,
  })
  const hasFactors = Boolean(energy?.factors?.length) || Boolean(factorsSlot)
  const [tab, setTab] = useState<Tab>('factors')
  const active: Tab = hasFactors ? tab : 'document'
  const data = doc.data

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Emissions methodology"
      subtitle={
        <>
          How tret turns token counts into an energy, carbon and money figure, and what those
          figures may not be used for. {ESTIMATE_NOTE}
        </>
      }
      headerExtra={
        data?.available ? (
          <span
            className="fine-print"
            title={`Read from ${data.repo_path} in this build. sha256 ${data.sha256}`}
          >
            <code>{data.repo_path}</code> · {formatTokens(data.bytes)} bytes
          </span>
        ) : null
      }
      footer={
        <span className="fine-print">
          Values in the factor table are read from the run's own stored accounting, not from the
          prose — the prose is the repository's <code>{METHODOLOGY_DOC}</code>, served as-is.{' '}
          {BAND_SHORT}
        </span>
      }
    >
      {hasFactors && (
        <div className="tabs" role="tablist" aria-label="Methodology sections">
          <button
            className={`tab ${active === 'factors' ? 'active' : ''}`}
            role="tab"
            id="methodology-tab-factors"
            aria-controls="methodology-panel"
            aria-selected={active === 'factors'}
            onClick={() => setTab('factors')}
          >
            live factors
          </button>
          <button
            className={`tab ${active === 'document' ? 'active' : ''}`}
            role="tab"
            id="methodology-tab-document"
            aria-controls="methodology-panel"
            aria-selected={active === 'document'}
            onClick={() => setTab('document')}
          >
            full document
          </button>
        </div>
      )}

      <div
        id="methodology-panel"
        role={hasFactors ? 'tabpanel' : undefined}
        aria-labelledby={hasFactors ? `methodology-tab-${active}` : undefined}
      >
        {active === 'factors' ? (
          <LiveFactors energy={energy ?? null} factorsSlot={factorsSlot} />
        ) : doc.isLoading ? (
          <div className="empty pulse">Loading the methodology…</div>
        ) : doc.isError ? (
          <DocUnavailable
            note={`The methodology document could not be loaded: ${(doc.error as Error).message}. It is in the repository at ${METHODOLOGY_DOC}.`}
          />
        ) : !data ? null : data.available && data.markdown ? (
          <MarkdownDoc source={data.markdown} />
        ) : (
          <DocUnavailable
            note={data.note ?? `Not available in this build. See ${METHODOLOGY_DOC}.`}
          />
        )}
      </div>
    </Modal>
  )
}

/** The live half: this run's own factor records, caveats and sensitivity. */
function LiveFactors({
  energy,
  factorsSlot,
}: {
  energy: EnergyAccounting | null
  factorsSlot?: ReactNode
}) {
  if (factorsSlot) return <>{factorsSlot}</>
  if (!energy) return null
  const uncertainty = energy.uncertainty
  const bandText = bandFactorText(uncertainty?.band_factor_low, uncertainty?.band_factor_high)
  return (
    <div className="stack" style={{ gap: 18 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 4 }}>
          Factors applied to this run
        </div>
        <div className="fine-print" style={{ marginBottom: 8 }}>
          Read from the run's stored accounting, exactly as recorded when it ran — not from the
          document, and not recomputed at today's settings. If a constant changes in tret, this
          table changes with it.
        </div>
        <FactorTable factors={energy.factors ?? []} />
      </div>

      {uncertainty && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            What the range is made of{bandText ? ` — ${bandText}` : ''}
          </div>
          <SensitivityTable uncertainty={uncertainty} />
          <div className="callout callout-note" style={{ marginTop: 8 }}>
            <span className="callout-title">On the range</span>
            {uncertainty.basis}
          </div>
        </div>
      )}

      <div>
        <div className="mono-label" style={{ marginBottom: 4 }}>
          Known biases on this run
        </div>
        <div className="fine-print" style={{ marginBottom: 8 }}>
          Every one of these is a way the figure is wrong, named rather than absorbed.
        </div>
        <CaveatList caveats={energy.caveats ?? []} />
      </div>
    </div>
  )
}

function DocUnavailable({ note }: { note: string }) {
  return (
    <div className="callout callout-warn">
      <span className="callout-title">The document is not available here</span>
      {note}
    </div>
  )
}

/** The trigger. Replaces the bare filename that several panels used to print, and
 *  owns its own open state so a caller only has to drop it in. */
export function MethodologyLink({
  energy,
  factorsSlot,
  label = METHODOLOGY_TRIGGER,
  className = 'link-button',
}: {
  energy?: EnergyAccounting | null
  factorsSlot?: ReactNode
  label?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className={className}
        onClick={() => setOpen(true)}
        title={METHODOLOGY_TRIGGER_HINT}
      >
        {label}
      </button>
      <MethodologyDialog
        open={open}
        onClose={() => setOpen(false)}
        energy={energy}
        factorsSlot={factorsSlot}
      />
    </>
  )
}
