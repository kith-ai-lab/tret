/** Synchronous send guard (Learning round R15).
 *
 *  React state (`inFlight`) only updates on the next render, so two Enters in
 *  the same tick both pass a state check — and on the new-session path the
 *  second one used to create a second session while the first awaited
 *  `createConversation`. A ref-backed lock closes that window: it is taken
 *  before the first await and released when the request settles. */
export interface SendLock {
  tryAcquire(): boolean
  release(): void
  readonly held: boolean
}

export function createSendLock(): SendLock {
  let held = false
  return {
    tryAcquire() {
      if (held) return false
      held = true
      return true
    },
    release() {
      held = false
    },
    get held() {
      return held
    },
  }
}

/** Run `task` only if the lock is free; release it when the task settles. */
export async function runExclusive<T>(lock: SendLock, task: () => Promise<T>): Promise<T | undefined> {
  if (!lock.tryAcquire()) return undefined
  try {
    return await task()
  } finally {
    lock.release()
  }
}

/** The one queued message (R16): sent when the run ends. */
export function shouldFlushQueue(queued: string | null, inFlight: boolean): boolean {
  return queued !== null && queued.trim() !== '' && !inFlight
}
