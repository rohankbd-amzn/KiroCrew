import { useCallback, useEffect, useRef, useState } from 'react'
import { Gauge } from 'lucide-react'
import { Btn, Card, CardTitle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { Progress } from '../../components/ui/progress'
import { OverlayPortal, useCloseWhenCrewHidden } from '../../components/overlayRoot'
import { useHoverIntent } from '../../hooks/useHoverIntent'
import { ESCAPE_PRIORITY, useEscapeLayer } from '../../hooks/useEscapeLayer'
import { acpBackendName } from '../../api/acpBackend'
import {
  PLAN_LIMIT_PROVIDERS,
  type PlanLimitProvider,
  type PlanLimitReading,
  type PlanLimitWindow,
  type PlanLimits,
} from '../../api/planLimits'
import { metricColor } from '../../utils/metricColor'
import type { ErrorReport } from '../../utils/errorReport'
import { fmtDateTime, fmtDuration, fmtPercent, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

const EM_DASH = '\u2014'

type WindowKey = 'five_hour' | 'weekly'

/** One window as the row and the detail card show it, judged against one clock. */
export interface PlanLimitWindowView {
  key: WindowKey
  label: string
  window: PlanLimitWindow
  /** The reset passed after the reading, so its percentage describes an earlier window. */
  stale: boolean
}

/**
 * What a provider's row has to say. `none`: the provider has reported nothing
 * on this gateway. `no-windows`: it reported, but named no usage window (a
 * Codex sign-in with no plan limits, or a Claude Code status with no window).
 */
export type PlanLimitRowState =
  | { kind: 'none' }
  | { kind: 'no-windows'; reading: PlanLimitReading; status: PlanLimitReading['status'] }
  | { kind: 'windows'; reading: PlanLimitReading; status: PlanLimitReading['status']; windows: PlanLimitWindowView[] }

/**
 * The window's length as the provider states it. Codex sends `window_minutes`;
 * Claude Code sends none and names its windows `five_hour` and `seven_day`, so
 * those names are the length.
 */
function windowMinutes(key: WindowKey, window: PlanLimitWindow): number {
  return window.windowMinutes ?? (key === 'five_hour' ? 300 : 7 * 1440)
}

export function planLimitWindowLabel(key: WindowKey, window: PlanLimitWindow): string {
  const minutes = windowMinutes(key, window)
  if (minutes % 1440 === 0) return i18nT('pages.overview.planLimits.window_days', { days: minutes / 1440 })
  if (minutes % 60 === 0) return i18nT('pages.overview.planLimits.window_hours', { hours: minutes / 60 })
  return i18nT('pages.overview.planLimits.window_minutes', { minutes })
}

export function planLimitRowState(reading: PlanLimitReading | null, now: number): PlanLimitRowState {
  if (!reading) return { kind: 'none' }
  const windows: PlanLimitWindowView[] = []
  for (const [key, window] of [['five_hour', reading.fiveHour], ['weekly', reading.weekly]] as const) {
    if (!window) continue
    // The server judged `stale` when it answered; the report can be minutes
    // old by the time it renders, so the reset time is judged again here.
    const stale = window.stale || (window.resetsAt !== null && window.resetsAt <= now)
    windows.push({ key, label: planLimitWindowLabel(key, window), window, stale })
  }
  // A status describes the limit that resets at `statusResetsAt`; past that
  // reset it says nothing, so a `rejected` never outlives its window.
  const status = reading.statusResetsAt !== null && reading.statusResetsAt <= now ? null : reading.status
  return windows.length ? { kind: 'windows', reading, status, windows } : { kind: 'no-windows', reading, status }
}

/** At or past the limit, by the provider's own live verdict or its own percentage. */
function limitReached(state: PlanLimitRowState): boolean {
  if (state.kind === 'none') return false
  if (state.status === 'rejected') return true
  return state.kind === 'windows' && state.windows.some(w => !w.stale && w.window.usedPercent >= 100)
}

function usedColor(view: PlanLimitWindowView): string {
  if (view.stale) return 'text-muted'
  return metricColor(view.window.usedPercent / 100)
}

/** The bar's fill in the same tone as its percentage, so a window near its limit does not draw a calm bar. */
function barTone(view: PlanLimitWindowView): string {
  const tone = usedColor(view)
  // Applied on a wrapper: the bar's root is its first child and the fill is the root's.
  return tone === 'text-danger' ? '[&>div>div]:bg-danger' : tone === 'text-warn' ? '[&>div>div]:bg-warn' : ''
}

function fmtShare(percent: number): string {
  return fmtPercent(percent / 100, { maximumFractionDigits: 0 })
}

/**
 * Time left until a reset, in the two largest units: `1h 59m`, `3d 4h`. A
 * single unit would floor 1 h 59 min to "1h" and promise the reset an hour
 * late. The last minute reads `1m`, never `0m`.
 */
export function fmtCountdown(ms: number): string {
  const minutes = Math.max(0, Math.floor(ms / 60_000))
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  if (days > 0) return fmtDuration([[days, 'day'], [hours, 'hour']], { dropZero: true })
  if (hours > 0) return fmtDuration([[hours, 'hour'], [minutes % 60, 'minute']], { dropZero: true })
  return fmtDuration([[Math.max(1, minutes), 'minute']])
}

/**
 * The providers that get a row: any provider that has reported something, and
 * the default harness even before it has, so a Claude Code or Codex install
 * sees why its row is empty. An install that runs only Kiro gets no card.
 */
export function planLimitProviders(limits: PlanLimits | undefined, defaultBackend: string | undefined): PlanLimitProvider[] {
  return PLAN_LIMIT_PROVIDERS.filter(p => (limits?.[p] ?? null) !== null || defaultBackend === p)
}

type Anchor = { top: number; right: number; maxHeight: number; maxWidth: number }

/** Whether focus arrived from the keyboard. An engine without `:focus-visible` says yes. */
function focusIsVisible(el: Element): boolean {
  try {
    return el.matches(':focus-visible')
  } catch {
    return true
  }
}

function anchorBelow(el: HTMLElement | null): Anchor | null {
  const r = el?.getBoundingClientRect()
  if (!r) return null
  const top = r.bottom + 6
  const right = Math.max(8, window.innerWidth - r.right)
  // The card's right edge sits on the row's, so its width is what is left of
  // the viewport from there, with the same 8px margin on the left.
  return { top, right, maxHeight: Math.max(120, window.innerHeight - top - 8), maxWidth: Math.max(160, window.innerWidth - right - 8) }
}

/** Whether the row still sits where the card was anchored to it. */
function rowStillAt(el: HTMLElement | null, anchor: Anchor | null): boolean {
  const now = anchorBelow(el)
  return !!now && !!anchor && Math.abs(now.top - anchor.top) < 1 && Math.abs(now.right - anchor.right) < 1
}

function PlanLimitRow({ provider, reading, refreshFailed, refreshReport }: {
  provider: PlanLimitProvider
  reading: PlanLimitReading | null
  refreshFailed: boolean
  refreshReport: ErrorReport | undefined
}) {
  const name = acpBackendName({ id: provider })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const cardId = `plan-limits-card-${provider}`
  // A click or tap pins the card as a dialog; a hover or keyboard focus shows
  // the same card as a tooltip without moving focus.
  const [pinned, setPinned] = useState<Anchor | null>(null)
  const [focusAnchor, setFocusAnchor] = useState<Anchor | null>(null)
  // Hover stays off while the focus preview shows the card: an open hover
  // pushes a newer Escape layer, so one Escape would close only the hover and
  // leave the focus card on screen.
  const hover = useHoverIntent({ enabled: !pinned && focusAnchor === null, triggerRef, surfaceRef: cardRef })
  const closeHover = hover.close
  const [hoverAnchor, setHoverAnchor] = useState<Anchor | null>(null)
  useEffect(() => {
    setHoverAnchor(hover.open ? anchorBelow(triggerRef.current) : null)
  }, [hover.open])
  // Set while focus is handed back to the row after Escape, so that focus
  // does not reopen the card the user just dismissed.
  const refocusing = useRef(false)
  const anchor = pinned ?? hoverAnchor ?? focusAnchor
  const open = anchor !== null
  const closeAll = useCallback(() => {
    setPinned(null)
    setFocusAnchor(null)
    closeHover()
  }, [closeHover])
  // The card is placed from a snapshot of the row's box, so a resize, or a
  // scroll that moves the row, closes it rather than leaving it adrift. A
  // scroll inside the card, or of a pane the row is not in, keeps it open.
  const anchorRef = useRef<Anchor | null>(null)
  useEffect(() => { anchorRef.current = anchor }, [anchor])
  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => {
      if (cardRef.current?.contains(e.target as Node | null)) return
      if (rowStillAt(triggerRef.current, anchorRef.current)) return
      closeAll()
    }
    window.addEventListener('resize', closeAll)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      window.removeEventListener('resize', closeAll)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open, closeAll])
  // A pinned card is a non-modal dialog: focus moves in, a press outside or
  // Escape closes it, and Escape hands focus back to the row.
  useEffect(() => {
    if (!pinned) return
    cardRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t) || cardRef.current?.contains(t)) return
      setPinned(null)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [pinned])
  useEscapeLayer(pinned !== null || focusAnchor !== null, e => {
    if (e.isComposing) return 'yield'
    const refocus = pinned !== null
    closeAll()
    if (refocus) {
      refocusing.current = true
      triggerRef.current?.focus()
      refocusing.current = false
    }
    return 'take'
  }, ESCAPE_PRIORITY.flyout)
  useCloseWhenCrewHidden(open, closeAll)
  // The clock every stale verdict and countdown is judged against. It moves
  // when a new report arrives and every 30 s while the card is open; nothing
  // ticks while it is closed.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { setNow(Date.now()) }, [reading])
  useEffect(() => {
    if (!open) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [open])

  const state = planLimitRowState(reading, now)
  const reached = limitReached(state)
  // A pinned card is a dialog. A hover or focus preview is a tooltip, unless
  // it carries the failed-refresh notice, whose hand-off is a control: then it
  // is a non-modal dialog that does not take focus, as the system-metrics card is.
  const role: 'dialog' | 'tooltip' = pinned || refreshFailed ? 'dialog' : 'tooltip'

  return (
    <>
      <Btn
        ref={triggerRef}
        data-testid={`plan-limits-row-${provider}`}
        className="w-full min-h-11 justify-between gap-3 px-3 py-2 text-left"
        {...hover.triggerProps}
        aria-haspopup="dialog"
        aria-expanded={pinned !== null}
        aria-describedby={open && role === 'tooltip' ? cardId : undefined}
        onFocus={e => {
          // Keyboard focus only: a mouse click focuses the row too, and pins it.
          if (!refocusing.current && focusIsVisible(e.currentTarget)) setFocusAnchor(anchorBelow(e.currentTarget))
        }}
        onBlur={e => {
          hover.triggerProps.onBlur(e)
          if (!cardRef.current?.contains(e.relatedTarget as Node | null)) setFocusAnchor(null)
        }}
        onClick={() => {
          hover.close()
          setFocusAnchor(null)
          setPinned(prev => (prev ? null : anchorBelow(triggerRef.current)))
        }}
      >
        <span className="font-medium text-text">{name}</span>
        <RowSummary provider={provider} state={state} reached={reached} />
      </Btn>
      {anchor && (
        <OverlayPortal>
          <div
            ref={cardRef}
            id={cardId}
            role={role}
            {...(pinned ? {} : hover.surfaceProps)}
            // Only the dialog form carries a name. The tooltip is what the row's
            // aria-describedby resolves to, so a label there would replace its text.
            {...(role === 'dialog' ? { 'aria-label': i18nT('pages.overview.planLimits.details_label', { provider: name }) } : {})}
            tabIndex={-1}
            data-testid="plan-limits-detail"
            className="fixed z-[70] w-80 overflow-y-auto rounded-xl bg-card border border-border shadow-xl px-3 py-2.5 flex flex-col gap-2 text-[12px] text-text"
            style={{ top: anchor.top, right: anchor.right, maxHeight: anchor.maxHeight, maxWidth: anchor.maxWidth }}
          >
            <div className="text-[12px] font-semibold text-text-strong">
              {i18nT('pages.overview.planLimits.details_label', { provider: name })}
            </div>
            <PlanLimitDetail provider={provider} name={name} state={state} reached={reached} now={now} />
            {refreshFailed && (
              <ErrorNotice variant="inline" askAgent message={i18nT('pages.overview.planLimits.refresh_failed')} report={refreshReport} testId="plan-limits-refresh-failed" />
            )}
          </div>
        </OverlayPortal>
      )}
    </>
  )
}

