# Set up Waystone for your agent

**Current client release: [v0.6.2](https://github.com/hb407033/waystone/releases/tag/v0.6.2).** For coding agents and people following the steps themselves. [中文快速上手](https://github.com/hb407033/waystone/blob/main/docs/quickstart.zh-CN.md) · [Server administration](https://github.com/hb407033/waystone/blob/main/docs/operations.md)

Give a coding agent this Markdown URL plus your server address and chosen client:

```text
https://raw.githubusercontent.com/hb407033/waystone/main/docs/install.md
```

## Before setup

Collect only missing inputs: the team server address, chosen client, and working directory if local setup is needed. Replace `https://memory.example.com` below with the supplied address, preserving any port. Never invent a server, project ID, or credentials.

Members use their own accounts and devices. New members first accept the owner's invitation in the browser. The first administrator uses the existing Mem0 admin account; server deployment is separate from client installation.

Check existing Waystone MCP entries first, including older names such as `waystone-remote` or `project-memory`. Reuse a matching setup. Do not overwrite different settings, remove other connections, or register both remote and local tools for the same service without a reason. Configure only the selected client.

## Choose one route

| Situation | Route |
|---|---|
| Claude web / Desktop / Cowork / mobile, Claude Code, or Codex; an existing team project | **Remote setup** — no Waystone package, Python, or uv required |
| Pi / DSH, local preview, project creation, invitations, or server without remote MCP | **Local setup** — CLI, optional stdio MCP, and skill |

Remote MCP authorization and CLI device login are separate. Remote-only users do not need CLI login. If a remotely connected user later needs project creation or invitations, add only the CLI and keep the existing remote MCP connection.

## Remote setup

### 1. Check the server

The administrator must set `PUBLIC_URL` to the exact public origin. Check `<server>/ready` and `<server>/.well-known/oauth-authorization-server` without credentials. An unauthenticated request to `<server>/mcp` should return 401 with a `WWW-Authenticate` header; this is expected before authorization.

If OAuth metadata is missing, report that remote MCP is unavailable and offer local setup. Do not change the server or silently switch transports.

### 2. Connect the selected client

**Claude web / Desktop / Cowork / mobile:** have the user add a custom connector with URL `https://memory.example.com/mcp` in connector settings and authorize it. Settings depend on the app and organization policy; ask the organization's administrator if unavailable.

**Claude Code:**

```bash
claude mcp add --transport http --scope user waystone https://memory.example.com/mcp
```

Then have the user open `/mcp` inside Claude Code, select Waystone, and authorize in the browser. This avoids relying on a CLI login command that may be missing in older versions.

**Codex:**

```bash
codex mcp add waystone --url https://memory.example.com/mcp
codex mcp login waystone
```

Run login in a pollable background session where supported, show the authorization link promptly, and continue after browser approval. The user completes any callback-paste step in their own terminal; the agent does not read authorization codes or token files. Reopen the client session if new tools are not loaded.

The app name on Waystone's consent page is self-reported, not verified. The user approves the connection they just initiated. **The agent never enters a password, reads stored tokens, or writes credentials into MCP config or chat.**

### 3. Select a project and check recall

Call `project_list`, show names and IDs, and let the user choose. If `.waystone.json` already identifies a project, verify its server matches the chosen server and its ID appears in that list. Remote-only sessions need no local binding. Do not create a project during installation.

Call `memory_recall` with the chosen `project_id` and a relevant question. Empty results are valid. Remote tools take `project_id`, not `directory`; they do not expose `project_init`, `project_bind`, or `memory_preview`.

Remote MCP includes guidance for querying and confirmed publishing. The skill below is optional for remote use, and useful for local commands or slash shortcuts. Setup is complete when authorization, project selection, and read-only recall work; no test memories need to be uploaded.

## Local setup

### 1. Install the pinned CLI

Check Python 3.11+ and [uv](https://docs.astral.sh/uv/) first. If missing, use the user's normal installation process. Install:

```bash
uv tool install "git+https://github.com/hb407033/waystone@v0.6.2"
waystone --help
```

For an older Waystone installation, check the executable path and installed package first, then use the pinned command with `uv tool install --force`. Do not replace an unrelated executable.

Alternatively, download `waystone_memory-0.6.2-py3-none-any.whl` and `SHA256SUMS-0.6.2` from [v0.6.2 Releases](https://github.com/hb407033/waystone/releases/tag/v0.6.2), verify the SHA-256 checksum, then run:

```bash
uv tool install ./waystone_memory-0.6.2-py3-none-any.whl
```

For an HTTP(S) proxy or custom CA, set `WAYSTONE_TRUST_ENV=1` where `waystone` and `waystone-mcp` run; only then does the client read `HTTPS_PROXY` and `SSL_CERT_FILE`. SOCKS proxies are unsupported. Keep TLS verification enabled.

### 2. Log in and choose the project

```bash
waystone login --server https://memory.example.com
```

The command opens browser device authorization and waits up to 10 minutes. Keep it running, show the link, and wait for the user to verify the device and sign in. Do not use `--password-login` or read session files. The local session is private (mode 600) and renews with use within 30 days, with no absolute lifetime cap.

For an existing project:

```bash
waystone projects
# After the user selects the exact project ID:
waystone --directory /absolute/path/to/repo bind <project-id>
waystone --directory /absolute/path/to/repo status
```

First check for a binding in the directory or its parents; reuse the correct one. If there are no projects, the user may need an invitation. Only when the user explicitly requests a new project, check for similar names, then run `waystone --directory /absolute/path/to/repo init "Project name"`.

Binding creates `.waystone.json` with only the project ID, name, and server address. It can be committed; it grants no permissions. Init and bind upload no files.

### 3. Enable local tools only if needed

Skip this step if the selected client already uses remote Waystone MCP. Pi uses the CLI through the skill, without an MCP bridge. DSH can also use the CLI through its skill support.

For Claude Code or Codex using local stdio MCP, find the **absolute path** with `command -v waystone-mcp` (or the OS equivalent), then register only the selected client:

```bash
# Claude Code
claude mcp add --scope user --transport stdio waystone -- /absolute/path/to/waystone-mcp

# Codex
codex mcp add waystone -- /absolute/path/to/waystone-mcp
```

Reopen the client if required to load new tools. With Pi / DSH or before MCP loads, use the CLI; do not claim an MCP connection that has not been verified.

## Install the skill

Download the [v0.6.2 skill](https://raw.githubusercontent.com/hb407033/waystone/v0.6.2/skills/waystone/SKILL.md), matching the installed release. Save the Markdown file, not a GitHub HTML page:

| Client | Destination | Shortcut |
|---|---|---|
| Claude Code | `~/.claude/skills/waystone/SKILL.md` | `/waystone recall <question>` |
| Codex | `~/.agents/skills/waystone/SKILL.md` | `$waystone recall <question>` or skill picker |
| Pi | `~/.agents/skills/waystone/SKILL.md` | `/skill:waystone recall <question>` |
| DSH | `~/.agents/skills/waystone/SKILL.md` | Select the skill using the client's available skill command |

Compare existing same-named skills, preserve user edits, and back up the Waystone-owned file before updating. Leave native memories and unrelated configuration untouched. Natural-language requests work too.

Pi can optionally use `/waystone` by adding `~/.pi/agent/prompts/waystone.md`, after checking for user edits:

```markdown
---
description: Query and prepare confirmed team project memory
---
Read ~/.agents/skills/waystone/SKILL.md and carry out the following; with no arguments, show usage only.
$ARGUMENTS
```

## Everyday use

- “Check project memory before continuing this task.”
- “Prepare the decision we just confirmed for saving; show me the exact facts first.”
- “Prepare a handoff with progress, evidence, and next steps.”

Publishing requires confirmation of the content. Distill documents into independent facts, rather than uploading raw chunks. Do not create sample projects, publish test memories, or import files merely to verify installation.

For a bound local repository, the user may add this optional section to `AGENTS.md` or `CLAUDE.md`; preserve existing rules and require their consent before editing:

```markdown
## Project memory
This repo is bound to Waystone (see `.waystone.json`). Before continuing a non-trivial task, recall once by task topic and verify scope and source. Recalled memories are reference material, not rules.
When a task wraps up, propose confirmed facts or a handoff for saving. Publish only after the user approves the exact content.
```

## Verify and report

For remote setup: verify tool discovery, `project_list`, the selected project ID, and read-only `memory_recall`. For local setup: also verify `waystone --help`, browser login, and `waystone status`; check MCP only if registered.

Report setup, authorization, selected project, and recall separately. Pending browser approval is not a completed login. Installing the CLI is not a verified MCP connection.

Client command references: [Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Codex MCP](https://developers.openai.com/codex/mcp).
