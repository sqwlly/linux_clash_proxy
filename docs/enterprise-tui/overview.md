# cproxy 企业 TUI 架构现状与边界

## 决策

**Direct Evidence:** 当前仓库是 Python 项目，不做 Go/Bubble Tea 重写。`pyproject.toml` 声明 `requires-python = ">=3.11"`，可选 TUI 依赖为 `textual>=5.0`，命令入口为 `cproxy-tui = "cproxy.tui.app:run_tui"`。

**Direct Evidence:** `deep-research-report.md` 给出的 Go/Bubble Tea/Lip Gloss 方案是通用企业 TUI 蓝图，不是本仓库迁移要求。本仓库落地方式是把报告中的架构、测试、安全、运维和验收要求翻译到现有 **Python 3.11 + Textual + Mihomo External Controller** 项目。

**Non-goal:** 本轮不新增 Go module、不引入 Bubble Tea、不重写 `src/cproxy/tui`，也不把 Mihomo/Clash 内核静态链接进前端。

## 当前分层

| 层 | 当前拥有者 | Direct Evidence | 说明 |
|---|---|---|---|
| TUI shell | `src/cproxy/tui/app.py` | `CProxyApp` 使用 `TabbedContent` 组合 Overview、Nodes、Providers、Connections、AI Route、Subs、Config、Proxy、Logs | Textual 应用壳和跨页快捷键在这里 |
| 页面组件 | `src/cproxy/tui/screens/*.py` | `DashboardScreen`、`ProxiesScreen`、`SubscriptionsScreen`、`ConfigEditorScreen`、`LogsScreen`、`SystemProxyScreen` | 每个页面只处理本页面交互和状态展示 |
| 服务层 | `src/cproxy/services/*.py` | `QueryService` 优先读取 API，API 不可用时回退 runtime；`DiagnosticsService` 封装连通性和 AI 探测 | TUI 与后端 IO 的窄接口 |
| Controller client | `src/cproxy/backend/api.py` | `APIBackend` 读取 `external-controller`，对有 `secret` 的请求加 `Authorization: Bearer ...`，覆盖 `/proxies`、`/configs`、`/connections`、`/providers/*` | 当前是同步 REST 客户端 |
| Runtime config | `src/cproxy/backend/runtime.py` | `RuntimeBackend.render_runtime()` 读取用户 YAML，注入 AI 路由组和规则后写入 runtime YAML | 配置渲染与业务规则在后端层 |
| Process owner | `src/cproxy/backend/process.py` | `ProcessBackend` 写 `cproxy-process.json`，停止前校验 PID、program 和 runtime | 防止 stale pidfile 误杀非 cproxy 进程 |
| XDG paths | `src/cproxy/config.py` | `default_paths()` 使用 `XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME` | 支持用户级配置、数据和状态隔离 |
| systemd/user | `systemd-user/*.service` | `cproxy.service` 使用 `%h/.local/bin/cproxy render/start/stop/restart` | 当前运维集成以用户级 systemd 为主 |

## 已有能力