function RowSummary({ provider, state, reached }: { provider: PlanLimitProvider; state: PlanLimitRowState; reached: boolean }) {
  if (state.kind === 'none') {
    return <span className="text-muted text-[12px]">{i18nT('pages.overview.planLimits.summary_no_reading')}</span>
  }
  if (state.kind === 'no-windows') {
    // Codex writes a window-less block for a sign-in with no plan limits.
    // Claude Code reports only for a subscription, so a status with no window
    // is a subscription that named no window this time, not one without limits.
    const tone = reached ? 'text-danger' : state.status === 'allowed_warning' ? 'text-warn' : 'text-muted'
    return (
      <span className={`text-[12px] ${tone}`}>
        {reached
          ? i18nT('pages.overview.planLimits.summary_limit_reached')
          : i18nT(provider === 'claude' ? 'pages.overview.planLimits.summary_no_window' : 'pages.overview.planLimits.summary_no_limits')}
      </span>
    )
  }
  return (
    <span className="flex flex-wrap items-baseline justify-end gap-x-3 gap-y-0.5 font-mono text-[12px] tabular-nums">
      {state.windows.map(view => (
        <span key={view.key} className={usedColor(view)}>
          {i18nT('pages.overview.planLimits.summary_window', { window: view.label, used: view.stale ? EM_DASH : fmtShare(view.window.usedPercent) })}
        </span>
      ))}
      {reached && <span className="font-body text-danger">{i18nT('pages.overview.planLimits.summary_limit_reached')}</span>}
    </span>
  )
}

