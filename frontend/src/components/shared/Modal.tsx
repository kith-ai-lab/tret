/** A keyboard-accessible modal dialog. No dependencies, no portal library.
 *
 *  What it guarantees, because a dialog that traps a keyboard user is worse than
 *  no dialog:
 *
 *  - `role="dialog"` + `aria-modal="true"` + `aria-labelledby` pointing at the
 *    rendered title, so assistive tech announces what opened.
 *  - Escape closes, from anywhere inside.
 *  - Focus moves into the panel on open and is **restored to the trigger** on
 *    close, so the reader lands back where they were.
 *  - Tab and Shift+Tab cycle within the panel: the focus trap is real, not just
 *    an initial focus() call.
 *  - The backdrop click closes; a click inside never does.
 *  - Body scroll is locked while open, and the panel scrolls internally, so a
 *    long document cannot break the page behind it.
 *
 *  Rendered inline rather than through a portal: the app has a single root and no
 *  transformed ancestors, and `position: fixed` on the backdrop is enough.
 */
import { type ReactNode, useCallback, useEffect, useId, useRef } from 'react'

/** Everything focusable we might contain. `[href]` matters here — the provenance
 *  and methodology content is full of links. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  headerExtra,
  children,
  footer,
  width = 900,
}: {
  open: boolean
  onClose: () => void
  /** Plain text: it is both the visible heading and the accessible name. */
  title: string
  subtitle?: ReactNode
  /** Right-aligned header slot — a status badge, a byte count, a hash. */
  headerExtra?: ReactNode
  children: ReactNode
  footer?: ReactNode
  width?: number
}) {
  const panelRef = useRef<HTMLDivElement | null>(null)
  const titleId = useId()
  const descId = useId()
  // Captured on open, restored on close: the element that opened the dialog.
  const returnFocusTo = useRef<HTMLElement | null>(null)

  const focusables = useCallback((): HTMLElement[] => {
    const panel = panelRef.current
    if (!panel) return []
    return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null || el === document.activeElement,
    )
  }, [])

  // Open: remember the trigger, move focus in. Close: put it back.
  useEffect(() => {
    if (!open) return
    returnFocusTo.current = (document.activeElement as HTMLElement | null) ?? null
    // The panel itself is focusable (tabIndex -1), so focus lands somewhere
    // sensible even when the dialog contains nothing focusable at all.
    const panel = panelRef.current
    const first = focusables()[0]
    ;(first ?? panel)?.focus()
    return () => {
      returnFocusTo.current?.focus?.()
    }
  }, [open, focusables])

  // Escape to close, Tab to cycle. Bound on the document so it works regardless
  // of where focus currently is inside the panel.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) {
        event.preventDefault()
        panelRef.current?.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      // Wrap at both ends, and pull focus back in if it has escaped entirely.
      if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !panelRef.current?.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [open, onClose, focusables])

  // Lock the page behind the dialog; a long doc scrolls inside the panel.
  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  if (!open) return null

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal-panel"
        style={{ maxWidth: width }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={subtitle ? descId : undefined}
        tabIndex={-1}
        ref={panelRef}
        // Stop a drag/click inside the panel from reaching the backdrop.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <h2 className="modal-title" id={titleId}>
              {title}
            </h2>
            {subtitle && (
              <div className="fine-print" id={descId} style={{ marginTop: 3 }}>
                {subtitle}
              </div>
            )}
          </div>
          <span style={{ flex: 1 }} />
          {headerExtra}
          <button className="btn btn-sm" onClick={onClose} aria-label="Close dialog (Escape)">
            close
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}
