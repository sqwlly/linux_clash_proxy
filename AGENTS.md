# Repository Instructions

## Git Policy

- If the user explicitly asks for `commit and push`, `提交并推送`, or an equivalent publish request, the agent may commit the scoped project changes and push the current branch without asking for another confirmation.
- Before committing, run `git status --short`, keep unrelated or pre-existing dirty files out of the commit, and include new files that belong to the requested change.
- Do not push for ordinary code changes unless the user explicitly requested commit-and-push or publish.
- Do not force push, delete branches, delete worktrees, or rewrite shared history unless the user explicitly confirms that exact operation.

## GeoIP Data

- `Country.mmdb` / `GeoSite.dat` 不再提交进 git。
- 运行中 mihomo 的地理数据来自 cproxy 数据目录（`~/.local/share/cproxy/`，含 `country.mmdb`、`geoip.metadb`、`ruleset/`），**不再**从仓库根目录读取——历史上 legacy mihomo 曾以 `/root/clash_proxy` 为 `-d`，那条链路已停用。
- 仓库根目录的 `Country.mmdb` 现在只是 `scripts/install.sh` 的**离线回退数据源**（顺序：已有用户级文件 → 仓库副本 → 从 meta-rules-dat 下载 → 打印手动放置警告）。删掉它会失去离线安装能力，但**不影响正在运行的代理**。

## Mihomo Binary

- mihomo 二进制的安装/升级统一走 `scripts/install-mihomo.sh`，版本与 sha256 以 `MIHOMO_VERSION_PIN` / `MIHOMO_SHA256_PIN` 常量固定在脚本内。
- 升级流程：`bash scripts/install-mihomo.sh --fetch-hash vX.Y.Z` 获取新哈希 → 更新两个常量 → `sudo bash scripts/install-mihomo.sh`。
- 不要用 `mihomo.gz` 手工放置的方式安装。

## Release

- 版本号权威来源是 `pyproject.toml`；发布流程见 `STATUS.md` 的“发布”一节（bump 版本 → CHANGELOG → git tag → GA 产物）。
- 生产安装使用 `CPROXY_EDITABLE=0 ./scripts/install.sh`（非 editable）；开发实验不要在生产 checkout 直接改。
- root 下 `install.sh` 会自动走**系统级**安装（装到 `/usr/local`），不产生 `~/.local` 副本：`cproxy.service` 硬编码 `/usr/local/bin/cproxy`，而 PATH 里 `~/.local/bin` 更靠前，两份副本会让交互命令与服务分叉到不同版本。不要手工执行 `pip install --user`。

## Legacy Retirement

- **`proxy.sh` 工作流已退役**：2026-09-11 停用并完全冻结，**2026-10-17 执行退役阶段 3**——`proxy.sh`、`update_config.sh`、其 helper 脚本（`scripts/{import_subscription,update_subscription_prod,probe_stable_node,ai_tools,probe_history,progress}.py`）、legacy systemd 单元（`systemd/clash-proxy.{service}` / `clash-proxy-refresh.*` / `clash-proxy-subscription.*` / `clash-proxy-command.env.example` / `generate-proxied-service.sh` / `example-proxied-service.override.conf`）与 `scripts/install-system-commands.sh` 已全部**从仓库删除**。新功能只进 `cproxy`。
- **仓库里已无 legacy 代码**：不要再找 `proxy.sh`，也不要再引入「改 legacy 同步副本」这类规则。`install-system-commands.sh` 已不存在（其非 legacy 能力 `--with-cproxy-alias` 一并移除）。
- ⚠️ **不要按名字前缀判断归属**。`clash-proxy-` 横跨新旧两代，退役后仍有活着的同名物：
  - `clash-proxy-traffic-collector.{service,timer}` 名字带 legacy 前缀，实际 `ExecStart=/usr/local/bin/cproxy traffic collect --raw`，是**活跃的 cproxy 组件**，绝不能删；
  - `/usr/local/bin/cproxy` 与 `cproxy-tui` 是 pip 装的**真入口**，不改不覆盖；
  - 判断前先看 unit 的 `ExecStart`，或查 `systemctl is-active`。
- **系统侧也已清完**（2026-10-17）：`/etc/systemd/system/` 的 6 个 legacy 单元、`/usr/local/bin/{clash-proxy,clash-proxy-update,cproxy-update}`、`/usr/local/lib/clash-proxy/` payload、`/etc/default/clash-proxy-command*`、`/etc/logrotate.d/clash_proxy` 与其 cron 行全部删除。回滚路径 = `/root/legacy-system-backup-20261017.tar.gz`（`tar xzf … -C /`）。**保留**：`clash-proxy-traffic-collector.{service,timer}`（活跃）、`/etc/logrotate.d/cproxy` 及其 cron 行、`systemd-user/*`。
- **systemd 单元引用已改指真实生产单元**（2026-10-17）：`src/cproxy/backend/process.py` 的 `SYSTEMD_SERVICE_NAME = "cproxy.service"`，`ops.py` 故障报告查 `systemctl --user is-active/status cproxy.service`（生产单元是**用户级** `systemd-user/cproxy.service`）。⚠️ `start()` 不再通过 `systemctl --user is-active cproxy.service` 拦截——因为 `cproxy.service` 就是 `cproxy` 自身，在 `restart()`（及 `ExecReload`）先 `stop()` 再 `start()` 的窗口期单元仍处于 `active`/`reloading`，查 `is-active` 会把自己当外来实例拦死；同实例幂等由 `is_running()` 负责，跨实例冲突由 `_ensure_no_foreign_instance()` 端口预检负责。`restart()` 刻意不调 `systemctl --user restart cproxy.service` 以免 `ExecReload` 自我递归；整单元重启用 `systemctl --user restart cproxy.service`。
- 退役条件、Gate 状态与分阶段步骤见 `docs/plans/2026-07-18-proxy-sh-retirement.md`；项目状态见 `STATUS.md`。
