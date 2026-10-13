# Waystone 中文快速上手

当前版本：[v0.6.2](https://github.com/hb407033/waystone/releases/tag/v0.6.2)。团队成员不需要登录服务器；向管理员取得**服务地址和项目邀请链接**即可。

完整的项目创建、邀请、日常保存、冲突处理、交接换机及常见问题见[完整使用手册](user-guide.zh-CN.md)。

## 最省事：直接连接远程 MCP

适用于 Claude 网页、Desktop、Cowork、手机，以及 Claude Code、Codex。无需安装 Waystone、Python 或 uv。管理员需已启用远程连接器。

1. 新成员先在浏览器接受项目邀请，完成账号注册。
2. 在客户端添加 `<服务地址>/mcp`，并在浏览器登录授权。服务地址若带端口，必须保留端口。
3. 对 Agent 说：“列出我加入的 Waystone 项目，我来选择；然后查询这个项目的已确认决定和当前进度。”

Claude 网页或桌面端在连接器设置中添加 URL。Claude Code 执行：

```bash
claude mcp add --transport http --scope user waystone https://memory.example.com/mcp
```

然后在 Claude Code 内输入 `/mcp` 完成授权。Codex 执行：

```bash
codex mcp add waystone --url https://memory.example.com/mcp
codex mcp login waystone
```

把 `https://memory.example.com` 换成管理员给的实际地址。已有相同连接时直接复用。

## 交给 Agent 配置

把下面这段话发给有本地执行能力的 Agent，填写自己的服务地址、客户端和项目目录：

```text
请读取 https://raw.githubusercontent.com/hb407033/waystone/main/docs/install.md 并为当前 Agent 配置 Waystone。
服务地址：https://memory.example.com
客户端：Codex
项目目录：/absolute/path/to/repo
优先远程 MCP；需要本地功能时才安装 CLI。请展示浏览器授权链接，等我登录后列出项目让我选择。安装期间不要新建项目或上传记忆。
```

你只需完成浏览器授权和项目选择，Agent 负责其他配置步骤。Pi、DSH 或需要创建项目、本地文件预览时，Agent 会按照[安装指南的本地流程](install.md#local-setup)安装 0.6.2。

## 平时怎么用

- **开始任务**：“先查这个项目的相关记忆，再继续开发。”
- **保存结论**：“把刚确认的方案整理成候选记忆，给我确认后再保存。”
- **换人或换设备**：“整理当前进度、已验证结果和下一步，准备项目交接。”

已连接远程 MCP 时直接说这些话即可。本地安装 Skill 后，也可用 Claude Code 的 `/waystone`、Codex 的 `$waystone`、Pi 的 `/skill:waystone` 快捷入口。

加入已有项目时选择原项目，不要再创建同名项目。远程连接器暂不提供新建项目、邀请成员和本地文件预览；这些操作由项目所有者或通过本地 CLI 完成。保存记忆前始终确认具体内容，原生 Agent 记忆保持独立。

管理员部署服务见[运维指南](operations.md)；普通成员可跳过。
