/**
 * Screenshot harness for the Slack setup wizard (Settings ▸ Messaging channels ▸ Slack).
 *
 * Loads the REAL built SPA with canned Slack config, manifest and install
 * status, then walks the wizard from "have a workspace" to "send a test
 * message": one shot per step, each with its schematic picture, in the dark and
 * light themes. Every scene asserts the step and its link before it shoots, so
 * a broken render fails the run rather than producing a plausible PNG.
 *
 * Credentials here are placeholders; they only reach the stubbed writer.
 *
 * Usage: node scripts/capture-slack-setup-wizard.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/slack-setup-wizard'
mkdirSync(OUT, { recursive: true })

const APP = 'A0123ABCD'
const TEAM = 'T0456EFGH'
const STATUS = {
  features: [], required_scopes: [], required_events: [], required_features: [], feature_changes: {},
  install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: APP, team_id: TEAM, reason: '' },
  app_manifest_url: `https://api.slack.com/apps/${APP}/app-manifest`,
  app_basic_info_url: `https://api.slack.com/apps/${APP}/general`,
  app_dm_url: `https://slack.com/app_redirect?app=${APP}&team=${TEAM}`,
}

function check(label, cond) {
  if (!cond) throw new Error(`assertion failed: ${label}`)
}

/** A mutable Slack config the stubbed writer updates, so the wizard advances. */
function makeState() {
  return {
    connected: false, connect_error: '', configured: false, read_only: false,
    bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
    owner_id: '', command: 'kirocrew', allowed_enterprise_ids: [], reactions_enabled: true,
    show_thinking: false, session_folder: '', auto_link_sessions: false, show_in_session_list: true,
  }
}

async function load(context, theme, state) {
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    const method = route.request().method()
    if (path === '/api/slack/config' && method === 'GET') return json(route, state), true
    if (path === '/api/slack/config' && method === 'PUT') {
      const body = route.request().postDataJSON() || {}
      if (body.bot_token) { state.bot_token_set = true; state.bot_token_preview = 'xoxb-…abcd' }
      if (body.app_token) { state.app_token_set = true; state.app_token_preview = 'xapp-…abcd'; state.configured = true }
      if (body.owner_id) state.owner_id = body.owner_id
      if (body.command) state.command = body.command
      return json(route, { ok: true, restart_required: false, reconnect_required: !!(body.bot_token || body.app_token || body.owner_id), verify_warning: '' }), true
    }
    if (path === '/api/slack/reconnect') { state.connected = true; return json(route, { connected: true, connect_error: '' }), true }
    if (path === '/api/slack/manifest') {
      const alias = new URL(route.request().url()).searchParams.get('alias') || 'kirocrew'
      return json(route, {
        alias, manifest: `display_information:\n  name: KiroCrew-${alias}\n`, // brand-ok: the app name slack-manifest.yaml renders
        create_url: 'https://api.slack.com/apps?new_app=1&manifest_yaml=x',
        command: alias === 'kirocrew' ? 'kirocrew' : `kirocrew-${alias.toLowerCase()}`, features: [],
      }), true
    }
    if (path === '/api/slack/manifest/status') return json(route, state.bot_token_set ? STATUS : { ...STATUS, install: { ...STATUS.install, status: 'not_configured', app_id: '', team_id: '' }, app_basic_info_url: null, app_dm_url: null, app_manifest_url: null }), true
    if (path === '/api/governance/channels') return json(route, { slack: true }), true
    if (path === '/api/config/schema') return json(route, { entries: [{ path: 'slack.command', type: 'string' }] }), true
    return false
  }
  await stubDashboardApi(page, { theme, extra, localStorageEntries: { 'mc-privacy-notice-v1': '1' } })
  await page.goto(context._base + '/settings/channels/slack', { waitUntil: 'domcontentloaded' })
  // Opt-in: the panel opens with the wizard closed; its button opens it.
  await page.getByTestId('slack-wizard-open').waitFor({ state: 'visible', timeout: 20_000 })
  await page.screenshot({ path: join(OUT, `0-panel-default-${theme}.png`) })
  await page.getByTestId('slack-wizard-open').click()
  await page.getByTestId('slack-wizard').waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(800)
  return page
}

async function shoot(page, name) {
  const wizard = page.getByTestId('slack-wizard')
  await wizard.scrollIntoViewIfNeeded()
  await page.mouse.move(2, 2)
  const card = wizard.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
  await (await card.count() ? card : wizard).screenshot({ path: join(OUT, name) })
}

const stepOf = page => page.getByTestId('slack-wizard').getAttribute('data-step')

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
      context._base = base
      const state = makeState()
      const page = await load(context, theme, state)
      check('opens on the workspace step', (await stepOf(page)) === 'workspace')
      check('workspace picture', await page.getByTestId('slack-wizard-diagram-workspace').isVisible())
      await shoot(page, `1-workspace-${theme}.png`)

      await page.getByRole('button', { name: 'I have a workspace' }).click()
      await page.getByTestId('slack-wizard-alias').fill('jdoe')
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard-create"]')?.getAttribute('aria-disabled') === 'false')
      await page.getByText('Slack app name: KiroCrew-jdoe').waitFor() // brand-ok: the rendered app name
      check('create picture', await page.getByTestId('slack-wizard-diagram-create').isVisible())
      await shoot(page, `2-create-${theme}.png`)
      // The link opens Slack in a new tab; close it, nothing leaves this machine.
      await context.route('https://api.slack.com/**', r => r.abort())
      const popup = context.waitForEvent('page')
      await page.getByTestId('slack-wizard-create').click()
      await (await popup).close()
      await page.waitForFunction(() => true)

      await page.getByRole('button', { name: 'I created the app' }).click()
      await page.getByTestId('slack-wizard-bot-token').fill('xoxb-placeholder')
      check('bot token picture', await page.getByTestId('slack-wizard-diagram-bot_token').isVisible())
      await shoot(page, `3-bot-token-${theme}.png`)

      await page.getByRole('button', { name: 'Verify and save' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'app_token')
      await page.waitForFunction(u => document.querySelector('[data-testid="slack-wizard-basic-info"]')?.getAttribute('href') === u, STATUS.app_basic_info_url)
      await page.getByTestId('slack-wizard-app-token').fill('xapp-placeholder')
      await shoot(page, `4-app-token-${theme}.png`)

      await page.getByRole('button', { name: 'Verify and save' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'owner')
      await page.getByTestId('slack-wizard-owner').fill('U0123ABC456')
      await page.waitForTimeout(400)
      await shoot(page, `5-owner-${theme}.png`)

      await page.getByRole('button', { name: 'Save and connect' }).click()
      await page.waitForFunction(() => document.querySelector('[data-testid="slack-wizard"]')?.getAttribute('data-step') === 'test')
      await page.waitForFunction(u => document.querySelector('[data-testid="slack-wizard-dm"]')?.getAttribute('href') === u, STATUS.app_dm_url)
      check('the Create click saved the alias command', state.command === 'kirocrew-jdoe')
      await shoot(page, `6-test-${theme}.png`)
      await page.getByRole('button', { name: 'I got a reply' }).click()
      await page.getByText('Slack is set up.').waitFor()
      await shoot(page, `7-done-${theme}.png`)
      // The whole panel, with the wizard open above the form, then closed.
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({ path: join(OUT, `8-panel-wizard-open-${theme}.png`), fullPage: true })
      await page.getByTestId('slack-wizard-close').click()
      await page.getByTestId('slack-wizard-open').waitFor()
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({ path: join(OUT, `9-panel-wizard-closed-${theme}.png`) })
      await context.close()
      console.log(`${theme}: OK`)
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
