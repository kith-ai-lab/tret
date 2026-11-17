/** Which finding (if any) the chat's document pane is showing, plus the layout
 *  that puts the pane beside the thread. The pane is per page view: closing the
 *  chat page or switching conversation closes it, nothing is persisted. */
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import { DocumentPane } from './DocumentPane'

interface PaneApi {
  openId: string | null
  open: (findingId: string) => void
  close: () => void
}

const PaneContext = createContext<PaneApi>({ openId: null, open: () => {}, close: () => {} })

export function useDocumentPane(): PaneApi {
  return useContext(PaneContext)
}

/** Replaces the chat page's root `div.chat-root`: provides the pane state,
 *  renders the children (rail + main column) and the pane beside them.
 *  `resetKey` closes the pane when it changes (the selected conversation). */
export function DocumentPaneLayout({
  children,
  resetKey,
}: {
  children: ReactNode
  resetKey: string | null
}) {
  const [openId, setOpenId] = useState<string | null>(null)
  // The card that opened the pane, so closing it puts focus back there.
  const returnFocus = useRef<HTMLElement | null>(null)

  const open = useCallback((id: string) => {
    setOpenId((current) => {
      if (current === null) returnFocus.current = document.activeElement as HTMLElement | null
      return id
    })
  }, [])
  const close = useCallback(() => {
    setOpenId(null)
    const el = returnFocus.current
    returnFocus.current = null
    // After the pane unmounts, so the focus does not land on a removed node.
    window.setTimeout(() => el?.focus?.(), 0)
  }, [])

  useEffect(() => setOpenId(null), [resetKey])

  const value = useMemo(() => ({ openId, open, close }), [openId, open, close])
  return (
    <PaneContext.Provider value={value}>
      <div className={`chat-root${openId ? ' has-pane' : ''}`}>
        {children}
        {openId && <DocumentPane findingId={openId} onClose={close} />}
      </div>
    </PaneContext.Provider>
  )
}