/** A window reported a minute or more before the reading's newest part carries its own age. */
const OWN_AGE_MS = 60_000

function WindowDetail({ view, now, readingCapturedAt }: { view: PlanLimitWindowView; now: number; readingCapturedAt: number | null }) {
  const { window: w, stale } = view
  const left = Math.max(0, 100 - w.usedPercent)
  const ownAge = w.capturedAt !== null && readingCapturedAt !== null && readingCapturedAt - w.capturedAt >= OWN_AGE_MS
  const reset = w.resetsAt === null
    ? i18nT('pages.overview.planLimits.reset_unknown')
    : stale
      ? i18nT('pages.overview.planLimits.reset_passed', { time: fmtDateTime(w.resetsAt) })
      : i18nT('pages.overview.planLimits.resets_at', { time: fmtDateTime(w.resetsAt), countdown: fmtCountdown(w.resetsAt - now) })
  return (
    <div className="flex flex-col gap-1" data-testid={`plan-limits-window-${view.key}`}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-muted">{i18nT('pages.overview.planLimits.window_label', { window: view.label })}</span>
        <span className={`font-mono tabular-nums ${usedColor(view)}`}>
          {stale ? EM_DASH : i18nT('pages.overview.planLimits.used_left', { used: fmtShare(w.usedPercent), left: fmtShare(left) })}
        </span>
      </div>
      {!stale && (
        <div className={barTone(view)}>
          <Progress value={Math.min(100, w.usedPercent)} aria-label={i18nT('pages.overview.planLimits.window_label', { window: view.label })} />
        </div>
      )}
      <div className="text-[11px] text-muted">{reset}</div>
      {ownAge && w.capturedAt !== null && (
        <div className="text-[11px] text-muted">{i18nT('pages.overview.planLimits.reported_at', { relative: fmtRelative(w.capturedAt, { now }) })}</div>
      )}
    </div>
  )
}

