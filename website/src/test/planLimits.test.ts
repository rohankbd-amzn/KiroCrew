/**
 * The plan windows on `GET /api/usage/kiro` are the provider's own numbers, so
 * the parser keeps what was sent and drops what was not: a missing or malformed
 * window is null (no reading), never 0%.
 */
import { describe, expect, it } from 'vitest'
import { parsePlanLimitReading, parsePlanLimits } from '../api/planLimits'

describe('parsePlanLimitReading', () => {
  it('reads a Codex block: seconds become milliseconds and the window length is kept', () => {
    expect(parsePlanLimitReading({
      five_hour: { used_percent: 10, resets_at: 1793971700, window_minutes: 300, stale: false },
      weekly: { used_percent: 63, resets_at: 1794377116, window_minutes: 10080, stale: true },
      captured_at: '2026-11-06T10:00:00Z',
      source_mtime: 1,
    })).toEqual({
      fiveHour: { usedPercent: 10, resetsAt: 1793971700_000, windowMinutes: 300, stale: false, capturedAt: Date.parse('2026-11-06T10:00:00Z') },
      weekly: { usedPercent: 63, resetsAt: 1794377116_000, windowMinutes: 10080, stale: true, capturedAt: Date.parse('2026-11-06T10:00:00Z') },
      status: null,
      statusResetsAt: null,
      capturedAt: Date.parse('2026-11-06T10:00:00Z'),
    })
  })

  it('reads a Claude Code reading: each window keeps its own report time and the status is kept', () => {
    const reading = parsePlanLimitReading({
      five_hour: { used_percent: 24, resets_at: 1788465600, window_minutes: null, stale: false, captured_at: '2026-11-10T15:00:00Z' },
      weekly: null,
      status: 'allowed_warning',
      captured_at: '2026-11-10T15:00:00Z',
    })
    expect(reading?.fiveHour).toEqual({ usedPercent: 24, resetsAt: 1788465600_000, windowMinutes: null, stale: false, capturedAt: Date.parse('2026-11-10T15:00:00Z') })
    expect(reading?.weekly).toBeNull()
    expect(reading?.status).toBe('allowed_warning')
  })

  it('a block with no windows is still a reading, so the row can say there are no plan limits', () => {
    expect(parsePlanLimitReading({ five_hour: null, weekly: null, captured_at: '2026-11-06T10:00:00Z' }))
      .toEqual({ fiveHour: null, weekly: null, status: null, statusResetsAt: null, capturedAt: Date.parse('2026-11-06T10:00:00Z') })
  })

  it.each([
    ['a string percent', { used_percent: '10' }],
    ['a negative percent', { used_percent: -1 }],
    ['a non-finite percent', { used_percent: Number.NaN }],
    ['no percent', { resets_at: 1 }],
    ['an array', [10]],
  ])('drops a window with %s', (_label, window) => {
    expect(parsePlanLimitReading({ five_hour: window })?.fiveHour).toBeNull()
  })

  it('drops an unknown status and unusable times', () => {
    const reading = parsePlanLimitReading({
      five_hour: { used_percent: 5, resets_at: 'soon', window_minutes: 0 },
      status: 'mystery',
      captured_at: 'not a date',
    })
    expect(reading).toEqual({
      fiveHour: { usedPercent: 5, resetsAt: null, windowMinutes: null, stale: false, capturedAt: null },
      weekly: null,
      status: null,
      statusResetsAt: null,
      capturedAt: null,
    })
  })

  it("keeps a status's reset time, and only beside a status", () => {
    expect(parsePlanLimitReading({ status: 'rejected', status_resets_at: 1788465600 })?.statusResetsAt).toBe(1788465600_000)
    expect(parsePlanLimitReading({ status: 'rejected', status_resets_at: 'soon' })?.statusResetsAt).toBeNull()
    expect(parsePlanLimitReading({ status: null, status_resets_at: 1788465600 })?.statusResetsAt).toBeNull()
  })

  it.each([null, undefined, 'x', 3, []])('no object is no reading: %s', raw => {
    expect(parsePlanLimitReading(raw)).toBeNull()
  })
})

describe('parsePlanLimits', () => {
  it('reads both providers, and an older gateway without the keys as no readings', () => {
    expect(parsePlanLimits({})).toEqual({ claude: null, codex: null })
    expect(parsePlanLimits(undefined)).toEqual({ claude: null, codex: null })
    expect(parsePlanLimits({ codex: { five_hour: { used_percent: 1 } } }).codex?.fiveHour?.usedPercent).toBe(1)
  })
})
