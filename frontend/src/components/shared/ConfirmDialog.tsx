/** In-app replacement for `window.confirm`.
 *
 *  Embedded browsers (the Claude desktop app's browser pane, some webviews)
 *  suppress native dialogs: `window.confirm` returns `false` immediately and the
 *  guarded action silently never runs. This renders the prompt as an ordinary
 *  modal instead, so it works wherever the app itself renders.
 *
 *  Usage:
 *
 *      const confirm = useConfirm()
 *      if (!(await confirm({ message: 'Delete x?', confirmLabel: 'Delete', destructive: true }))) return
 *
 *  One `<ConfirmProvider>` sits at the app root (inside the router) and owns the
 *  single dialog. The promise resolves `true` on the confirm button or Enter, and
 *  `false` on Cancel, Escape, the close button, a backdrop click or any route
 *  change. Focus trapping, Escape and focus return to the trigger come from
 *  `Modal`.
 */
import { createContext, type ReactNode, useCallback, useContext, useEffect, useId, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { Modal } from './Modal'

export interface ConfirmOptions {
  /** The question, shown verbatim as the dialog body. */
  message: string
  /** Dialog heading and accessible name. Defaults to "Confirm". */
  title?: string
  /** Label of the confirming button. Defaults to "OK". */
  confirmLabel?: string
  /** Label of the cancelling button. Defaults to "Cancel". */
  cancelLabel?: string
  /** Style the confirming button as a dangerous action (`btn-reject`). */
  destructive?: boolean
}

export type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

/** Returns a `confirm(options)` function that resolves once the user answers. */
export function useConfirm(): ConfirmFn {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm must be used inside <ConfirmProvider>')
  return confirm
}

interface Pending {
  options: ConfirmOptions
  resolve: (value: boolean) => void
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  // Mirror of `pending` for callbacks that must not go stale.
  const pendingRef = useRef<Pending | null>(null)

  const settle = useCallback((value: boolean) => {
    const current = pendingRef.current
    if (!current) return
    pendingRef.current = null
    setPending(null)
    current.resolve(value)
  }, [])

  const confirm = useCallback<ConfirmFn>((options) => {
    // A second prompt while one is open: the first one is answered "no", so no
    // caller is ever left awaiting a promise that never settles.
    pendingRef.current?.resolve(false)
    return new Promise<boolean>((resolve) => {
      const next = { options, resolve }
      pendingRef.current = next
      setPending(next)
    })
  }, [])

  // Navigating with a prompt open (browser back, a link): answer "no". The
  // provider sits above the routes and never unmounts on navigation, so without
  // this the prompt would outlive the view that asked, and confirming could act
  // on whatever that view now shows. A no-op on first mount: nothing is pending.
  const { key: locationKey } = useLocation()
  useEffect(() => settle(false), [locationKey, settle])

  // Unmounting with a prompt open (error boundary, teardown): answer "no".
  useEffect(() => () => pendingRef.current?.resolve(false), [])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <ConfirmDialog
        open={pending !== null}
        options={pending?.options ?? { message: '' }}
        onConfirm={() => settle(true)}
        onCancel={() => settle(false)}
      />
    </ConfirmContext.Provider>
  )
}

/** The presentational dialog. Exported for direct use where a provider is not
 *  wanted; most callers should use `useConfirm()`. */
export function ConfirmDialog({
  open,
  options,
  onConfirm,
  onCancel,
}: {
  open: boolean
  options: ConfirmOptions
  onConfirm: () => void
  onCancel: () => void
}) {
  const { message, title = 'Confirm', confirmLabel = 'OK', cancelLabel = 'Cancel', destructive = false } = options
  const confirmRef = useRef<HTMLButtonElement | null>(null)
  const messageId = useId()

  // Enter confirms, like the native dialog. A focused button keeps its own
  // Enter behaviour, so Enter on Cancel still cancels.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Enter' || event.repeat) return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'BUTTON' || target.tagName === 'A' || target.tagName === 'TEXTAREA')) return
      event.preventDefault()
      onConfirm()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onConfirm])

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      width={480}
      initialFocusRef={confirmRef}
      describedBy={messageId}
      footer={
        <div className="confirm-actions">
          <button type="button" className="btn" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            ref={confirmRef}
            className={destructive ? 'btn btn-reject' : 'btn btn-primary'}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      }
    >
      <p className="confirm-message" id={messageId}>
        {message}
      </p>
    </Modal>
  )
}