function PlanLimitDetail({ provider, name, state, reached, now }: {
  provider: PlanLimitProvider
  name: string
  state: PlanLimitRowState
  reached: boolean
  now: number
}) {
  if (state.kind === 'none') {
    return <p className="text-muted">{i18nT(provider === 'claude' ? 'pages.overview.planLimits.no_reading_claude' : 'pages.overview.planLimits.no_reading_codex')}</p>
  }
  const { reading } = state
  return (
    <>
      {state.kind === 'windows'
        ? state.windows.map(view => <WindowDetail key={view.key} view={view} now={now} readingCapturedAt={reading.capturedAt} />)
        : <p className="text-muted">{i18nT(provider === 'claude' ? 'pages.overview.planLimits.no_windows_claude' : 'pages.overview.planLimits.no_windows_codex')}</p>}
      {reached && <p className="text-danger">{i18nT('pages.overview.planLimits.status_rejected', { provider: name })}</p>}
      {!reached && state.status === 'allowed_warning' && <p className="text-warn">{i18nT('pages.overview.planLimits.status_warning', { provider: name })}</p>}
      <div className="border-t border-border pt-1.5 flex flex-col gap-0.5 text-[11px] text-muted">
        <span>
          {reading.capturedAt === null
            ? i18nT('pages.overview.planLimits.reported_unknown')
            : i18nT('pages.overview.planLimits.reported_at', { relative: fmtRelative(reading.capturedAt, { now }) })}
        </span>
        <span>{i18nT(provider === 'claude' ? 'pages.overview.planLimits.source_claude' : 'pages.overview.planLimits.source_codex')}</span>
      </div>
    </>
  )
}

