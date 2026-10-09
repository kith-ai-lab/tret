/**
 * @tret/sdk — a typed client for the tret API.
 *
 * Zero runtime dependencies: built on the web-platform `fetch`, streams,
 * FormData and AbortController that Node >= 18, browsers and Electron share.
 */
export {
  NON_JSON,
  Tret,
  type CompleteOptions,
  type CompleteResult,
  type CreateRunOptions,
  type EventsOptions,
  type EventsSummary,
  type ListRunsOptions,
  type TretAuth,
  type TretOptions,
  type UploadData,
} from './client.js'
export {
  KNOWN_GATE_REASONS,
  TretAuthError,
  TretBudgetRefused,
  TretError,
  TretNotFound,
  TretRateLimited,
  TretStreamError,
  errorFromResponse,
  gateRefusal,
} from './errors.js'
export * from './events.js'
export { buildReceipt, formatReceipt, type Receipt, type ReceiptRouting, type ReceiptUsage } from './receipt.js'
export { parseSse, type SseFrame } from './sse.js'
export * from './types.js'
export type { components as OpenApiComponents, paths as OpenApiPaths } from './generated/openapi.js'
