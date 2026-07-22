import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api, type ChatMessage } from '../api/client'
import { useRunStream } from '../api/useRunStream'
import { ListItem } from '../components/shared/ListDetail'
import { shortModelName } from '../components/shared/RoutingBadge'

const EXAMPLE_PROMPTS = [
  'Is the vendor flood score for Alder Point (S-003) still trustworthy?',
  'Extract governance evidence from the uploaded questionnaire',
  'What datasets do we have?',
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

  const conversationsQuery = useQuery({
    queryKey: ['conversations'],
    queryFn: api.listConversations,
  })
  const conversations = conversationsQuery.data ?? []

  // Default to the most recent conversation once the list loads. Never
  // override an explicit selection (e.g. a just-created conversation that the
  // cached list doesn't contain yet).
  useEffect(() => {
    if (conversationsQuery.data === undefined || selectedId) return
    setSelectedId(conversationsQuery.data[0]?.id ?? null)
  }, [conversationsQuery.data, selectedId])

  const conversationQuery = useQuery({
    queryKey: ['conversation', selectedId],
    queryFn: () => api.getConversation(selectedId!),
    enabled: !!selectedId,
  })

  const stream = useRunStream(pending?.runId ?? null)

  // While a turn is in flight, poll the conversation until the assistant
  // message for that run is appended server-side. A manual interval (not
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

  const createMutation = useMutation({
    mutationFn: () => api.createConversation(),
    onSuccess: (conv) => {
      queryClient.setQueryData(['conversation', conv.id], conv)
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
      setSelectedId(conv.id)
    },
  })

  const sendMutation = useMutation({
    mutationFn: ({ conversationId, text }: { conversationId: string; text: string }) =>
      api.sendChatMessage(conversationId, text),
    onSuccess: (data) => {
      setPending({ conversationId: data.conversation_id, runId: data.run_id })
      // The user message is committed before the endpoint returns.
      queryClient.invalidateQueries({ queryKey: ['conversation', data.conversation_id] })
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })

  const inFlight = pending !== null || sendMutation.isPending

  const send = async (text: string) => {
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
  }

  const conversation = conversationQuery.data
  const showLive = !!pending && pending.conversationId === selectedId

  return (
    <div>
      <h1 className="view-title">Chat</h1>
      <div className="view-sub">
        Ask in plain language — the assistant delegates to the right harness task and every turn is
        an auditable run.
      </div>

      <div className="chat-layout">
        {/* LHS conversation list */}
        <div className="chat-list">
          <button
            className="btn btn-primary btn-sm"
            style={{ marginBottom: 10 }}
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending}
          >
            + New chat
          </button>
          {conversationsQuery.isLoading ? (
            <div className="empty pulse" style={{ padding: '8px 0' }}>
              Loading…
            </div>
          ) : conversations.length === 0 ? (
            <div className="empty" style={{ padding: '8px 0' }}>
              No conversations yet.
            </div>
          ) : (
            conversations.map((c) => (
              <ListItem
                key={c.id}
                active={c.id === selectedId}
                onClick={() => setSelectedId(c.id)}
                title={c.title ?? 'New conversation'}
                sub={`${c.message_count} msg${c.message_count === 1 ? '' : 's'} · ${relativeTime(c.updated_at)}`}
              />
            ))
          )}
        </div>

        {/* RHS thread */}
        <div className="chat-thread-wrap">
          {!selectedId && !createMutation.isPending ? (
            <div className="empty" style={{ margin: 'auto', textAlign: 'center' }}>
              Start a conversation — ask for an assessment and the assistant will run the right
              task.
              <div style={{ marginTop: 14 }}>
                <button className="btn btn-primary" onClick={() => createMutation.mutate()}>
                  + New chat
                </button>
              </div>
            </div>
          ) : (
            <>
              <Thread
                messages={conversation?.messages ?? []}
                loading={conversationQuery.isLoading}
                live={
                  showLive
                    ? { runId: pending.runId, text: stream.text, stream, error: stream.error }
                    : null
                }
                onSuggestion={send}
              />
              <Composer
                disabled={inFlight}
                value={draft}
                onChange={setDraft}
                onSend={() => void send(draft)}
                error={sendMutation.isError ? (sendMutation.error as Error).message : null}
              />
            </>
          )}
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
  onSuggestion,
}: {
  messages: ChatMessage[]
  loading: boolean
  live: {
    runId: string
    text: string
    stream: ReturnType<typeof useRunStream>
    error: string | null
  } | null
  onSuggestion: (text: string) => void
}) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const liveTextLength = live?.text.length ?? 0
  const liveItemCount = live?.stream.items.length ?? 0

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, liveTextLength, liveItemCount])

  if (loading) return <div className="chat-thread empty pulse">Loading conversation…</div>

  return (
    <div className="chat-thread">
      {messages.length === 0 && !live && (
        <div style={{ margin: 'auto', textAlign: 'center', maxWidth: 520 }}>
          <div className="empty" style={{ padding: '0 0 14px' }}>
            What should we look into? Try one of these:
          </div>
          <div className="row" style={{ flexWrap: 'wrap', justifyContent: 'center', gap: 8 }}>
            {EXAMPLE_PROMPTS.map((p) => (
              <button key={p} className="chip suggestion" onClick={() => onSuggestion(p)}>
                {p}
              </button>
            ))}
          </div>
        </div>
      )}

      {messages.map((m, i) =>
        m.role === 'user' ? (
          <div key={i} className="chat-msg user">
            {m.content}
          </div>
        ) : (
          <AssistantMessage key={i} message={m} />
        ),
      )}

      {live && <LiveTurn live={live} />}
      <div ref={bottomRef} />
    </div>
  )
}

