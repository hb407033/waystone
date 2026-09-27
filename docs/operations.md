# 服务端部署与运维

适用于小团队单实例自托管：一台服务器、允许维护窗口，不承诺高可用。客户端安装见 [install.md](install.md)。

## 部署拓扑

- **Waystone 服务**：Docker 容器，端口 8900 只映射到宿主机 `127.0.0.1`，加入 Mem0 所在的 Docker 网络，通过 `MEM0_URL` 访问 Mem0。
- **反向代理**：负责 HTTPS 证书，并把真实来源 IP 写入 `X-Forwarded-For`，示例配置为 [`deploy/Caddyfile.example`](../deploy/Caddyfile.example)。
- **数据**：SQLite 数据库位于挂载卷 `data/`，是记录的唯一权威来源；Mem0 只做可重建的向量索引。
- **凭据**：Mem0 服务 API Key 以只读文件挂载进容器，成员和 Agent 永远拿不到。

## 首次部署

1. 部署 [Mem0 官方自托管服务](https://docs.mem0.ai/open-source/setup)，创建管理员账号，并为 Waystone 生成一个服务用 API Key。
2. 在服务器上准备 `/opt/waystone`（备份与监测脚本默认使用这个路径），放入 `deploy/compose.yaml`，创建 `data/` 目录和权限为 600 的 `secrets/mem0_key`。
3. 按实际情况修改 `compose.yaml`：镜像标签、`MEM0_URL`、外部网络名（示例是 `mem0_default`）。
4. 构建并启动：

   ```bash
   docker build -t waystone:0.5.0 .
   docker compose -f /opt/waystone/compose.yaml up -d
   ```

5. 参考 `deploy/Caddyfile.example` 配置域名。不经过 Cloudflare 时，可以删掉 `@cloudflare` 分支，只保留 `header_up X-Forwarded-For {remote_host}` 那一段。
6. 确认 `curl https://memory.example.com/ready` 返回 `{"status":"ready"}`，再用 Mem0 管理员账号执行 `waystone login --server https://memory.example.com` 创建第一个项目。
7. （推荐）用新镜像连真实 Mem0 做一次隔离冒烟，它使用临时数据库，结束后只清理本次测试产生的向量：

   ```bash
   docker run --rm --network mem0_default -e MEM0_URL=http://mem0:8000 -e MEM0_KEY_FILE=/run/secrets/mem0_key \
     -v /opt/waystone/secrets/mem0_key:/run/secrets/mem0_key:ro -v "$PWD/deploy/smoke.py:/app/smoke.py:ro" \
     waystone:0.5.0 python /app/smoke.py
   ```

## 服务端配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WAYSTONE_DB` | `/data/waystone.sqlite` | SQLite 数据库路径 |
| `MEM0_URL` | `http://mem0:8000` | Mem0 服务地址 |
| `MEM0_KEY_FILE` | `/run/secrets/mem0_key` | Mem0 服务 API Key 文件 |
| `FORWARDED_ALLOW_IPS` | uvicorn 默认只信任 `127.0.0.1` | compose 中设为 `*`，让限流采信反向代理写入的来源 IP |

## 登录限流与来源 IP

登录、加入和设备授权接口按来源 IP 每分钟 20 次限流，设备轮询每分钟 240 次。

- 容器内看到的连接对端是 Docker 网关，不是真实用户，所以必须由反向代理写入来源 IP，并让 uvicorn 采信（`FORWARDED_ALLOW_IPS='*'`）。端口只映射到 `127.0.0.1`，外部请求一定经过反向代理。
- 示例 Caddy 配置：对端属于 Cloudflare 网段且带 `CF-Connecting-IP` 时取该头，其余情况取 TCP 对端地址，并覆盖客户端自带的 `X-Forwarded-For`，所以伪造转发头绕不过限流。
- Cloudflare 调整网段时，同步更新 Caddyfile 和 `tests/test_proxy.py`。Cloudflare 侧不要开启「Remove visitor IP headers」托管转换，否则所有请求会按边缘节点 IP 计数。
- 已知边界：同一 Docker 网络里的其他容器可以直连服务并自带转发头；IPv6 客户端可以在同一网段内更换地址。

## 备份、监测与恢复演练

`deploy/` 下的 systemd 单元（`waystone-*.service` / `.timer`）复制到 `/etc/systemd/system/` 后，用 `systemctl enable --now <名称>.timer` 启用。

| 脚本 | 定时 | 做什么 |
|---|---|---|
| `backup.py` | 每天 00/06/12/18:15 | 生成一致的 SQLite 快照并做完整性检查；导出 Mem0 的 `postgres` 与 `mem0_app` 两个库并校验；复制 Mem0 历史库和两份 compose 文件；打包为 `backups/snapshot-*.tar.gz`，写 SHA-256 与 `latest.json`；保留 28 天。不包含 `.env` 和明文服务密钥；剩余空间不足 2 GB 时拒绝执行 |
| `monitor.py` | 每 5 分钟 | 检查检索就绪、8 小时内有备份、35 天内做过恢复验证、磁盘余量，结果写 `/opt/waystone/ops/status.json` |
| `restore-check.py` | 每月 1 日 03:30 | 把最新快照恢复到临时 SQLite 和无网络的临时 PostgreSQL 容器中校验，不碰线上数据 |

`backup.py` 里写死了 Mem0 容器名 `mem0-postgres-1`、`mem0-mem0-1` 和 Mem0 compose 路径 `/opt/mem0/server/compose.deploy.yaml`，请按你的 Mem0 部署修改。

## 异地备份与独立监测（可选）

主机内的监测无法报告自己宕机或断网，建议在另一台服务器上运行独立节点：

- `offsite-monitor.py`（`waystone-offsite.timer`，每 5 分钟）：检查公网 `/ready`，经 SSH 拉取最新快照，校验大小和 SHA-256 后原子更新，保留 28 天；失败时保留已有副本。
- 主服务器给这台机器的专用公钥加上 `restrict,command="/usr/bin/python3 /opt/waystone/backup-export.py"` 和 `from=` 来源限制。`backup-export.py` 只允许读取状态、最新备份元数据和指定快照，拒绝其他命令和符号链接。
- 使用专用系统用户 `waystone-offsite`；配置参考 `deploy/offsite-config.example.json`，放到 `/etc/waystone-offsite/config.json`；运行时固定主服务器主机公钥。
- 邮件告警：以管理员身份运行 `setup-offsite-email.py --host <SMTP 服务器> --sender <发件邮箱> --recipient <收件邮箱>`，验证授权码并发送测试邮件后启用 `waystone-notify.timer`。只在状态变化时通知；发送失败保留队列重试；邮件不包含记忆内容或备份附件。SMTP 接受邮件不代表已送达收件箱，请实际确认。

独立节点本身故障时仍会中断异地同步与告警，建议再用云平台自带的主机监控覆盖它。

## 索引修复

- `waystone reindex` 重试索引失败的记录；所有者用 `waystone reindex --full` 核对并补齐全部有效记录。每批返回 `next_cursor`，继续传 `--cursor` 直到为空；`pending` 不为 0 说明仍有失败。
- 归档项目同样可以 reindex：归档只冻结新增和改写，不阻止修复索引。
- 召回先用 SQL 按项目权限和有效范围筛出候选记录，再按候选 ID 分批向量检索，不会被其他环境或已失效的向量挤占。

## 冲突、交接与撤回

- **冲突**：B、C 同时针对 A 提案，B 生效后，所有者核对 C 与 B，用 `rebase --expected <B>` 重新提交 C，再 `resolve --expected <B>` 选择是否生效。rebase 本身不让内容生效；版本再次变化返回 409，需要重新核对。
- **拒绝**：`reject` 是终态，保留提案与审计，不能再 rebase；需要重新考虑时重新保存内容，会生成新提案。
- **过期**：到期的有效记录和提案自动变为 expired，审计记为 `system`。交接到期后再次保存相同内容，会生成新的记录和有效期。
- **撤回**：`retract` 把记录改为 retracted，正文替换为「[已撤回]」并重算内容哈希，删除 Mem0 中该记录的向量。删除前先把查到的向量 ID 写入审计 `purge_vectors`，删完再查一次，查不到才标记 purged；向量归属（命名空间、项目 ID）核对不一致时不删除，保持 `purge_pending`，再次执行 retract 会重试。
- **撤回当前版本之后**：如果被撤回的是某条提案所替代的当前版本，该主题暂时没有有效版本，所有者核对后用 `resolve` 且不传 `--expected`（MCP 中 `expected_id` 留空）让提案生效；撤回后再发布同主题内容，也会成为需要所有者确认的提案。
- **撤回的残留**：已生成的备份要到 28 天保留期后才会轮换掉；Mem0 自身的历史库可能保留原文，需要按 `purge_vectors` 审计里的向量 ID 在服务器上核对清理。

## 远程连接器（OAuth）

让 Claude 网页、Desktop、Cowork、手机，以及 Claude Code、Codex 不装客户端、直接通过 `<服务地址>/mcp` 使用 Waystone。

- **启用**：在 `deploy/compose.yaml` 设置 `PUBLIC_URL`（形如 `https://memory.example.com`，只能是 origin，不带路径和末尾斜杠）。它必须与用户填写的连接地址前缀完全一致，Claude 会校验受保护资源元数据里的 `resource`。留空则不挂载 `/mcp`、`/.well-known/*`、`/authorize`、`/token`、`/register`、`/revoke`、`/oauth/consent`。
- **协议**：动态客户端注册（一律按公共客户端，不签发 client secret）+ PKCE S256；访问令牌 1 小时，刷新令牌 30 天、每次刷新轮换，已用过的刷新令牌被重放时作废整个令牌家族。授权请求 10 分钟、授权码 5 分钟，均一次性。所有令牌只存 sha256 哈希（表 `oauth_clients`、`oauth_requests`、`oauth_codes`、`oauth_tokens`）。
- **回调白名单**：默认只允许 `https://claude.ai/api/mcp/auth_callback` 和本机回环地址（忽略端口，`127.0.0.1`、`localhost`、`::1` 视为同一主机，路径必须一致）；其他客户端通过 `OAUTH_EXTRA_REDIRECT_URIS`（逗号分隔、逐字匹配）加入。
- **限流与上限**：`/register` 每 IP 每分钟 20 次、`/token` 120 次、`/authorize` 30 次；待确认授权请求每客户端 20 个、全局 1000 个；未产生令牌的客户端总数上限 `OAUTH_MAX_PENDING_CLIENTS`（默认 500）；客户端注册信息不超过 8 KB。
- **网络**：Claude 从 Anthropic 的出站网段（`160.79.104.0/21`）访问 `/mcp`、`/.well-known/*`、`/register`、`/token`，WAF 或 CDN 不得对这些路径做人机质询。
- **断开**：用户用 `waystone connections` / `waystone disconnect <client_id>` 自助撤销；紧急情况下在同一事务里执行 `DELETE FROM oauth_codes WHERE user_id=?; DELETE FROM oauth_tokens WHERE user_id=?;`。
- **残余风险与审计**：授权页上的应用名称由客户端自报、无法验证；用户若被诱导批准他人发来的授权链接，授权可能落到对方账户。批准、拒绝、刷新令牌重放、撤销、断开都会写入 `audit` 表（`action` 以 `oauth_` 开头）；`oauth_refresh_reuse` 可能意味着令牌泄露。

## 健康检查与性能

- 容器健康检查只看进程是否存活（`/health`）；依赖是否可用看 `/ready`，成功结果缓存 30 秒，并发请求共用同一次探测。依赖故障靠监控脚本读 `/ready` 发现，请确保通知链可用。
- **Mem0 的 API Key 校验很慢**：Mem0 用 bcrypt 校验普通 API Key，每次调用约 260 毫秒，远大于检索本身（约 25 毫秒）。建议在 Mem0 配置 `ADMIN_API_KEY`（常量时间比较），Waystone 的 Key 文件改用它。Key 在服务首次调用 Mem0 时读取，更换后需重启服务。
- 服务端复用到 Mem0 的连接；一次召回只调用一次向量检索（已注明与未注明范围的候选共享 `2×limit` 个名额，任一组都可能被挤掉，需要只看某一范围时传完整的环境和分支）。
- 客户端在进程内复用连接、空闲保留 120 秒，常驻的 MCP 服务连续调用可省掉每次的 TLS 握手；连接超时 5 秒，等待响应 30 秒（重建与撤回 120 秒）。
- 会话 30 天内有使用自动续期（每天最多续一次），不设绝对上限。`logout` 只撤销当前会话；怀疑泄露时执行 `DELETE FROM sessions WHERE user_id=?`。
- 撤回与索引按记录加锁，锁对象随记录数增长、不回收。
- 容器日志按 10 MB × 3 个文件轮转；httpx 请求记录降到警告级别。

## 升级

1. 用 SQLite backup API 保存一致快照，备份当前 compose 文件和反向代理配置。
2. 构建新镜像，先用上面的冒烟命令验证，再切换服务并确认 `/health` 版本和 `/ready`。
3. 表结构变化会在服务启动时以追加字段的方式自动完成，不改写已有内容。回退时恢复旧镜像；如果新版本改过表结构，还要恢复升级前的数据库，并先导出升级后产生的写入。

## 灾难恢复

1. 在目标系统核对快照的 SHA-256 和内部 manifest，先用 `restore-check.py` 做隔离恢复验证；不要把历史快照直接覆盖到仍有新写入的线上库。
2. 停止 Waystone 写入并保留故障现场；恢复 `waystone.sqlite`（必要时连同 `history.sqlite`），从 dump 恢复 `postgres` 与 `mem0_app` 数据库。
3. 按保存的 compose 和对应镜像重建服务。数据库口令、JWT 密钥、模型 API Key、Mem0 服务 Key 等凭据不从聊天或日志恢复，由管理员在目标系统重新配置或生成，并撤销不再使用的旧凭据。
4. 服务启动后检查 `/ready`，所有者对每个项目执行 `waystone reindex --full`，直到 `pending` 为 0。
5. 用两个成员验证查询、冲突确认和撤权，再恢复写入；记录实际恢复耗时和采用的快照时间。
