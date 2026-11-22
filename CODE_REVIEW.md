# cproxy Code Review 记录

更新日期：2026-11-22

## 结论

当前仓库已完全收敛到 Python 3.11 `cproxy` CLI / Textual TUI（legacy `proxy.sh` 工作流已于 2026-10-17 退役并移除），安全与运维基线完成全面审计加固：

- **Shell 补全防命令注入（P0）**：`cproxy.bash` 改用 `mapfile -t` 零展开读取候选并以 `printf '%q'` 转义含空白/元字符候选，彻底消除 `compgen -W` 二次求值 RCE；`_cproxy` 补齐 `"${words[@]}"` 与 `"${candidates[@]}"` 引号。
- **凭据脱敏与私密落盘（P0）**：扩展 `redaction.py` 覆盖 `uuid`/`private-key`/`psk`/`auth`/`api-key`/`cookie`/`webhook`/`short-id`、带前缀赋值与引号值、`Authorization: Bearer|Basic|Token`、URL userinfo 及 Telegram/Webhook/订阅路径 Token；`support.py`、`notify.py`、`bundle.py`、`install.py`、`audit.py` 统一采用 `0o600` 原子写（或 `O_CREAT` `0o600`），`support.py` 改用 `deque` 流式读尾并容错损坏 YAML。
- **控制器安全校验加固（P0）**：`security.py` 独立校验 `external-controller` 与 `external-controller-tls`，拦截空主机/`:9090`/`*`、宽松 `secret-file` 权限、`secret-systemd-credential` 路径遍历、`external-controller-pipe`、`allow-lan` 通配绑定与无条件告警内联 `secret`。
- **SSRF 与脱敏导入防护（P1）**：`notify.py` 禁用 HTTP 302 重定向并校验 `http/https` + 非空主机名；`bundle.py` 递归识别嵌套 `[REDACTED]`/`?...` 哨兵以保护现有订阅不被覆盖，并按 `RULE_TYPE_ALIASES` 白名单与非法字符校验导入规则。
- **GA 产物与安装/运维脚本加固（P1/P2）**：`build-ga-artifacts.sh` 仅打包已追踪文件并通过 Python 生成 `provenance.json`；`verify-ga-artifacts.sh` 校验归档无路径穿越且实测 SHA-256 与 `provenance.json`、`SHA256SUMS` 三方一致；`install.sh`、`install-mihomo.sh`、`install-verge-task.ps1`、`setup-windows-verge.bat`、systemd oneshot 单元及 CI 工作流完成同步加固。

## 历史记录归档（2026-04-09）

早期基于 `proxy.sh` 与系统级 `clash-proxy.service` 的初版评审问题（旧实例接管、PID 识别、`.service` 后缀兼容、环境模板保护）已全部在 `cproxy` 架构与 `systemd-user/*` 辅助脚本中原生解决，原 legacy 脚本与对应测试已随 2026-10-17 阶段 3 退役删除（详见 [STATUS.md](STATUS.md) 与 [docs/plans/2026-07-18-proxy-sh-retirement.md](docs/plans/2026-07-18-proxy-sh-retirement.md)）。

## 建议的例行检查

更新配置、升级依赖或发布前，可执行：

```bash
ruff check src/ tests/
mypy src/
./scripts/test.sh -q
for s in tests/*.sh; do bash "$s"; done
bash scripts/audit-enterprise-tui.sh
cproxy security-check
cproxy doctor
systemctl --user status cproxy.service --no-pager -l
```
