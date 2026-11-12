import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ApiError, lessonsApi } from '../../api/client'

// ── Lessons: a pack's or a harness's per-workspace memory ───────────────────
// A pack's lessons are what runs learned running it here (services/lessons.py);
// a harness's are proposed by a batch review. Either way only a workspace
// approver-or-higher decides (Approve/Reject); retiring an already-approved
// lesson is admin-only. The `list_pack_lessons`/`propose_pack_lesson` tools
// are what a run itself sees — this panel is the human side of that loop.
export type LessonsScope = { kind: 'pack'; packId: string } | { kind: 'harness'; harnessId: string }

export const ROLE_RANK: Record<string, number> = { analyst: 0, approver: 1, admin: 2, owner: 3 }

export function Lessons({ scope }: { scope: LessonsScope }) {
  const queryClient = useQueryClient()
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const role = meQuery.data?.role ?? 'analyst'
  const canReview = (ROLE_RANK[role] ?? 0) >= ROLE_RANK.approver
  const canRetire = (ROLE_RANK[role] ?? 0) >= ROLE_RANK.admin

  const isPack = scope.kind === 'pack'
  const scopeId = scope.kind === 'pack' ? scope.packId : scope.harnessId
  // The pack key stays 'pack-lessons' so existing invalidations keep working.
  const queryKey = [isPack ? 'pack-lessons' : 'harness-lessons', scopeId]
  const lessonsQuery = useQuery({
    queryKey,
    queryFn: () =>
      scope.kind === 'pack'
        ? lessonsApi.listLessons(scope.packId)
        : lessonsApi.listHarnessLessons(scope.harnessId),
  })
  const lessons = lessonsQuery.data ?? []
  const approved = lessons.filter((l) => l.status === 'approved')
  const proposed = lessons.filter((l) => l.status === 'proposed')
  const retired = lessons.filter((l) => l.status === 'retired')

  const reviewMutation = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      scope.kind === 'pack'
        ? lessonsApi.reviewLesson(scope.packId, id, approve)
        : lessonsApi.reviewHarnessLesson(scope.harnessId, id, approve),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey })
      // A batch review shows the same proposals; keep it in step.
      if (!isPack) {
        void queryClient.invalidateQueries({ queryKey: ['batch-evaluation'] })
        void queryClient.invalidateQueries({ queryKey: ['batch-scorecard'] })
      }
    },
  })
  const retireMutation = useMutation({
    mutationFn: (id: string) =>
      scope.kind === 'pack'
        ? lessonsApi.retireLesson(scope.packId, id)
        : lessonsApi.retireHarnessLesson(scope.harnessId, id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey })
      if (!isPack) {
        void queryClient.invalidateQueries({ queryKey: ['batch-evaluation'] })
        void queryClient.invalidateQueries({ queryKey: ['batch-scorecard'] })
      }
    },
  })
  const mutationError = (reviewMutation.error ?? retireMutation.error) as ApiError | null

  if (lessonsQuery.isLoading) return <div className="empty pulse">Loading lessons…</div>

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Lessons
      </div>
      <div className="view-sub" style={{ marginTop: -4, marginBottom: 10 }}>
        {isPack
          ? "What this pack has learned running in this workspace — approved lessons ride along in every run's prompt; proposals wait here for a reviewer."
          : "What batch reviews have suggested for this harness — approved lessons are added to the instructions of every future run of it; proposals wait here for a reviewer."}
      </div>

      {mutationError && (
        <div className="error-text" style={{ marginBottom: 10 }}>
          {mutationError.status === 403 ? 'Requires approver role or higher.' : mutationError.message}
        </div>
      )}

      {proposed.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="mono-label" style={{ marginBottom: 6, color: 'var(--amber)' }}>
            Pending review ({proposed.length})
          </div>
          <div className="stack" style={{ gap: 8 }}>
            {proposed.map((l) => (
              <div key={l.id} className="panel">
                <div className="mono-body">{l.text}</div>
                <div style={{ color: 'var(--text-muted)', fontSize: 'var(--fs-sm)', marginTop: 4 }}>
                  {l.rationale}
                </div>
                {canReview && (
                  <div className="row" style={{ marginTop: 8 }}>
                    <button
                      className="btn btn-approve"
                      disabled={reviewMutation.isPending}
                      onClick={() => reviewMutation.mutate({ id: l.id, approve: true })}
                    >
                      Approve
                    </button>
                    <button
                      className="btn btn-reject"
                      disabled={reviewMutation.isPending}
                      onClick={() => reviewMutation.mutate({ id: l.id, approve: false })}
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {approved.length === 0 && proposed.length === 0 ? (
        <div className="empty">No lessons recorded yet for this {isPack ? 'pack' : 'harness'} in this workspace.</div>
      ) : (
        approved.length > 0 && (
          <table className="mono-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Lesson</th>
                <th>Rationale</th>
                {canRetire && <th />}
              </tr>
            </thead>
            <tbody>
              {approved.map((l) => (
                <tr key={l.id}>
                  <td>{l.ordinal}</td>
                  <td>
                    {l.text}
                    {!l.in_effect && (
                      <span
                        className="badge badge-amber"
                        style={{ marginLeft: 8, fontSize: 'var(--fs-2xs)', padding: '1px 6px' }}
                        title="Over the 40-item / 4,000-character cap on what a run's prompt carries — approved, but not currently sent to any run."
                      >
                        over cap — not sent to runs
                      </span>
                    )}
                  </td>
                  <td style={{ color: 'var(--text-muted)' }}>{l.rationale}</td>
                  {canRetire && (
                    <td>
                      <button
                        className="btn btn-sm"
                        disabled={retireMutation.isPending}
                        onClick={() => retireMutation.mutate(l.id)}
                      >
                        Retire
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}

      {retired.length > 0 && (
        <div style={{ marginTop: 10, fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>
          {retired.length} retired lesson{retired.length === 1 ? '' : 's'} — no longer shown to runs.
        </div>
      )}
    </div>
  )
}

