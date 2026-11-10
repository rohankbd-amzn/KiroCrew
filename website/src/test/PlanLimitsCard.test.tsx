/**
 * The Usage view's plan-limits card: one row per agent provider, and a detail
 * card that a hover, keyboard focus, or a click or tap opens. Every state says
 * what the provider reported and nothing more: no reading, no plan limits, a
 * stale window, a window near its limit, a limit reached, a failed refresh.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { HOVER_OPEN_MS } from '../hooks/useHoverIntent'
import { PlanLimitsCard, fmtCountdown, planLimitProviders, planLimitRowState } from '../pages/overview/PlanLimitsCard'
import type { PlanLimitReading, PlanLimits } from '../api/planLimits'
import type { ErrorReport } from '../utils/errorReport'

// Shows which report the agent hand-off would carry.
vi.mock('../components/AskAgentButton', async importOriginal => ({
  ...(await importOriginal<typeof import('../components/AskAgentButton')>()),
  default: ({ report }: { report?: ErrorReport }) => <button type="button" data-testid="ask-agent" data-report-id={report?.id ?? ''}>Ask the agent</button>,
}))

const NOW = Date.parse('2026-11-10T15:00:00Z')
const HOUR = 3_600_000

function codex(fiveHourUsed: number, weeklyUsed: number, opts: { fiveHourResetsAt?: number } = {}): PlanLimitReading {
  return {
    fiveHour: { usedPercent: fiveHourUsed, resetsAt: opts.fiveHourResetsAt ?? NOW + 2 * HOUR, windowMinutes: 300, stale: false, capturedAt: NOW - 3 * 60_000 },
    weekly: { usedPercent: weeklyUsed, resetsAt: NOW + 4 * 24 * HOUR, windowMinutes: 10080, stale: false, capturedAt: NOW - 3 * 60_000 },
    status: null,
    statusResetsAt: null,
    capturedAt: NOW - 3 * 60_000,
  }
}

function claude(over: Partial<PlanLimitReading>): PlanLimitReading {
  return {
    fiveHour: { usedPercent: 40, resetsAt: NOW + HOUR, windowMinutes: null, stale: false, capturedAt: NOW },
    weekly: null,
    status: 'allowed',
    statusResetsAt: NOW + HOUR,
    capturedAt: NOW,
    ...over,
  }
}

/** Pin the row's box, so a test can move it the way a page scroll would. */
function placeRow(row: HTMLElement, top: number) {
  row.getBoundingClientRect = () => ({ top, bottom: top + 40, left: 20, right: 700, width: 680, height: 40, x: 20, y: top, toJSON: () => ({}) })
}

function limits(over: Partial<PlanLimits>): PlanLimits {
  return { claude: null, codex: null, ...over }
}

function mount(props: { limits?: PlanLimits; defaultBackend?: string; refreshFailed?: boolean; refreshReport?: ErrorReport }) {
  return render(
    <PlanLimitsCard limits={props.limits} defaultBackend={props.defaultBackend} refreshFailed={props.refreshFailed ?? false} refreshReport={props.refreshReport} />,
  )
}

