/**
 * The account plan windows that an agent provider reports about itself, as
 * `GET /api/usage/kiro` carries them under `claude` and `codex`.
 *
 * These are the PROVIDER'S numbers for the whole account (Claude Code's 5-hour
 * and 7-day windows, Codex's primary and secondary windows), not this
 * install's own token or cost totals. The server reads them from frames and
 * logs the harness already writes; nothing here invents a value. A field the
 * provider did not send stays `null`, so a missing reading is "no reading",
 * never 0%.
 */

/** The providers whose plan windows the usage route carries, in display order. */
export const PLAN_LIMIT_PROVIDERS = ['claude', 'codex'] as const
export type PlanLimitProvider = typeof PLAN_LIMIT_PROVIDERS[number]

/** One usage window, as the provider reported it. */
export interface PlanLimitWindow {
  /** Share of the window's limit used, in percent. Can pass 100 when the provider says so. */
  usedPercent: number
  /** When the window resets, in epoch milliseconds; null when the provider did not say. */
  resetsAt: number | null
  /** The window's length, when the provider reports one (Codex does, Claude Code does not). */
  windowMinutes: number | null
  /** The reset time passed after this reading, so the percentage describes an earlier window. */
  stale: boolean
  /** When this window was reported, in epoch milliseconds; null when unknown. */
  capturedAt: number | null
}

export interface PlanLimitReading {
  fiveHour: PlanLimitWindow | null
  weekly: PlanLimitWindow | null
  /** Claude Code's own verdict on the newest request; Codex sends none. */
  status: 'allowed' | 'allowed_warning' | 'rejected' | null
  /** When the limit that `status` describes resets, in epoch milliseconds; past it the status says nothing. */
  statusResetsAt: number | null
  /** When the newest part of the reading was reported, in epoch milliseconds. */
  capturedAt: number | null
}

export type PlanLimits = Record<PlanLimitProvider, PlanLimitReading | null>

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** An ISO timestamp as epoch milliseconds, or null when it does not parse. */
function instant(value: unknown): number | null {
  if (typeof value !== 'string' || !value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

function parseWindow(raw: unknown, readingCapturedAt: number | null): PlanLimitWindow | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const w = raw as Record<string, unknown>
  const used = finite(w.used_percent)
  if (used === null || used < 0) return null
  const resetsAt = finite(w.resets_at)
  const minutes = finite(w.window_minutes)
  return {
    usedPercent: used,
    resetsAt: resetsAt !== null && resetsAt > 0 ? resetsAt * 1000 : null,
    windowMinutes: minutes !== null && minutes > 0 ? minutes : null,
    stale: w.stale === true,
    capturedAt: instant(w.captured_at) ?? readingCapturedAt,
  }
}

const STATUSES = new Set(['allowed', 'allowed_warning', 'rejected'])

/** One provider's reading from the route's payload, or null when there is none. */
export function parsePlanLimitReading(raw: unknown): PlanLimitReading | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const capturedAt = instant(r.captured_at)
  const status = typeof r.status === 'string' && STATUSES.has(r.status)
    ? r.status as PlanLimitReading['status']
    : null
  const statusResetsAt = finite(r.status_resets_at)
  return {
    fiveHour: parseWindow(r.five_hour, capturedAt),
    weekly: parseWindow(r.weekly, capturedAt),
    status,
    statusResetsAt: status !== null && statusResetsAt !== null && statusResetsAt > 0 ? statusResetsAt * 1000 : null,
    capturedAt,
  }
}

/** Both providers' readings from a `GET /api/usage/kiro` body. */
export function parsePlanLimits(body: { claude?: unknown; codex?: unknown } | null | undefined): PlanLimits {
  return {
    claude: parsePlanLimitReading(body?.claude),
    codex: parsePlanLimitReading(body?.codex),
  }
}
