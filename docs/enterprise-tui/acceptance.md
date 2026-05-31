# 企业 TUI 验收命令

本文档把 `deep-research-report.md` 的验收要求落到当前 Python/Textual cproxy 项目。命令默认在仓库根目录执行。

## 基础静态审计

```bash
bash "scripts/audit-enterprise-tui.sh"
```

验收标准：

| 检查项 | 通过条件 |
|---|---|
| 技术栈 | `pyproject.toml` 保持 Python `>=3.11`、Textual optional dependency、`cproxy-tui` 入口 |
| 重写防线 | 仓库根目录没有 `go.mod`，源码中没有 Bubble Tea runtime 依赖 |
| 文档落地 | `docs/enterprise-tui/overview.md`、`acceptance.md`、`security-ops.md` 均存在并声明“不做 Go/Bubble Tea 重写，当前项目采用 Python 3.11 + Textual” |
| 变更范围 | 输出当前 `src`、`tests`、`pyproject.toml`、`README.md`、`USAGE.md` 的 worktree 变更清单，便于最终验收对账 |
| 企业安全入口 | `external-controller-tls`、`external-controller-unix` 风险处理、secret provider、结构化审计和供应链 CI 均有仓库证据 |
| GA readiness | `security-check`、`support-bundle`、GA artifact build/verify、80/120 布局和 5000 connections 压力 smoke 均有测试或脚本 |

## TUI 回归

```bash
PYTHONPATH="src" python3 -m pytest \
  "tests/test_tui_app.py" \
  "tests/test_tui_proxies.py" \
  "tests/test_tui_connections_providers.py" \
  "tests/test_ga_readiness.py" \
  "tests/test_tui_subscriptions.py" \
  "tests/test_tui_logs.py" \
  -q
```

覆盖点：

| 测试文件 | Direct Evidence | 验收目标 |
|---|---|---|
| `tests/test_tui_app.py` | 覆盖页签切换、从 tabbar 进入内容、escape 回退、表单/按钮/日志/AI 表格焦点 | 键盘主流程可达 |
| `tests/test_tui_proxies.py` | 覆盖节点切换、API 不可用、左右/escape 焦点、节点表边界 | 策略组页面核心行为稳定 |
| `tests/test_tui_connections_providers.py` | 覆盖连接关闭、关闭全部二次确认、Provider 更新 | 连接追踪与 Provider 手动更新可操作 |
| `tests/test_ga_readiness.py` | 覆盖 security-check、支持包脱敏、journald hook、GA artifacts、5000 connections 压力 smoke | GA readiness gate 可本地复跑 |
| `tests/test_tui_subscriptions.py` | 覆盖订阅命令拼装、用户路径 env、临时 refresh script、URL 脱敏、输出摘要 | 订阅导入不泄漏 token，dry-run/apply 可区分 |
| `tests/test_tui_logs.py` | 覆盖日志 tail thread 停止和 unmount 不阻塞 | 日志页退出不会拖住 TUI |

## 后端与运行时回归

```bash
PYTHONPATH="src" python3 -m pytest \
  "tests/test_backend_services.py" \
  "tests/test_runtime_and_process.py" \
  "tests/test_diagnostics_and_proxy_commands.py" \
  -q
```

覆盖点：

| 测试文件 | Direct Evidence | 验收目标 |
|---|---|---|
| `tests/test_backend_services.py` | 覆盖 `QueryService` API 模型和 runtime fallback | API 不可用时仍能只读查看 runtime |
| `tests/test_api_backend.py` | 覆盖 TLS controller、Unix socket 拒绝、secret-file 和 systemd credential secret 读取 | Controller/secret 安全路径可复测 |
| `tests/test_runtime_and_process.py` | 覆盖 render/start/status/restart/stop、stale pidfile 不误杀外部进程 | 运行时生命周期不越权 |
| `tests/test_diagnostics_and_proxy_commands.py` | 覆盖组延迟、proxy env、连通性、GeoIP 缺失、AI probe retry/backoff | 日常排障命令可复跑 |

## 运维示例与安装 dry-run

```bash
bash "tests/systemd_user_examples_test.sh"
systemd-analyze verify "systemd-user/cproxy.service"
bash "tests/system_command_installer_test.sh"
bash "scripts/install-system-commands.sh" --dry-run --bindir "/tmp/cproxy-enterprise-tui-audit/bin"
bash "scripts/build-ga-artifacts.sh" "/tmp/cproxy-ga"
bash "scripts/verify-ga-artifacts.sh" "/tmp/cproxy-ga"
```

