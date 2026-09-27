# Contributing

Thank you for helping improve Waystone. Please read this page before contributing.

## Development environment

- Python 3.11 or later; we recommend managing dependencies with [uv](https://docs.astral.sh/uv/)
- Optional: Docker — pull `docker pull caddy:2` ahead of time so the reverse-proxy forwarding tests run for real inside a container; otherwise they are skipped automatically

```bash
uv sync --extra test
uv run pytest -q
```

Tests don't need a real Mem0: server tests use an in-memory fake vector backend. `deploy/smoke.py` requires a real Mem0 connection and only runs after deploying to the server.

## Contribution requirements

- **Tests first**: for bug fixes, first add a failing test that reproduces the issue; new features ship with tests covering the main branches.
- **Small, focused commits**: one PR does one thing; explain "what problem it solves and how you verified it".
- **Follow the existing style**: code follows the repo's existing compact style; comments explain "why", not "what"; update adjacent comments when you change logic.
- **Do not commit**: secrets, session files, databases, backups, or any real server addresses or personal information. Examples always use reserved domains like `memory.example.com` and `example.invalid`.

## Changes that need extra explanation

The following changes affect data security or existing deployments — describe the impact and migration path in the PR description:

- Permissions and project isolation (`acl`, member roles, sessions, invitations, device authorization)
- Memory state transitions (proposals, acceptance, rejection, expiry, retraction)
- Database schema or the meaning of existing data
- HTTP API, MCP tool parameters, CLI parameters
- Data sent to the vector store or written to logs

## Reporting issues

- Feature issues and suggestions: open an Issue directly, including the version (the `version` returned by `/health`), reproduction steps, and the expected result.
- Security issues: **do not file a public Issue** — report privately per [SECURITY.md](SECURITY.md).

By submitting code, you agree to license your contribution under [Apache License 2.0](LICENSE).