function AssistantMessage({ message }: { message: ChatMessage }) {
  const navigate = useNavigate()
  const activity = message.activity ?? []
  return (
    <div className="chat-msg assistant">
      {activity.length > 0 && (
        <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
          {activity.map((a, i) => (
            <button
              key={i}
              className="chip activity"
              title={message.run_id ? 'View the run for this turn' : undefined}
              onClick={() => message.run_id && navigate(`/runs/${message.run_id}`)}
            >
              ⚙ {a.summary || a.tool}
            </button>
          ))}
        </div>
      )}
      <pre className="prose-stream">{message.content}</pre>
      <div className="chat-msg-footer">
        {message.status && message.status !== 'completed' && (
          <span style={{ color: 'var(--red)' }}>{message.status} · </span>
        )}
        {message.model_used ? `${shortModelName(message.model_used)} · ` : ''}
        {message.cost_usd !== undefined ? `$${message.cost_usd.toFixed(4)}` : ''}
        {message.run_id && (
          <>
            {' · '}
            <Link to={`/runs/${message.run_id}`}>view run</Link>
          </>
        )}
      </div>
    </div>
  )
}

function LiveTurn({
  live,
}: {
  live: {
    runId: string
    text: string
    stream: ReturnType<typeof useRunStream>
    error: string | null
  }
}) {
  const navigate = useNavigate()
  const toolCalls = live.stream.items.filter((i) => i.kind === 'tool_call')
  return (
    <div className="chat-msg assistant">
      {toolCalls.length > 0 && (
        <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
          {toolCalls.map((tc, i) => (
            <button
              key={i}
              className="chip activity"
              onClick={() => navigate(`/runs/${live.runId}`)}
              title="View the run for this turn"
            >
              ⚙{' '}
              {tc.tool === 'run_harness_task'
                ? `delegated ${String(tc.arguments['task_type'] ?? '?')}`
                : tc.tool}
            </button>
          ))}
        </div>
      )}
      {live.text ? (
        <pre className="prose-stream">{live.text}</pre>
      ) : (
        <div className="mono-label pulse" style={{ padding: '2px 0' }}>
          thinking…
        </div>
      )}
      {live.error && <div className="error-text" style={{ marginTop: 6 }}>{live.error}</div>}
      <div className="chat-msg-footer">
        <span className={live.stream.done ? '' : 'pulse'}>
          {live.stream.done ? 'finalizing…' : 'streaming'}
        </span>
        {' · '}
        <Link to={`/runs/${live.runId}`}>view run</Link>
      </div>
    </div>
  )
}

// ── Composer ──────────────────────────────────────────────────────────────

function Composer({
  value,
  onChange,
  onSend,
  disabled,
  error,
}: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  disabled: boolean
  error: string | null
}) {
  const ref = useRef<HTMLTextAreaElement>(null)

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
    <div className="chat-input-row">
      {error && (
        <div className="error-text" style={{ marginBottom: 8 }}>
          {error}
        </div>
      )}
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <textarea
          ref={ref}
          rows={2}
          value={value}
          disabled={disabled}
          placeholder={disabled ? 'Waiting for the assistant…' : 'Message — Enter to send, Shift+Enter for a new line'}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          autoFocus
        />
        <button className="btn btn-primary" onClick={onSend} disabled={disabled || !value.trim()}>
          Send
        </button>
      </div>
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