验收标准：

| 命令 | 通过条件 |
|---|---|
| `systemd_user_examples_test.sh` | 用户级 service/override/env 示例仍指向 `%h/.local/bin/cproxy` 和用户环境文件 |
| `systemd-analyze verify` | systemd 能解析仓库内 user service；如果输出其他系统 unit 的 warning，只要返回码为 0 且没有指向 `systemd-user/cproxy.service` 的 error 即可 |
| `system_command_installer_test.sh` | wrapper 安装 dry-run 不创建 bindir，真实测试只写临时目录 |
| `install-system-commands.sh --dry-run` | 只打印将安装的 `clash-proxy`、`clash-proxy-update` 和 payload，不写 `/usr/local` |
| `build-ga-artifacts.sh` / `verify-ga-artifacts.sh` | 生成 source tarball、`SHA256SUMS` 和 `provenance.json`，并能校验 checksum 与 provenance 字段 |

## 目标环境 GA smoke

以下命令读取目标机器的真实用户配置，发布前应在目标用户下执行。它们可能因为当前机器尚未配置 TLS controller 或 secret provider 而失败；这种失败表示环境未达 GA 配置，不表示仓库测试失败。

```bash
PYTHONPATH="src" python3 -m cproxy.cli security-check --strict
PYTHONPATH="src" python3 -m cproxy.cli support-bundle --output "/tmp/cproxy-support.tar.gz"
journalctl --user -t cproxy-audit -n 20 --no-pager
```

systemd 安全分析按目标系统能力选择：

```bash
if systemd-analyze --help | rg -q -- "--offline"; then
  systemd-analyze security --offline=yes "$(pwd)/systemd-user/cproxy.service"
else
  systemd-analyze verify "systemd-user/cproxy.service"
  systemd-analyze --user security "cproxy.service"
fi
```

验收标准：

| 命令 | 通过条件 |
|---|---|
| `security-check --strict` | `external-controller-tls` 已配置，controller 地址为 loopback，secret 来自 systemd credential、secret-file 或 keyring |
| `support-bundle` | tar.gz 能生成，内部 `config`、`runtime`、audit 和日志尾部已脱敏 |
| `journalctl -t cproxy-audit` | 在启用 `audit-journald: true` 后能看到脱敏审计事件 |
| `systemd-analyze security` | 新版 systemd 可离线分析仓库 unit；旧版 systemd 需先 verify 仓库 unit，再在目标用户已安装 `cproxy.service` 后分析真实 unit |

## 可选本地 smoke

以下命令会写入临时 HOME，不碰用户真实配置：

```bash
tmp_home="$(mktemp -d)"
HOME="$tmp_home" XDG_CONFIG_HOME="$tmp_home/.config" XDG_DATA_HOME="$tmp_home/.local/share" XDG_STATE_HOME="$tmp_home/.local/state" \
  PYTHONPATH="src" python3 -m cproxy.cli init
HOME="$tmp_home" XDG_CONFIG_HOME="$tmp_home/.config" XDG_DATA_HOME="$tmp_home/.local/share" XDG_STATE_HOME="$tmp_home/.local/state" \
  PYTHONPATH="src" python3 -m cproxy.cli status --raw
rm -rf "$tmp_home"
```

验收标准：

| 结果 | 含义 |
|---|---|
| `init` 返回 0 | 用户级目录和默认配置可创建 |
| `status --raw` 返回 0 | CLI 可读取临时 XDG 布局并输出状态 |

## 仍需外部环境验证

这些项目不能仅靠本地仓库证明，发布前需要在目标环境执行：

| 增强项 | 当前状态 | 提升置信度所需证据 |
|---|---|---|
| 真实 TLS Mihomo controller | Local-ready | 目标环境 controller 证书、secret 和 `/version` 访问回放 |
| 真实 keyring 后端 | Local-ready | Secret Service/KWallet/pass 后端可用性和 fallback 测试 |
| journald 回放 | Local-ready | `journalctl -t cproxy-audit` 能看到脱敏审计事件 |
| 发布签名 | Local-ready | 真实 signing key、签名上传和验签记录 |
| 长跑 soak | Local smoke | 目标机器 12h soak、CPU/内存阈值和断线重连记录 |
