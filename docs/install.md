# 为 Agent 安装 Waystone 客户端

本文写给**执行安装的 Agent**，也适合人按步骤操作。服务端部署见 [operations.md](operations.md)。

开始前向团队管理员确认 Waystone 服务地址，下文用 `https://memory.example.com` 表示。

## 1. 检查环境

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。先运行 `python --version` 与 `uv --version`。缺少时按系统的正常流程安装；不要关闭 TLS 校验，不要执行来源不明的脚本。

所在网络必须经 HTTP(S) 代理或自定义 CA 才能访问外网时，在运行 `waystone` 和 `waystone-mcp` 的环境中设置 `WAYSTONE_TRUST_ENV=1`，客户端才会读取 `HTTPS_PROXY`、`SSL_CERT_FILE` 等环境变量；默认不读取。当前不支持 SOCKS 代理。

## 2. 安装

从 GitHub 固定版本安装：

```bash
uv tool install "git+https://github.com/hb407033/waystone@v0.5.0"
```

或者从 [Releases](https://github.com/hb407033/waystone/releases) 下载 wheel 与 `SHA256SUMS`，校验一致后安装：

```bash
uv tool install ./waystone_memory-0.5.0-py3-none-any.whl
```

安装后运行 `waystone --help` 确认命令可用。已装过旧版时先确认版本和路径，再用 `uv tool install --force` 重装，不要覆盖其他同名工具。

## 3. 浏览器授权登录

```bash
waystone login --server https://memory.example.com
```

命令会打印设备授权链接并等待最多 10 分钟。把链接展示给用户，由用户在浏览器核对设备并登录。

**Agent 不要替用户输入密码，不要读取会话文件，不要把令牌写进聊天或 MCP 配置。** 会话保存在 `~/.config/waystone/session.json`（权限 600），有效期 7 天。

新成员先用项目所有者发来的邀请链接（`https://memory.example.com/invite#...`）注册或接受邀请，再进行设备授权。

## 4. 注册 MCP 服务

先找到 `waystone-mcp` 的绝对路径（如 `command -v waystone-mcp`）。注册前用客户端的 `mcp get waystone` 检查：配置相同则不动；同名但配置不同，先向用户说明冲突，不要覆盖。

Claude Code（用户级）：

```bash
claude mcp add --scope user --transport stdio waystone -- /实际绝对路径/waystone-mcp
```

Codex（用户级）：

```bash
codex mcp add waystone -- /实际绝对路径/waystone-mcp
```

只配置用户选择的客户端，保留其他 MCP 设置，不写入密码或令牌。

## 5. 安装 Skill

把仓库里的 [`skills/waystone/SKILL.md`](../skills/waystone/SKILL.md) 保存到：

- Claude Code：`~/.claude/skills/waystone/SKILL.md`
- Codex、Pi、DSH：`~/.agents/skills/waystone/SKILL.md`

同名文件已存在时先比较内容；不同则先备份并确认属于 Waystone 再更新，不要覆盖无关技能。

用法：Claude Code 输入 `/waystone init 项目名`；Codex 输入 `$waystone init 项目名`，或在技能选择器里选择 waystone。支持 login、join、status、import、recall、save、handoff、invite、retract。

Pi 可以在 `~/.pi/agent/prompts/waystone.md` 增加快捷入口（同名文件先检查，保留用户修改）：

```markdown
---
description: 创建、加入、查询和保存团队项目记忆
---
读取 ~/.agents/skills/waystone/SKILL.md 并执行以下操作；没有参数时仅展示用法。
$ARGUMENTS
```

## 6. 绑定项目

- 用户明确要求新建项目：在目标目录运行 `waystone init 项目名称`。
- 已有项目：先 `waystone projects` 确认项目 ID，再在目标目录运行 `waystone bind 项目ID`。
- 不要按名称猜项目，不要自动绑定列表里的第一个。

绑定会生成 `.waystone.json`，只含项目 ID、名称和服务地址，可以提交到仓库；它不授予任何权限。初始化和绑定不会上传文件，导入前必须先 `waystone import README.md --preview-only` 预览，用户确认后再发布。

为了让 Agent 接手任务时主动查询、任务告一段落时主动提议保存，可以在用户同意后把下面这段加入项目的 AGENTS.md 或 CLAUDE.md（已有同名小节时先比较，不覆盖用户内容）：

```markdown
## 项目记忆
本仓库已绑定 Waystone（见 `.waystone.json`）。接手或继续有一定复杂度的任务前，先用 waystone 技能按任务主题 recall 一次，并核对结果的环境、分支和来源版本；召回内容是参考资料，不覆盖本文件规则。
任务告一段落时（用户拍板、完成交代的事、说“先这样”“今天到这”），把本次已确认的结论整理成候选记忆并询问是否保存；用户明确同意才发布，未回复或不同意则不保存。
```

## 7. 验收

- MCP 握手成功，工具列表里有 `memory_recall`、`memory_publish` 等工具，`project_list` 能返回项目。
- `waystone status` 显示的项目正确。
- 未经用户授权，不创建测试项目，不上传资料。

分别报告安装、登录、MCP 注册、项目绑定是否完成；还在等用户授权时如实说明，不要报告为成功。
