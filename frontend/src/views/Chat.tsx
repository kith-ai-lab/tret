import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type KeyboardEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api, type ChatMessage } from '../api/client'
import { useRunStream } from '../api/useRunStream'
import { formatCost, formatTokens } from '../components/shared/format'
import { LiveFootprint } from '../components/shared/LiveFootprint'
import { shortModelName } from '../components/shared/RoutingBadge'
import { Markdown } from './Packs'

const EXAMPLE_PROMPTS = [
  {
    title: 'Trust a vendor score',
    prompt: 'Is the vendor flood score for Alder Point (S-003) still trustworthy?',
  },
  {
    title: 'Extract evidence',
    prompt: 'Extract governance evidence from the uploaded questionnaire',
  },
  { title: 'Survey the data', prompt: 'What datasets do we have?' },
  {
    title: 'Draft a section',
    prompt: 'Draft the risk management section of the TCFD assessment',
  },
]

interface PendingTurn {
  conversationId: string
  runId: string
}

export function Chat() {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingTurn | null>(null)
  const [draft, setDraft] = useState('')
  const [railOpen, setRailOpen] = useState(true)

  const conversationsQuery = useQuery({
    queryKey: ['conversations'],
    queryFn: api.listConversations,
  })
  const conversations = conversationsQuery.data ?? []

  // Default to the most recent conversation once the list first loads. Never
  // override the user's explicit selection (incl. the deliberate null of a
  // fresh chat).
  const initializedRef = useRef(false)
  useEffect(() => {
    if (conversationsQuery.data === undefined || initializedRef.current) return
    initializedRef.current = true
    setSelectedId(conversationsQuery.data[0]?.id ?? null)
  }, [conversationsQuery.data])

  const conversationQuery = useQuery({
    queryKey: ['conversation', selectedId],
    queryFn: () => api.getConversation(selectedId!),
    enabled: !!selectedId,
  })

  const stream = useRunStream(pending?.runId ?? null)

  // While a turn is in flight, poll the conversation until the assistant
  // message for that run is appended server-side. Manual interval (not
  // refetchInterval) so polling continues even in unfocused/background tabs.
  useEffect(() => {
    if (!pending) return
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ['conversation', pending.conversationId] })
    }, 1500)
    return () => window.clearInterval(timer)
  }, [pending, queryClient])

  // When the persisted assistant message lands, close out the live turn.
  useEffect(() => {
    if (!pending || conversationQuery.data?.id !== pending.conversationId) return
    const landed = conversationQuery.data.messages.some(
      (m) => m.role === 'assistant' && m.run_id === pending.runId,
    )
    if (landed) {
      setPending(null)
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    }
  }, [conversationQuery.data, pending, queryClient])

  const startNewChat = useCallback(() => {
    setPending(null)
    setDraft('')
    setSelectedId(null)
  }, [])

  const sendMutation = useMutation({
    mutationFn: ({ conversationId, text }: { conversationId: string; text: string }) =>
      api.sendChatMessage(conversationId, text),
    onSuccess: (data) => {
      setPending({ conversationId: data.conversation_id, runId: data.run_id })
      queryClient.invalidateQueries({ queryKey: ['conversation', data.conversation_id] })
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })

  const inFlight = pending !== null || sendMutation.isPending

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || inFlight) return
      let conversationId = selectedId
      if (!conversationId) {
        const conv = await api.createConversation()
        queryClient.setQueryData(['conversation', conv.id], conv)
        queryClient.invalidateQueries({ queryKey: ['conversations'] })
        setSelectedId(conv.id)
        conversationId = conv.id
      }
      setDraft('')
      sendMutation.mutate({ conversationId, text: trimmed })
    },
    [inFlight, selectedId, queryClient, sendMutation],
  )

  const conversation = selectedId ? conversationQuery.data : undefined
  const messages = conversation?.messages ?? []
  const showLive = !!pending && pending.conversationId === selectedId
  const isLanding = !selectedId && !inFlight && messages.length === 0
  const threadLoading = !!selectedId && conversationQuery.isLoading && !pending

  return (
    <div className="chat-root">
      {railOpen && (
        <ConversationRail
          conversations={conversations}
          loading={conversationsQuery.isLoading}
          selectedId={selectedId}
          onSelect={(id) => {
            if (inFlight) return
            setSelectedId(id)
          }}
          onNewChat={startNewChat}
        />
      )}

      <div className="chat-main">
        <div className="chat-topbar">
          <button
            className="icon-btn"
            title={railOpen ? 'Hide conversations' : 'Show conversations'}
            onClick={() => setRailOpen((o) => !o)}
            aria-label="Toggle conversation list"
          >
            <PanelIcon />
          </button>
          <div className="chat-topbar-title">
            {conversation?.title ?? (isLanding ? 'New chat' : 'bench')}
          </div>
          <button className="icon-btn" title="New chat" onClick={startNewChat} aria-label="New chat">
            <PlusIcon />
          </button>
        </div>

        {isLanding ? (
          <Landing draft={draft} onDraft={setDraft} onSend={send} disabled={inFlight} />
        ) : (
          <>
            <Thread
              messages={messages}
              loading={threadLoading}
              live={showLive ? { runId: pending.runId, stream } : null}
            />
            <div className="chat-composer-dock">
              <div className="chat-column">
                <Composer
                  value={draft}
                  onChange={setDraft}
                  onSend={() => void send(draft)}
                  disabled={inFlight}
                  error={sendMutation.isError ? (sendMutation.error as Error).message : null}
                />
                <div className="chat-hint">
                  Responses are drafts — structured findings go to Approvals before they count.
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── Conversation rail ───────────────────────────────────────────────────────

function ConversationRail({
  conversations,
  loading,
  selectedId,
  onSelect,
  onNewChat,
}: {
  conversations: { id: string; title: string | null; message_count: number; updated_at: string | null }[]
  loading: boolean
  selectedId: string | null
  onSelect: (id: string) => void
  onNewChat: () => void
}) {
  return (
    <aside className="chat-rail">
      <button className="chat-newchat" onClick={onNewChat}>
        <PlusIcon />
        <span>New chat</span>
      </button>
      <div className="chat-rail-list">
        {loading ? (
          <div className="empty pulse" style={{ padding: '8px 4px' }}>
            Loading…
          </div>
        ) : conversations.length === 0 ? (
          <div className="empty" style={{ padding: '8px 4px', fontSize: 11 }}>
            No conversations yet.
          </div>
        ) : (
          conversations.map((c) => (
            <button
              key={c.id}
              className={`chat-rail-item${c.id === selectedId ? ' active' : ''}`}
              onClick={() => onSelect(c.id)}
              title={c.title ?? 'New conversation'}
            >
              <span className="chat-rail-title">{c.title ?? 'New conversation'}</span>
              <span className="chat-rail-time">{relativeTime(c.updated_at)}</span>
            </button>
          ))
        )}
      </div>
    </aside>
  )
}

// ── Empty landing ────────────────────────────────────────────────────────────

function Landing({
  draft,
  onDraft,
  onSend,
  disabled,
}: {
  draft: string
  onDraft: (v: string) => void
  onSend: (text: string) => void
  disabled: boolean
}) {
  return (
    <div className="chat-landing">
      <div className="chat-column">
        <div className="chat-hero-mark">
          bench<span>_</span>
        </div>
        <h1 className="chat-hero-title">What can I help you assess?</h1>
        <p className="chat-hero-sub">
          Ask in plain language. I delegate structured work to the right harness task, and every
          turn is an auditable run.
        </p>
        <Composer
          value={draft}
          onChange={onDraft}
          onSend={() => onSend(draft)}
          disabled={disabled}
          error={null}
          autoFocus
        />
        <div className="chat-cards">
          {EXAMPLE_PROMPTS.map((ex) => (
            <button key={ex.prompt} className="chat-card" onClick={() => onSend(ex.prompt)}>
              <span className="chat-card-title">{ex.title}</span>
              <span className="chat-card-prompt">{ex.prompt}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Thread ────────────────────────────────────────────────────────────────

function Thread({
  messages,
  loading,
  live,
}: {
  messages: ChatMessage[]
  loading: boolean
  live: { runId: string; stream: ReturnType<typeof useRunStream> } | null
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const liveTextLength = live?.stream.text.length ?? 0
  const liveItemCount = live?.stream.items.length ?? 0

  // Stick to the bottom as content grows.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, liveTextLength, liveItemCount, loading])

  return (
    <div className="chat-scroll" ref={scrollRef}>
      <div className="chat-column chat-messages">
        {loading && (
          <div className="empty pulse" style={{ padding: '24px 0' }}>
            Loading conversation…
          </div>
        )}

        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="chat-turn user">
              <div className="chat-bubble-user">{m.content}</div>
            </div>
          ) : (
            <AssistantTurn key={i} message={m} />
          ),
        )}

        {live && <LiveTurn runId={live.runId} stream={live.stream} />}
      </div>
    </div>
  )
}

function AssistantAvatar() {
  return <div className="chat-avatar">b</div>
}

function AssistantTurn({ message }: { message: ChatMessage }) {
  const navigate = useNavigate()
  const activity = message.activity ?? []
  return (
    <div className="chat-turn assistant">
      <AssistantAvatar />
      <div className="chat-assistant-body">
        {activity.length > 0 && (
          <div className="chat-pills">
            {activity.map((a, i) => (
              <button
                key={i}
                className="chat-pill done"
                title={message.run_id ? 'View the run for this turn' : undefined}
                onClick={() => message.run_id && navigate(`/runs/${message.run_id}`)}
              >
                <GearIcon /> {a.summary || a.tool}
              </button>
            ))}
          </div>
        )}
        <div className="chat-prose md">
          <Markdown source={message.content || '_(no output)_'} />
        </div>
        <div className="chat-turn-footer">
          {message.status && message.status !== 'completed' && (
            <span className="err">{message.status}</span>
          )}
          {message.model_used && <span>{shortModelName(message.model_used)}</span>}
          {message.cost_usd !== undefined && <span>{formatCost(message.cost_usd)}</span>}
          {message.run_id && (
            <Link to={`/runs/${message.run_id}`} className="chat-viewrun">
              view run
            </Link>
          )}
        </div>
      </div>
    </div>
  )
}

function LiveTurn({ runId, stream }: { runId: string; stream: ReturnType<typeof useRunStream> }) {
  const navigate = useNavigate()
  const toolCalls = stream.items.filter((i) => i.kind === 'tool_call')
  const finalizing = stream.done
  const usage = stream.usage
  return (
    <div className="chat-turn assistant">
      <AssistantAvatar />
      <div className="chat-assistant-body">
        {toolCalls.length > 0 && (
          <div className="chat-pills">
            {toolCalls.map((tc, i) => (
              <button
                key={i}
                className="chat-pill running"
                onClick={() => navigate(`/runs/${runId}`)}
                title="View the run for this turn"
              >
                <GearIcon />{' '}
                {tc.tool === 'run_harness_task'
                  ? `running ${String(tc.arguments['task_type'] ?? 'task')}…`
                  : `${tc.tool}…`}
              </button>
            ))}
          </div>
        )}
        {stream.text ? (
          <div className="chat-prose">
            <span style={{ whiteSpace: 'pre-wrap' }}>{stream.text}</span>
            {!finalizing && <span className="chat-caret" />}
          </div>
        ) : (
          <div className="chat-thinking">
            <span className="chat-caret" /> {finalizing ? 'finalizing…' : 'thinking…'}
          </div>
        )}
        {stream.error && <div className="error-text" style={{ marginTop: 8 }}>{stream.error}</div>}
        <div className="chat-turn-footer">
          <span className="pulse">{finalizing ? 'finalizing' : 'streaming'}</span>
          {usage && (
            <span
              title={`cache ${formatTokens(usage.cache_read_tokens)} read / ${formatTokens(usage.cache_write_tokens)} write`}
            >
              {formatTokens(usage.input_tokens)} in / {formatTokens(usage.output_tokens)} out
            </span>
          )}
          {usage && <span>{formatCost(usage.cost_usd)}</span>}
          {/* Rendered unconditionally: the ticker holds its own width and shows
              em-dashes until the first usage frame, so the footer never reflows. */}
          <LiveFootprint usage={usage} />
          <Link to={`/runs/${runId}`} className="chat-viewrun">
            view run
          </Link>
        </div>
      </div>
    </div>
  )
}

// ── Composer ──────────────────────────────────────────────────────────────

const MAX_TEXTAREA_PX = 200

function Composer({
  value,
  onChange,
  onSend,
  disabled,
  error,
  autoFocus,
}: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  disabled: boolean
  error: string | null
  autoFocus?: boolean
}) {
  const ref = useRef<HTMLTextAreaElement>(null)

  // Auto-grow up to a cap, then scroll.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_PX)}px`
  }, [value])

  // Autofocus, and refocus when a turn completes.
  useEffect(() => {
    if (!disabled) ref.current?.focus()
  }, [disabled])

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  return (
    <div className="chat-composer">
      {error && <div className="error-text" style={{ marginBottom: 8 }}>{error}</div>}
      <div className={`chat-inputbox${disabled ? ' disabled' : ''}`}>
        <textarea
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          placeholder={disabled ? 'Waiting for bench…' : 'Message bench…'}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          autoFocus={autoFocus}
        />
        <button
          className="chat-send"
          onClick={onSend}
          disabled={disabled || !value.trim()}
          aria-label="Send message"
          title="Send (Enter)"
        >
          {disabled ? <span className="chat-send-dot pulse" /> : <SendIcon />}
        </button>
      </div>
    </div>
  )
}

// ── Icons ─────────────────────────────────────────────────────────────────

function SendIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

function PanelIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: '-1px' }}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}