/**
 * The Usage view's plan-limits card: one row per agent provider that reports
 * its account's usage windows, each opening a detail card on hover, keyboard
 * focus, or a click or tap. The numbers are the provider's, for the account;
 * the token and cost cards below count only this install's turns.
 */
export function PlanLimitsCard({ limits, defaultBackend, refreshFailed, refreshReport }: {
  limits: PlanLimits | undefined
  /** `agent.acp_backend` of a LOADED config; undefined while it is unread. */
  defaultBackend: string | undefined
  /** The newest refresh of the usage report failed, so these readings are what the last one held. */
  refreshFailed: boolean
  /** The failed refresh's report, for the hand-off: the notice's message is the card's own wording. */
  refreshReport?: ErrorReport
}) {
  const providers = planLimitProviders(limits, defaultBackend)
  if (!providers.length) return null
  return (
    <Card data-testid="plan-limits-card">
      <CardTitle><Gauge className="lucide-inline" /> {i18nT('pages.overview.planLimits.title')}</CardTitle>
      <p className="text-[12px] text-muted mb-3">{i18nT('pages.overview.planLimits.intro')}</p>
      <div className="flex flex-col gap-2">
        {providers.map(p => (
          <PlanLimitRow key={p} provider={p} reading={limits?.[p] ?? null} refreshFailed={refreshFailed} refreshReport={refreshReport} />
        ))}
      </div>
    </Card>
  )
}
