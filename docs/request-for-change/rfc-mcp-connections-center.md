---
title: MCP Connections Center — credentials, last error and fix hints, and a curated list on the MCP Servers tab
status: draft
author: RohanK6
created: 2026-11-19
last-audited: 2026-11-19
audited-at: 3cc96a36de
doc-pr: null
implementation-prs: []
tracking-issues: [38054]
supersedes: []
superseded-by: []
---

# RFC: MCP Connections Center

- Status: draft. Nothing built; this document changes no code. Answers
  [#38054](https://github.com/kirodotdev/KiroCrew/issues/38054), where a
  maintainer asked for a request-for-change before any of these stages is
  built. Acceptance is requested from a maintainer.
- Author: RohanK6
- Claims about today's code were checked at `3cc96a36de` (main).

## 1. Summary

Stage 1, a per-row **Test** button on Customize → MCP Servers, shipped in
[#38055](https://github.com/kirodotdev/KiroCrew/pull/38055). This RFC proposes the
three stages after it, each its own pull request:

| Stage | What a person gets | Moves a boundary? |
|---|---|---|
| 2 | Enter a server's key on its row, store it in the vault, test it | Yes: a browser write of a secret, and a probe that resolves one |
| 3 | The last error of each server, with a short fix hint | No |
| 4 | A curated, ordered "popular servers" list in Add Server | No |

Stage 2 is the reason this is an RFC: it changes where a secret is written
from and who may resolve it.

## 2. Motivation: what main does today

Every claim below was checked at `3cc96a36de`.

- **Keys are named, not entered.** `McpBrowserModal` lists a discovered
  server's required env names after install
  (`website/src/components/McpBrowserModal.tsx`, `requiredEnv`). It has no
  field to type a value, so the person edits a config file by hand.
- **The vault and its write route exist.** `SecretVault`
  (`src/kiro_crew/secrets/vault.py`) is an encrypted per-entry store.
  `POST /api/secrets` writes it, and its handler `api_secrets_set` is
  owner-only through `_owner_only`
  (`src/kiro_crew/dashboard/handlers/secrets.py`).
- **A server env can point at the vault.** A `secret://NAME` value is resolved
  in memory at spawn time, and the file keeps only the reference
  (`src/kiro_crew/mcp_gateway/secret_uri.py`, module docstring).
- **The dashboard cannot test such a server.** `probe_server` does not resolve a
  `secret://` value. A server whose env holds one reports `outdated` with the
  reason "not probed: env … holds a secret:// reference", or `routed` when the
  running gateway serves it (`probe_server` and `secret_reference_refusal` in
  `src/kiro_crew/mcp_discovery_probe.py`).
  So after a person stores a key the stage 1 Test button cannot check it.
- **The last error is saved but not shown.** A row's `error` is the latest
  probe's (the `mcp-probe-error` notice in `website/src/pages/overview/McpTab.tsx`).
  The quarantine store already keeps more: for an `error` or `timeout` verdict,
  `record_verdicts` in `src/kiro_crew/mcp_quarantine.py` saves `last_status`,
  `last_error` and `last_failed_at` to `mcp-quarantine.json`, and an `ok`
  verdict clears the record. `_state_from` reads them back as `lastError`, but
  `_annotate_quarantine` in `src/kiro_crew/dashboard/handlers/mcp_probe.py`
  copies only `probeFailures` and `probeFailing` onto a row. No row offers a fix
  hint.
- **The popular list is eight bundled rows.** The Add Server modal shows the
  `mcp_servers` rows of `src/kiro_crew/data/discovery_featured.json` when the
  search is empty (the `featured: true` query in `McpBrowserModal.tsx`, and
  `_featured_results` in `src/kiro_crew/dashboard/handlers/mcp_discover.py`). The rows carry
  `id`, `name`, `provider`, `title`, `description`, `repo_url` and `methods`; no
  order and no category.

## 3. Goals

1. A person can add a key for a server on its row, without editing a file.
2. The key lands in the vault and the server's env holds only a `secret://`
   reference.
3. The Test button can check a server whose env holds a reference.
4. A failing row shows its last error and one short, actionable hint.
5. Add Server opens on a short, ordered list of servers people use most.

## 4. Non-goals

- A new secret store. Stage 2 uses `SecretVault` and `POST /api/secrets`.
- Showing a stored value back in the browser.
- A remote catalog or ratings. Stage 4 edits the bundled file only.
- OAuth sign-in. Connections already owns it
  (`docs/system-specs/modules/connections.md`).

## 5. Design

### 5.1 Stage 2: enter a key on the row

**Write path.** The row's form sends the value to the existing
`POST /api/secrets`, under a name the server derives from the server and key
names. The server's env entry is then set to `secret://<that name>` through the
existing `PUT /api/mcp/servers/{name}` (`api_mcp_server_detail` in
`src/kiro_crew/dashboard/handlers/mcp.py`, which accepts an `env` body). No new route writes a secret, and no raw value
reaches `mcp.json`.

**Who may write.** Owner only for a browser caller, as both routes already
are (`api_secrets_set`'s `_owner_only`; `api_mcp_server_detail` gates a
cookie caller on the owner). No App
Kit or internal caller gains a new way to set a server's env.

**Request safety.** Both routes keep the request checks they have today; this
stage adds none and removes none. The form never echoes the value back, and the
field is cleared after a successful save. Q4 asks a maintainer whether that is
enough for a browser form that writes a secret.

**Redaction.** The value never reaches a log line, an error message, the probe
cache or a probe row. Errors name the env key only, as
`secret_reference_refusal` already does.

**Testing a referenced server (the boundary move).** Today only the gateway
resolves a reference. Stage 2 proposes that the owner's Test of one server
(`POST /api/mcp/probe/{name}`) resolves that server's references in memory for
that one spawn, as the gateway does at spawn time. The background probe and the
full probe keep refusing. Open question Q1 asks whether a maintainer accepts
this; if not, stage 2 ships without it and the row says the key is saved but can
only be checked by a session.

### 5.2 Stage 3: last error and fix hints

Stage 3 reuses the record the quarantine store already writes; it adds no second
copy. `_annotate_quarantine` also copies the record's `lastError` and
`lastFailedAt` onto the row, so a row that is healthy now can still say when and
how it last failed, until an `ok` clears the record. A small, fixed table maps
known error shapes to one hint: a missing command, a timeout, an HTTP 401 or 403,
a missing required env name. Hints are catalog strings in all locales. An unknown
error shows no hint. The store's format and its write path are unchanged.

### 5.3 Stage 4: a curated popular list

`discovery_featured.json` rows gain two optional keys: `order` (an integer) and
`blurb` (a one-line reason to use the server). Rows without them keep today's
behavior and sort after ordered rows. The modal renders the blurb under the
title. No new data source.

## 6. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 3 | Last error and hints | A row with a quarantine record carries `lastError` and `lastFailedAt`; each mapped shape shows its hint and an unmapped error shows none; `mcp-quarantine.json`'s format is unchanged; a test pins each mapping |
| 4 | Ordered featured list | Ordered rows render first in order; a row without `order` still renders; the JSON schema test accepts both |
| 2a | Key form, no resolve | Saving a key creates a vault entry and sets `secret://` in env; the value never appears in a response, log or `mcp.json` (asserted with a planted synthetic credential) |
| 2b | Test resolves a reference | Blocked on Q1. The single-server Test resolves references for that spawn only; the full and background probes still refuse |

Stages 3 and 4 do not depend on this RFC's open questions and can land first.

## 7. Backward compatibility

- Stage 2a adds a form; existing env values, plain or referenced, are untouched.
- Stage 3 adds optional row fields read from the existing store; older clients ignore them.
- Stage 4 adds optional JSON keys; rows without them behave as today.
- Nothing is removed or renamed.

## 8. Security considerations

- Stage 2a writes a secret only through the owner-only `POST /api/secrets`.
- Stage 2b lets one owner-started probe hold a resolved value in memory. The
  value goes to the child's env only, as at gateway spawn; it is never written,
  logged or returned. This is the one boundary move and is gated on Q1.
- No change to `sandbox.py`, to the `.vault` leaf (HIDDEN, bind-masked in every
  sandbox mode), or to any governance ceiling.

## 9. Alternatives considered

1. **A new MCP credential route.** Rejected: it would be a second secret
   writer beside `POST /api/secrets`.
2. **Write the plain value into `mcp.json`.** Rejected: it puts a secret in a
   file the vault exists to keep it out of.
3. **Let every probe resolve references.** Rejected: background probes run
   without a person asking, so they should not touch secrets.

## 10. Open questions

- **Q1.** May the owner's single-server Test resolve `secret://` references for
  that one spawn? Stage 2b is blocked on this answer.
- **Q2.** Should a row show its last error after a later `ok`? Today an `ok`
  clears the quarantine record, so this RFC shows the last error only while a
  record exists. Keeping it past an `ok` would change the store's clearing rule.
- **Q3.** Who curates the stage 4 order: maintainers in the JSON file, or a
  generated count? This RFC proposes the JSON file.
- **Q4.** Are the existing request checks on `POST /api/secrets` and
  `PUT /api/mcp/servers/{name}` enough for a browser form that writes a secret,
  or does stage 2a need an extra confirmation step?
