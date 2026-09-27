# Server deployment and operations

For small-team, single-instance self-hosting: one server, maintenance windows allowed, no high availability promised. For client setup, see [install.md](install.md).

## Deployment topology

- **Waystone service**: Docker container, port 8900 mapped only to the host's `127.0.0.1`, joined to the Docker network Mem0 is on, reaching Mem0 via `MEM0_URL`.
- **Reverse proxy**: handles HTTPS certificates and writes the real source IP into `X-Forwarded-For`; example config at [`deploy/Caddyfile.example`](../deploy/Caddyfile.example).
- **Data**: the SQLite database lives on the mounted `data/` volume and is the source of truth for all records; Mem0 is only a rebuildable vector index.
- **Credentials**: the Mem0 service API key is mounted into the container as a read-only file; members and agents never see it.

## First-time deployment

1. Deploy the [Mem0 official self-hosted service](https://docs.mem0.ai/open-source/setup), create an admin account, and generate a service API key for Waystone.
2. On the server, prepare `/opt/waystone` (the backup and monitoring scripts use this path by default), place `deploy/compose.yaml` there, and create the `data/` directory plus a `secrets/mem0_key` file with 600 permissions.
3. Edit `compose.yaml` for your environment: the image tag, `MEM0_URL`, and the external network name (the example uses `mem0_default`).
4. Build and start:

   ```bash
   docker build -t waystone:0.5.0 .
   docker compose -f /opt/waystone/compose.yaml up -d
   ```

5. Configure your domain following `deploy/Caddyfile.example`. If you're not behind Cloudflare, you can drop the `@cloudflare` branch and keep only the `header_up X-Forwarded-For {remote_host}` section.
6. Confirm `curl https://memory.example.com/ready` returns `{"status":"ready"}`, then create the first project with `waystone login --server https://memory.example.com` using the Mem0 admin account.
7. (Recommended) Run an isolated smoke test against the real Mem0 with the new image; it uses a temporary database and afterwards only cleans up the vectors created during this test:

   ```bash
   docker run --rm --network mem0_default -e MEM0_URL=http://mem0:8000 -e MEM0_KEY_FILE=/run/secrets/mem0_key \
     -v /opt/waystone/secrets/mem0_key:/run/secrets/mem0_key:ro -v "$PWD/deploy/smoke.py:/app/smoke.py:ro" \
     waystone:0.5.0 python /app/smoke.py
   ```

## Server configuration

| Variable | Default | Description |
|---|---|---|
| `WAYSTONE_DB` | `/data/waystone.sqlite` | SQLite database path |
| `MEM0_URL` | `http://mem0:8000` | Mem0 server address |
| `MEM0_KEY_FILE` | `/run/secrets/mem0_key` | Mem0 service API key file |
| `FORWARDED_ALLOW_IPS` | uvicorn's default trusts only `127.0.0.1` | set to `*` in compose so rate limiting honors the source IP written by the reverse proxy |

## Login rate limiting and source IP

Login, join, and device authorization endpoints are rate limited to 20 requests per source IP per minute; device polling is limited to 240 per minute.

- The peer address seen inside the container is the Docker gateway, not the real user, so the reverse proxy must write the source IP and uvicorn must honor it (`FORWARDED_ALLOW_IPS='*'`). The port is mapped only to `127.0.0.1`, so external requests always pass through the reverse proxy.
- Example Caddy config: when the peer is in a Cloudflare range and carries `CF-Connecting-IP`, take that header; otherwise take the TCP peer address, and overwrite any client-supplied `X-Forwarded-For` — so forged forwarding headers can't bypass rate limiting.
- When Cloudflare changes its ranges, update the Caddyfile and `tests/test_proxy.py` in sync. Don't enable the "Remove visitor IP headers" managed transform on the Cloudflare side, or all requests will be counted against edge node IPs.
- Known limitations: other containers on the same Docker network can reach the service directly with their own forwarding headers; IPv6 clients can rotate addresses within the same subnet.

## Backups, monitoring, and recovery drills

Copy the systemd units under `deploy/` (`waystone-*.service` / `.timer`) to `/etc/systemd/system/`, then enable them with `systemctl enable --now <name>.timer`.

| Script | Schedule | What it does |
|---|---|---|
| `backup.py` | daily at 00:15/06:15/12:15/18:15 | takes a consistent SQLite snapshot with integrity checks; exports Mem0's `postgres` and `mem0_app` databases with verification; copies the Mem0 history database and both compose files; packages them as `backups/snapshot-*.tar.gz`, writing SHA-256 checksums and `latest.json`; 28-day retention. Excludes `.env` and plaintext service keys; refuses to run when free disk space is under 2 GB |
| `monitor.py` | every 5 minutes | checks search readiness, a backup within the last 8 hours, a recovery verification within the last 35 days, and free disk space; writes results to `/opt/waystone/ops/status.json` |
| `restore-check.py` | monthly on the 1st at 03:30 | restores the latest snapshot into a temporary SQLite database and a network-isolated temporary PostgreSQL container for verification; never touches production data |

`backup.py` hardcodes the Mem0 container names `mem0-postgres-1`, `mem0-mem0-1` and the Mem0 compose path `/opt/mem0/server/compose.deploy.yaml`; adjust them to match your Mem0 deployment.

## Offsite backups and independent monitoring (optional)

Monitoring running on the host can't report its own outage or network failure, so run an independent node on a separate server:

- `offsite-monitor.py` (`waystone-offsite.timer`, every 5 minutes): checks the public `/ready`, pulls the latest snapshot over SSH, atomically updates after verifying size and SHA-256, with 28-day retention; keeps the existing copy on failure.
- On the primary server, add `restrict,command="/usr/bin/python3 /opt/waystone/backup-export.py"` and a `from=` source restriction to this machine's dedicated public key. `backup-export.py` only permits reading status, latest backup metadata, and a specified snapshot, and refuses all other commands and symlinks.
- Use a dedicated system user `waystone-offsite`; for configuration see `deploy/offsite-config.example.json`, placed at `/etc/waystone-offsite/config.json`; pin the primary server's host public key at runtime.
- Email alerts: as administrator, run `setup-offsite-email.py --host <SMTP server> --sender <sender email> --recipient <recipient email>`, verify the authorization code and send a test email, then enable `waystone-notify.timer`. Notify only on state changes; queue and retry on send failure; emails contain no memory content or backup attachments. SMTP acceptance does not mean the message reached the inbox — confirm delivery in practice.

If the independent node itself fails, offsite sync and alerts still stop; consider covering it with your cloud provider's built-in host monitoring as well.

## Index repair

- `waystone reindex` retries records whose indexing failed; owners use `waystone reindex --full` to check and backfill all active records. Each batch returns `next_cursor`; keep passing `--cursor` until it's empty; a non-zero `pending` means failures remain.
- Archived projects can be reindexed too: archiving only freezes additions and rewrites; it doesn't block index repair.
- Recall first filters candidate records by project permissions and validity scope with SQL, then runs vector search in batches by candidate ID, so it can't be crowded out by vectors from other environments or expired vectors.

## Conflicts, handoffs, and retraction

- **Conflicts**: B and C both propose against A; after B takes effect, the owner reviews C against B, resubmits C with `rebase --expected <B>`, then decides whether it takes effect with `resolve --expected <B>`. Rebase alone doesn't make content take effect; if the version changes again, a 409 is returned and the review must be redone.
- **Rejection**: `reject` is a terminal state that keeps the proposal and its audit trail; it can't be rebased afterwards. To reconsider, save the content again, which creates a new proposal.
- **Expiry**: expired active records and proposals automatically become `expired`, with `system` recorded in the audit log. Saving identical content again after a handoff expires creates a new record and a new validity period.
- **Retraction**: `retract` changes the record to `retracted`, replaces the body with "[retracted]" and recomputes the content hash, then deletes the record's vectors from Mem0. Before deleting, write the found vector IDs into the `purge_vectors` audit entry; after deleting, look them up once more, and only mark `purged` if they're gone. If vector ownership (namespace, project ID) doesn't match the check, don't delete — keep `purge_pending`, and running `retract` again retries.
- **After retracting the current version**: if what was retracted was the current version that a proposal supersedes, that topic temporarily has no active version; the owner reviews it and runs `resolve` without `--expected` (leaving `expected_id` empty in MCP) to let the proposal take effect. Publishing content on the same topic after a retraction also becomes a proposal requiring owner confirmation.
- **Retraction residue**: existing backups only rotate out after the 28-day retention period; Mem0's own history database may retain the original text, which must be verified and cleaned up on the server using the vector IDs in the `purge_vectors` audit entry.

## Remote connector (OAuth)

Lets Claude web, Desktop, Cowork, and mobile, as well as Claude Code and Codex, use Waystone directly through `<server-address>/mcp` with no client install.

- **Enable**: set `PUBLIC_URL` in `deploy/compose.yaml` (e.g. `https://memory.example.com`; origin only, no path or trailing slash). It must be a verbatim prefix of the connection address the user enters — Claude validates the `resource` in the protected resource metadata. If left empty, `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, and `/oauth/consent` are not mounted.
- **Protocol**: dynamic client registration (always as public clients; no client secret is issued) + PKCE S256; access tokens last 1 hour; refresh tokens last 30 days and rotate on each refresh; replaying a used refresh token invalidates the entire token family. Authorization requests last 10 minutes, authorization codes 5 minutes, both single-use. All tokens are stored as SHA-256 hashes only (tables `oauth_clients`, `oauth_requests`, `oauth_codes`, `oauth_tokens`).
- **Callback allowlist**: by default only `https://claude.ai/api/mcp/auth_callback` and the machine's loopback addresses are allowed (ports ignored; `127.0.0.1`, `localhost`, and `::1` count as the same host; paths must match); other clients are added via `OAUTH_EXTRA_REDIRECT_URIS` (comma-separated, exact match).
- **Rate limiting and caps**: `/register` 20/min per IP, `/token` 120/min, `/authorize` 30/min; pending authorization requests: 20 per client, 1000 globally; cap on registered clients that have not yet issued tokens `OAUTH_MAX_PENDING_CLIENTS` (default 500); client registration metadata max 8 KB.
- **Network**: Claude reaches `/mcp`, `/.well-known/*`, `/register`, and `/token` from Anthropic's egress range (`160.79.104.0/21`); the WAF or CDN must not put a bot challenge on these paths.
- **Disconnect**: users revoke access themselves with `waystone connections` / `waystone disconnect <client_id>`; in an emergency, run `DELETE FROM oauth_codes WHERE user_id=?; DELETE FROM oauth_tokens WHERE user_id=?;` in a single transaction.
- **Residual risk and audit**: the app name shown on the consent page is self-reported by the client and can't be verified; if a user is tricked into approving an authorization link sent by someone else, the grant may land in the other person's account. Approvals, denials, refresh token replays, revocations, and disconnects are all written to the `audit` table (`action` starting with `oauth_`); `oauth_refresh_reuse` may indicate a leaked token.

## Health checks and performance

- The container health check only looks at process liveness (`/health`); dependency availability is reported by `/ready`, whose successful result is cached for 30 seconds, and concurrent requests share a single probe. Dependency failures are discovered by the monitoring scripts reading `/ready`, so make sure the notification chain works.
- **Mem0's API key check is slow**: Mem0 verifies ordinary API keys with bcrypt, costing about 260 ms per call — far more than the search itself (~25 ms). Configure `ADMIN_API_KEY` on Mem0 (constant-time comparison) and point Waystone's key file at it. The key is read the first time the service calls Mem0, so restart the service after rotating it.
- The server reuses its connection to Mem0; one recall issues a single vector search (candidates with and without a declared scope share a 2×limit slot quota; either group can be crowded out — pass the full environment and branch when you need to look at just one scope).
- The client reuses connections in-process, kept idle for 120 seconds, so a long-running MCP service skips the TLS handshake on each consecutive call; connection timeout is 5 seconds, response wait is 30 seconds (120 seconds for rebuild and retract).
- Sessions auto-renew with use within 30 days (at most once per day), with no absolute cap. `logout` only revokes the current session; if you suspect a leak, run `DELETE FROM sessions WHERE user_id=?`.
- Retraction and indexing lock per record; lock objects grow with the record count and are never reclaimed.
- Container logs rotate at 10 MB × 3 files; httpx request logging dropped to warning level.

## Upgrades

1. Take a consistent snapshot with the SQLite backup API, and back up the current compose file and reverse proxy configuration.
2. Build the new image, validate it with the smoke command above first, then switch the service and confirm the `/health` version and `/ready`.
3. Schema changes are applied automatically at startup by adding columns, without rewriting existing content. To roll back, restore the old image; if the new version changed the schema, also restore the pre-upgrade database, and export any writes made after the upgrade first.

## Disaster recovery

1. On the target system, verify the snapshot's SHA-256 and internal manifest, and run an isolated restore verification with `restore-check.py` first; don't overwrite a production database that still has new writes with an older snapshot.
2. Stop Waystone writes and preserve the failure scene; restore `waystone.sqlite` (plus `history.sqlite` if needed), and restore the `postgres` and `mem0_app` databases from the dump.
3. Rebuild the service from the saved compose file and matching image. Credentials such as database passwords, JWT keys, model API keys, and the Mem0 service key are not recovered from chat or logs — the administrator re-configures or regenerates them on the target system and revokes the old credentials that are no longer used.
4. After the service starts, check `/ready`, and have the owner run `waystone reindex --full` for each project until `pending` is 0.
5. Verify queries, conflict resolution, and permission revocation with two members, then restore writes; record the actual recovery time and the snapshot timestamp used.
