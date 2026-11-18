import { useQuery } from '@tanstack/react-query'

import { api } from './api/client'

/** UI edition: which screens the web UI shows. Presentation only, not an
 *  access control (backend: tret/services/edition.py). */
export type Edition = 'standard' | 'cohort'

export interface NavItem {
  to: string
  label: string
  count?: 'approvals' | 'deliverables'
}

// Single source of truth for per-edition navigation. `standard` is the full
// sidebar, in today's order, with nothing hidden. `cohort` is a lean list.
// Approvals stays visible in cohort because rejected, noted and non-automatic
// items still need a home even when most approvals happen automatically.
// `exactPrefixes` hides a list page but not its detail pages (`/runs/<id>`).
const EDITIONS: Record<Edition, { nav: NavItem[]; hiddenPrefixes: string[]; exactPrefixes?: string[] }> = {
  standard: {
    nav: [
      { to: '/', label: 'Chat' },
      { to: '/workbench', label: 'Workbench' },
      { to: '/runs', label: 'Runs' },
      { to: '/approvals', label: 'Approvals', count: 'approvals' },
      { to: '/batches', label: 'Batches' },
      { to: '/analytics', label: 'Analytics' },
      { to: '/emissions', label: 'Emissions' },
      { to: '/deliverables', label: 'Deliverables', count: 'deliverables' },
      { to: '/documents', label: 'Documents' },
      { to: '/packs', label: 'Packs' },
      { to: '/harnesses', label: 'Harnesses' },
      { to: '/settings', label: 'Settings' },
    ],
    hiddenPrefixes: [],
  },
  cohort: {
    nav: [
      { to: '/', label: 'Sessions' },
      { to: '/approvals', label: 'Approvals', count: 'approvals' },
      { to: '/deliverables', label: 'Deliverables', count: 'deliverables' },
      { to: '/documents', label: 'Documents' },
      { to: '/settings', label: 'Settings' },
    ],
    hiddenPrefixes: [
      '/workbench',
      '/batches',
      '/analytics',
      '/emissions',
      '/packs',
      '/harnesses',
      '/marketplace-review',
      '/collections',
    ],
    exactPrefixes: ['/runs'],
  },
}

/** Anything that is not exactly "cohort" (an older server that does not send
 *  the field, an unknown value) is the standard edition. */
export function normalizeEdition(value: string | null | undefined): Edition {
  return value === 'cohort' ? 'cohort' : 'standard'
}

export function visibleNav(edition: Edition): NavItem[] {
  return EDITIONS[edition].nav
}

export function isRouteHidden(edition: Edition, pathname: string): boolean {
  const { hiddenPrefixes, exactPrefixes = [] } = EDITIONS[edition]
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  return (
    hiddenPrefixes.some((p) => pathname === p || pathname.startsWith(`${p}/`)) ||
    exactPrefixes.includes(path)
  )
}

/** The nouns the chat page uses for itself. Tret Learning calls a chat a
 *  "session" so learners connect it with Claude Code; standard keeps "chat". */
export interface ChatWording {
  /** Rail button, topbar title and icon button: "New chat" / "New session". */
  newChat: string
  /** Lower-case singular, for sentences. */
  noun: string
  /** Lower-case plural, for sentences. */
  nouns: string
  /** Capitalised plural, for the toggle titles. */
  showList: string
  hideList: string
  toggleList: string
  empty: string
  loadFailed: string
  untitled: string
  loading: string
  threadLoadFailed: string
}

export function chatWording(edition: Edition): ChatWording {
  if (edition === 'cohort') {
    return {
      newChat: 'New session',
      noun: 'session',
      nouns: 'sessions',
      showList: 'Show sessions',
      hideList: 'Hide sessions',
      toggleList: 'Toggle session list',
      empty: 'No sessions yet.',
      loadFailed: 'Could not load sessions',
      untitled: 'New session',
      loading: 'Loading session…',
      threadLoadFailed: 'Could not load this session',
    }
  }
  return {
    newChat: 'New chat',
    noun: 'conversation',
    nouns: 'conversations',
    showList: 'Show conversations',
    hideList: 'Hide conversations',
    toggleList: 'Toggle conversation list',
    empty: 'No conversations yet.',
    loadFailed: 'Could not load conversations',
    untitled: 'New conversation',
    loading: 'Loading conversation…',
    threadLoadFailed: 'Could not load this conversation',
  }
}

/** The logged-in user's edition, read from the shared ['me'] query (same key
 *  and fn as App.tsx, so it is a cache read, not a second request). */
export function useEdition(): Edition {
  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false, staleTime: Infinity })
  return normalizeEdition(me.data?.edition)
}