function mouseIn(el: HTMLElement) {
  fireEvent.pointerEnter(el, { pointerType: 'mouse' })
  fireEvent.mouseEnter(el)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('which providers get a row', () => {
  it('an install that runs only Kiro and has no readings gets no card', () => {
    expect(planLimitProviders(limits({}), 'kas')).toEqual([])
    const { container } = mount({ limits: limits({}), defaultBackend: 'kas' })
    expect(container).toBeEmptyDOMElement()
  })

  it('a provider with a reading gets a row whatever the default harness is', () => {
    expect(planLimitProviders(limits({ codex: codex(1, 1) }), 'kas')).toEqual(['codex'])
  })

  it('the default harness gets a row before it has reported anything', () => {
    expect(planLimitProviders(limits({}), 'claude')).toEqual(['claude'])
  })
})

describe('the row', () => {
  it('shows each window the provider named, by its own length, with the used share', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(row).toHaveTextContent('Codex')
    expect(row).toHaveTextContent('5-hour 24%')
    expect(row).toHaveTextContent('7-day 63%')
  })

  it('colors a window near its limit and names a reached limit', () => {
    mount({ limits: limits({ codex: codex(95, 100) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(within(row).getByText('5-hour 95%')).toHaveClass('text-danger')
    expect(row).toHaveTextContent('Limit reached')
  })

  it('shows a dash, never the old share, for a window that reset after the reading', () => {
    mount({ limits: limits({ codex: codex(80, 10, { fiveHourResetsAt: NOW - 60_000 }) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(row).toHaveTextContent('5-hour \u2014')
    expect(row).not.toHaveTextContent('80%')
  })

  it('says there is no reading yet, not 0%', () => {
    mount({ limits: limits({}), defaultBackend: 'claude' })
    const row = screen.getByTestId('plan-limits-row-claude')
    expect(row).toHaveTextContent('No reading yet')
    expect(row).not.toHaveTextContent('%')
  })

  it('says a sign-in with no plan windows has no plan limits', () => {
    mount({ limits: limits({ codex: { fiveHour: null, weekly: null, status: null, statusResetsAt: null, capturedAt: NOW } }) })
    expect(screen.getByTestId('plan-limits-row-codex')).toHaveTextContent('No plan limits')
  })

  it('does not call a Claude Code status with no window "no plan limits"', () => {
    mount({ limits: limits({ claude: claude({ fiveHour: null, status: 'allowed_warning' }) }) })
    const row = screen.getByTestId('plan-limits-row-claude')
    expect(row).toHaveTextContent('No usage window')
    expect(row).not.toHaveTextContent('No plan limits')
    expect(within(row).getByText('No usage window')).toHaveClass('text-warn')
  })

  it('drops a rejected status once the reset it belongs to has passed', () => {
    mount({ limits: limits({ claude: claude({
      fiveHour: { usedPercent: 100, resetsAt: NOW - 60_000, windowMinutes: null, stale: false, capturedAt: NOW - 2 * HOUR },
      status: 'rejected',
      statusResetsAt: NOW - 60_000,
    }) }) })
    const row = screen.getByTestId('plan-limits-row-claude')
    expect(row).not.toHaveTextContent('Limit reached')
    expect(row).toHaveTextContent('5-hour \u2014')
    fireEvent.click(row)
    expect(screen.getByRole('dialog')).not.toHaveTextContent('a limit is reached')
  })

  it('keeps a rejected status until its reset', () => {
    mount({ limits: limits({ claude: claude({ status: 'rejected', statusResetsAt: NOW + HOUR }) }) })
    expect(screen.getByTestId('plan-limits-row-claude')).toHaveTextContent('Limit reached')
  })
})

describe('the detail card', () => {
  it('a hover shows used, left and the reset time as a tooltip', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    mouseIn(row)
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    const card = screen.getByRole('tooltip')
    expect(row).toHaveAttribute('aria-describedby', card.id)
    const fiveHour = within(card).getByTestId('plan-limits-window-five_hour')
    expect(fiveHour).toHaveTextContent('5-hour window')
    expect(fiveHour).toHaveTextContent('24% used · 76% left')
    // The hover delay ran, so 1 h 59 min and some seconds are left: two units, floored.
    expect(fiveHour).toHaveTextContent('Resets Nov 10, 2026, 5:00 PM (in 1h 59m)')
    expect(card).toHaveTextContent('Reported 3m ago')
    expect(card).toHaveTextContent('From the session logs that Codex writes on this machine.')
  })

  it('a click pins it as a dialog with focus inside, and Escape hands focus back without reopening it', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    fireEvent.click(row)
    const dialog = screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })
    expect(row).toHaveAttribute('aria-expanded', 'true')
    expect(document.activeElement).toBe(dialog)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('aria-expanded', 'false')
  })

  it('keyboard focus shows the card without moving focus, and Escape dismisses it', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    expect(document.activeElement).toBe(row)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('one Escape dismisses the focus card even after a hover or ArrowDown on the focused row', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    fireEvent.keyDown(row, { key: 'ArrowDown' })
    mouseIn(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
  })

  it('a tap pins the card, so a touch screen gets the same detail', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    fireEvent.pointerEnter(row, { pointerType: 'touch' })
    fireEvent.pointerDown(row, { pointerType: 'touch' })
    fireEvent.pointerUp(row, { pointerType: 'touch' })
    fireEvent.mouseEnter(row)
    fireEvent.click(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS * 2) })
    expect(screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })).toBeInTheDocument()
  })

  it('a scroll that moves the row closes the card, which is placed from a snapshot of the row', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 100)
    fireEvent.click(row)
    fireEvent.scroll(window)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    placeRow(row, 40)
    fireEvent.scroll(window)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a scroll inside the card keeps it open, so a tall card can be read to the end', () => {
    mount({ limits: limits({ codex: codex(24, 63) }) })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 100)
    fireEvent.click(row)
    const dialog = screen.getByRole('dialog')
    placeRow(row, 40)
    fireEvent.scroll(dialog)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it("gives a window its own age when it was reported well before the reading's newest part", () => {
    mount({ limits: limits({ claude: claude({
      weekly: { usedPercent: 13, resetsAt: NOW + 3 * 24 * HOUR, windowMinutes: null, stale: false, capturedAt: NOW - 2 * 24 * HOUR },
    }) }) })
    fireEvent.click(screen.getByTestId('plan-limits-row-claude'))
    expect(screen.getByTestId('plan-limits-window-weekly')).toHaveTextContent('Reported 2d ago')
    expect(screen.getByTestId('plan-limits-window-five_hour')).not.toHaveTextContent('Reported')
  })

  it('explains why a provider has no reading', () => {
    mount({ limits: limits({}), defaultBackend: 'claude' })
    fireEvent.click(screen.getByTestId('plan-limits-row-claude'))
    expect(screen.getByRole('dialog')).toHaveTextContent('only for a Claude subscription sign-in')
  })

  it('names a stale window and its reset instead of the old share', () => {
    mount({ limits: limits({ codex: codex(80, 10, { fiveHourResetsAt: NOW - 60_000 }) }) })
    fireEvent.click(screen.getByTestId('plan-limits-row-codex'))
    const fiveHour = screen.getByTestId('plan-limits-window-five_hour')
    expect(fiveHour).toHaveTextContent('after this reading')
    expect(fiveHour).not.toHaveTextContent('80%')
  })

  it("carries Claude Code's own status", () => {
    mount({ limits: limits({ claude: claude({ status: 'allowed_warning' }) }) })
    fireEvent.click(screen.getByTestId('plan-limits-row-claude'))
    const dialog = screen.getByRole('dialog', { name: 'Claude Code plan limits' })
    expect(dialog).toHaveTextContent('Claude Code reports that you are close to a limit.')
    expect(within(dialog).queryByTestId('plan-limits-window-weekly')).toBeNull()
  })

  it('a failed refresh is a notice with the hand-off, so even the hover form is a dialog', () => {
    mount({ limits: limits({ codex: codex(24, 63) }), refreshFailed: true })
    const row = screen.getByTestId('plan-limits-row-codex')
    mouseIn(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    const dialog = screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })
    expect(within(dialog).getByTestId('plan-limits-refresh-failed')).toHaveTextContent('Could not refresh. This is the last reading that loaded.')
    // The preview does not take focus, and the row is not described by a dialog.
    expect(document.activeElement).not.toBe(dialog)
    expect(row).not.toHaveAttribute('aria-describedby')
  })

  it("the failed refresh's hand-off carries the failed request's report", () => {
    const report: ErrorReport = { id: 'err-7', at: NOW, source: 'api', message: 'HTTP 502', status: 502 }
    mount({ limits: limits({ codex: codex(24, 63) }), refreshFailed: true, refreshReport: report })
    fireEvent.click(screen.getByTestId('plan-limits-row-codex'))
    const notice = within(screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })).getByTestId('plan-limits-refresh-failed')
    expect(within(notice).getByTestId('ask-agent')).toHaveAttribute('data-report-id', 'err-7')
  })
})

describe('fmtCountdown', () => {
  it.each([
    [119 * 60_000 + 59_000, '1h 59m'],
    [2 * HOUR, '2h'],
    [3 * 24 * HOUR + 4 * HOUR + 59 * 60_000, '3d 4h'],
    [30_000, '1m'],
    [-5_000, '1m'],
  ])('%d ms left reads %s', (ms, text) => {
    expect(fmtCountdown(ms)).toBe(text)
  })
})

describe('planLimitRowState', () => {
  it('judges a reset against the clock it is given, not only the server flag', () => {
    const state = planLimitRowState(codex(50, 50, { fiveHourResetsAt: NOW + 1000 }), NOW + 2000)
    expect(state.kind).toBe('windows')
    if (state.kind === 'windows') expect(state.windows.map(w => [w.key, w.stale])).toEqual([['five_hour', true], ['weekly', false]])
  })
})
