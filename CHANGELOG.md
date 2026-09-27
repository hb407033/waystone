# Changelog

Formatted per [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/); versioning follows [Semantic Versioning](https://semver.org/lang/zh-CN/).

## [Unreleased]

## [0.6.2] - 2026-09-27

### Improved

- The client reuses connections in-process, keeping them idle for 120 seconds; long-running MCP services no longer do a TLS handshake on every call.

## [0.6.1] - 2026-09-27

### Improved

- Recall now performs a single vector search; conflict prompts only query pending proposals. Pre-publish duplicate checks no longer embed the body text; the server reuses its connection to Mem0.
- Retraction and indexing now lock per record, so a stalled Mem0 no longer blocks all writes.
- `/ready` caches successful results for 30 seconds; concurrent requests share one probe; container health checks now use `/health`, logs rotate at 10 MB × 3, and httpx logging is dropped to warning level.
- Sessions auto-renew with use within 30 days; expiry messages include the re-login command directly.
- Client timeouts: 5s to connect, 30s for responses (120s for rebuilds and retractions); write timeouts report "result unconfirmed"; device login polling keeps waiting through network jitter.
- The device authorization page auto-verifies the requesting device on open; submitting without verification first shows the device and asks for a second confirmation, and the approval applies to the verified authorization code.
- Ops docs describe the bcrypt verification overhead of Mem0's regular API keys; recommend switching to `ADMIN_API_KEY`.

## [0.6.0] - 2026-09-15

### Added

- Remote MCP connector: with `PUBLIC_URL` set, Claude web, Desktop, Cowork, mobile, Claude Code, and Codex can connect directly via `<server>/mcp`, authenticating with OAuth (dynamic client registration, always treated as public clients; PKCE S256; refresh tokens rotate by family, and replay invalidates the entire family).
- The authorization page shows the app's self-reported name (unverified), callback address, and permission scopes; callbacks are restricted to Claude-hosted callbacks, local loopback addresses, and explicitly configured addresses.
- `waystone connections` / `waystone disconnect` list and revoke remote authorizations; authorization approval, rejection, replay, revocation, and disconnection are written to the audit trail.
- Registration, token, and authorization endpoints are rate-limited by source IP, with global caps on pending clients and authorization requests.

### Improved

- Skill: when wrapping up (the user makes a call, finishes an assigned task, or says winding-down remarks), the agent proactively organizes confirmed conclusions into candidate memories and asks whether to save them, publishing only with explicit user consent.
- Skill: before creating a project, list joined projects; when a same or similar name exists, ask whether to bind it or create a new one, so invited members don't accidentally create duplicate projects.
- Skill: when saving documents, first distill them into self-contained facts and save them one by one; headings don't become standalone entries, topics carry no numeric suffixes, choose kind explicitly by content, and no longer upload the whole document chunked by paragraph or character count.

## [0.5.0] - 2026-09-14

First open-source release. Previously used and iterated internally by a small team over four versions.

### Added

- Per-project isolated team memory service: three roles — owner, collaborator, read-only; invitation-link signup; browser device-code login.
- Local offline preview before publishing; both client and server block obvious credentials (tolerates JSON/YAML styles, scans all text fields).
- Edits to the same topic, environment, and branch become pending proposals for the owner to accept, rebase, or reject; rejection is a terminal state.
- Scope: environment, applicable branch, source version; recall first filters candidates by permission and scope, then runs vector search within the candidates, verifying results back in SQL.
- `handoff` records expire by default after 7 days; expired active records and proposals are automatically invalidated, and the expiry audit is recorded as `system`.
- Retract mistakenly published content: wipe the body, replace the content hash, delete vectors (record vector IDs before deletion, verify afterward, skip deletion on ownership mismatch); republishing the same topic after retraction still requires owner confirmation.
- Archived projects can still rebuild their search indexes.
- 12 stdio MCP tools, a CLI, and an Agent Skill.
- Logins rate-limited by real source IP; Caddy config is Cloudflare-compatible and prevents forged forwarding headers.
- Readiness probe, scheduled backups, isolated recovery drills, off-site pulls, and email alert scripts.

### Notes

- The client ships with no default server address: `waystone login` requires an explicit `--server`, or set `WAYSTONE_SERVER`.