| 领域 | Direct Evidence | 当前能力 |
|---|---|---|
| 键盘优先 | `CProxyApp.BINDINGS` 定义 `q`、`ctrl+r`、`[`、`]`、数字页签和 `escape`；`on_key()` 处理 tabbar、按钮、输入框、开关的焦点移动 | 主导航和表单页可用键盘完成 |
| 概览页 | `DashboardScreen` 每 5 秒刷新 runtime、API、端口、Controller、PID、AI Route、traffic 和 connections | 有 TUI 首页健康视图 |
| 策略组 | `ProxiesScreen` 支持组列表、节点列表、切换、延迟测试、restart 和 API 不可用时 runtime 只读视图 | 覆盖核心切换流程 |
| Provider | `ProvidersScreen` 通过 `QueryService.list_proxy_providers()` 展示 `/providers/proxies`，支持选中 provider 后手动更新 | 覆盖报告要求的 Provider 手动更新 |
| 连接追踪 | `ConnectionsScreen` 通过 `QueryService.list_connections()` 展示 `/connections`，支持关闭选中连接，关闭全部连接需二次确认 | 覆盖报告要求的连接查看与断开 |
| 订阅 | `SubscriptionsScreen` 支持 Preview、Apply、Validate；订阅 URL 输出前经 `redact_subscription_url()` 脱敏；Apply 时使用临时 refresh script | 有订阅导入的 dry-run/apply 分离 |
| 配置 | `ConfigEditorScreen` 支持保存、渲染 runtime、渲染后重启、重载编辑器内容 | 可在 TUI 内完成配置编辑、渲染和受控重启 |
| 日志 | `LogsScreen` tail 本地 `cproxy.log`，展示最近 500 行，追加时保留最近 1000 行，支持 pause/follow | 有本地日志观察面 |
| 代理环境 | `SystemProxyScreen` 支持当前会话代理环境变量和 `.bashrc`/`.zshrc` 标记块写入 | 有用户级环境接入能力 |
| 诊断 | `DiagnosticsService` 覆盖 GeoIP、外网连通性、出口 IP、ChatGPT/OpenAI 探测、组延迟测试 | 有基础连通性与 AI 路由验证 |
| 安装与运维 | `scripts/install.sh` 支持 pipx 或 user pip 安装、GeoIP 安装、logrotate 配置、system command 安装；`scripts/install-system-commands.sh` 有 `--dry-run` | 有用户级安装和 root-level wrapper 刷新路径 |
| 测试 | `tests/test_tui_app.py`、`tests/test_tui_proxies.py`、`tests/test_tui_subscriptions.py`、`tests/test_tui_logs.py`、`tests/test_runtime_and_process.py`、`tests/systemd_user_examples_test.sh` | 已有 Textual 交互、运行时、安装和 systemd 示例测试 |

## 企业缺口

| 领域 | 缺口 | 影响 | 当前验收方式 |
|---|---|---|---|
| Controller TLS | `APIBackend.controller_url()` 优先使用 `external-controller-tls` 并默认生成 `https://` URL | 已有 TLS controller 入口 | 仍需真实 TLS controller 集成测试 |
| Secret 来源 | `APIBackend.api_secret()` 支持 `secret-systemd-credential`、`secret-file`、`secret-keyring-service`，最后才回退 `secret` | 可避免把真实 secret 写入 YAML | 兼容回退仍保留，迁移期允许旧配置继续运行 |
| Unix socket 风险 | `APIBackend.controller_url()` 遇到 `external-controller-unix` 会拒绝并提示该控制面不校验 secret | 已显式 fail closed | 后续若必须支持 Unix socket，需要独立 ADR |
| 实时流 | 当前日志来自本地文件 tail，traffic 通过 `/connections` 轮询；未看到 `/logs`、`/traffic`、`/memory` stream 客户端 | 高频流量和日志仍不是报告要求的实时流模型 | 作为增强项，不阻断当前 MVP |
| 审计日志 | `write_audit_event()` 写 `cproxy-audit.jsonl`，`audit-journald: true` 时额外写 `systemd-cat` | 已有本地 JSONL 与可选 journald 审计 | 生产仍需定义保留周期 |
| 危险动作确认 | 关闭全部连接已有二次确认；节点切换、restart、配置保存、shell rc 写入尚未统一二次确认或撤销 | 运维误操作成本较高 | Textual 测试只能覆盖部分确认策略，不能证明所有危险动作已统一 |
| 包与供应链 | `.github/workflows/ci.yml` 覆盖 Python tests、shell tests、企业审计、`pip-audit`、SBOM、Scorecard 和 GA artifact smoke | 已有 CI 入口和本地 provenance/checksum 脚本 | 后续需要真实发布签名和包仓库 |
| 可用性基线 | `tests/test_tui_app.py` 覆盖 80/120 列布局 smoke | 窄屏/标准宽度不会阻断主导航挂载 | 后续可补截图 golden |
| 性能与压力 | `tests/test_ga_readiness.py` 覆盖 5000 条 connections 映射压力 smoke | 高频连接列表基础映射可测 | 后续可补长跑 soak |
| SSO/RBAC/多租户 | 当前是单机单用户工具 | 符合 MVP，但不满足大组织集中治理 | 明确列为后续企业迭代 |

## 下一步边界

1. 先把现有 Python/Textual 路线审计清楚：文档、静态审计脚本和 focused tests 必须可复跑。
2. 后续若补安全能力，优先补审计保留策略、失败场景支持包样例和真实 TLS controller 回放，不先做框架迁移。
3. 后续若补运维能力，优先补真实发布签名、deb/rpm/OCI 和 systemd security 报告，不引入 GoReleaser/nFPM 作为当前项目默认工具链。
