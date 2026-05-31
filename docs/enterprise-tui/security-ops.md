# 企业 TUI 安全与运维基线

## 安全基线

| 领域 | Direct Evidence | 当前结论 | 企业缺口 |
|---|---|---|---|
| Controller 地址 | `APIBackend.controller_url()` 默认 `external-controller` 为 `127.0.0.1:9090` | 默认本机 loopback | 未强制拒绝 `0.0.0.0` 或非 loopback 生产配置 |
| Controller secret | `APIBackend.api_secret()` 按 `secret-systemd-credential`、`secret-file`、`secret-keyring-service`、`secret` 顺序读取，请求时写 `Authorization: Bearer ...` | 支持 systemd credential/file/keyring/YAML 回退 | YAML secret 仅作为兼容回退 |
| Controller TLS | `APIBackend.controller_url()` 优先使用 `external-controller-tls` 并默认 `https://` | TLS controller 已有入口 | 仍需真实 TLS controller 集成测试 |
| Unix socket | `APIBackend.controller_url()` 遇到 `external-controller-unix` 直接拒绝 | 避免使用不会校验 secret 的控制面 | 如果未来支持，必须单独 ADR |
| TUI 权限 | `default_paths()` 使用 XDG 用户目录；`systemd-user/cproxy.service` 使用 `%h/.local/bin/cproxy` | TUI 可以用户级运行 | 仍需文档禁止用 root 长期运行 TUI |
| 进程所有权 | `ProcessBackend` 校验 `cproxy-process.json`、PID、program、runtime 后才 stop | stale pidfile 不应误杀外部进程 | 需要继续保留 `test_stop_does_not_kill_unowned_process_from_stale_pidfile` |
| 日志 | `LogsScreen` tail `cproxy.log`；安装脚本可写 logrotate 配置 | 本地日志可查看和轮转 | 生产仍需配置日志保留周期 |
| 操作审计 | `write_audit_event()` 写 `cproxy-audit.jsonl`，`audit-journald: true` 时写 `systemd-cat` | selector 切换、连接断开、Provider 更新和配置重启有审计记录 | 目标环境需用 `journalctl` 回放确认 |
| 支持包 | `support-bundle` 生成 tar.gz，脱敏 secret/token/password/credential/authorization 字段和 URL query | 可导出排障材料而不直接泄漏 token | 生产仍需定义支持包保存和传输策略 |
| 连接操作 | `ConnectionsScreen.action_close_all()` 要求连续触发两次才调用 `close_all_connections()` | 批量断开已有本地二次确认 | 单条连接关闭和其他危险动作仍未统一确认 |
| 订阅 URL | `redact_subscription_url()` 隐藏 query token | TUI 输出避免直接泄漏订阅 token | 需要对日志、异常、支持包导出统一脱敏 |

## 运维入口

| 场景 | 命令 | 说明 |
|---|---|---|
| 初始化 | `PYTHONPATH="src" python3 -m cproxy.cli init` | 创建用户级配置目录和默认配置 |
| 渲染运行配置 | `PYTHONPATH="src" python3 -m cproxy.cli render` | 从用户配置生成 runtime YAML |
| 查看状态 | `PYTHONPATH="src" python3 -m cproxy.cli status` | 输出进程、端口、Controller 和 runtime 状态 |
| 用户级 systemd 校验 | `bash "tests/systemd_user_examples_test.sh"` | 验证 service/override/env 示例没有漂移 |
| root-level wrapper dry-run | `bash "scripts/install-system-commands.sh" --dry-run --bindir "/tmp/cproxy-enterprise-tui-audit/bin"` | 只打印安装动作，不写 `/usr/local` |
| 文档/审计 gate | `bash "scripts/audit-enterprise-tui.sh"` | 静态校验当前企业 TUI 文档和项目事实是否一致 |

## 操作风险与处理

| 风险 | 直接证据 | 处理方式 |
|---|---|---|
| API 不可用 | `QueryService.load_context(require_api=False)` 捕获 `APIUnavailableError` 后回退 runtime | TUI 应展示 runtime-only，不允许执行切换 |
| secret 错误 | `APIBackend.request()` 失败会抛 `APIUnavailableError("... external-controller、secret 或服务状态")` | 排障顺序是 controller 地址、secret、服务状态 |
| stale pidfile | `ProcessBackend.stop()` 在 `_is_owned_process()` 失败时抛 `ProcessOwnershipError` | 不要手动 kill 未确认进程，先检查 pidfile 和 `cproxy-process.json` |
| 日志膨胀 | `scripts/install.sh` 可生成 logrotate 配置，模板使用 `copytruncate` | 安装后用 logrotate dry-run 验证配置 |
| 订阅 token 泄漏 | 订阅 TUI 输出会脱敏 URL query | 禁止把完整订阅 URL 写入截图、issue、支持包 |

## 后续安全工作顺序

1. **Controller lint:** 启动前检查 `external-controller` 是否为 loopback，生产配置是否启用 secret，显式拒绝或告警 `external-controller-unix`。
2. **Secret provider:** 新配置优先使用 systemd credential、secret-file 或 keyring；YAML `secret` 只作为旧配置兼容回退。
3. **AuditSink:** JSONL 是默认本地审计，`audit-journald: true` 是 systemd 环境增强；目标环境发布前必须回放 journal。
4. **Supply chain:** 本地生成 `SHA256SUMS` 和 `provenance.json`；真实发布时再挂接 signing key 和包仓库。
5. **Usability gate:** 80/120 列 smoke 与 5000 connections pressure 是 PR gate；长跑 soak 在目标机器执行。

## 不能从当前证据推出的结论

| 结论 | 状态 | 原因 |
|---|---|---|
| “已完成真实 TLS controller 集成” | Unknown | 当前只有 URL 选择和单元测试，未连接真实 TLS Mihomo |
| “所有 secret 都已迁出明文 YAML” | Unknown | 当前仍保留 `secret` 回退以兼容旧配置 |
| “审计日志已满足合规追踪” | Medium | 当前有 JSONL 和 journald hook，缺少目标环境 journal 回放和保留策略 |
| “高频日志/traffic 流不会卡 UI” | Medium | 当前有 5000 connections 映射 smoke，缺少 12h soak |
| “可直接面向多用户/SSO 生产部署” | Unknown | 当前文档和代码证据是单机单用户 |
