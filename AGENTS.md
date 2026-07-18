# Repository Instructions

## Git Policy

- If the user explicitly asks for `commit and push`, `提交并推送`, or an equivalent publish request, the agent may commit the scoped project changes and push the current branch without asking for another confirmation.
- Before committing, run `git status --short`, keep unrelated or pre-existing dirty files out of the commit, and include new files that belong to the requested change.
- Do not push for ordinary code changes unless the user explicitly requested commit-and-push or publish.
- Do not force push, delete branches, delete worktrees, or rewrite shared history unless the user explicitly confirms that exact operation.

## Installed Command Copy

- The live root-level command is `/usr/local/bin/clash-proxy`, which executes payloads under `/usr/local/lib/clash-proxy`.
- When changing `proxy.sh` or helper scripts used by `probe-stable-node`, `ai-use`, `guard`, `ai-connections`, or `incident`, verify whether the installed payload also needs to be refreshed.
- Prefer `scripts/install-system-commands.sh` for refreshing the installed payload so helper files stay in sync with the wrapper layout.

## GeoIP Data

- `Country.mmdb` / `GeoSite.dat` 不再提交进 git。仓库根目录的 `Country.mmdb` 磁盘文件仍被运行中的 legacy mihomo（`-d /root/clash_proxy`）使用，不要删除。
- `scripts/install.sh` 的 GeoIP 准备顺序：已有用户级文件 → 仓库根目录遗留副本 → 从 meta-rules-dat 下载（`CPROXY_GEODATA_DOWNLOAD=0` 关闭）→ 打印手动放置警告。

## Mihomo Binary

- mihomo 二进制的安装/升级统一走 `scripts/install-mihomo.sh`，版本与 sha256 以 `MIHOMO_VERSION_PIN` / `MIHOMO_SHA256_PIN` 常量固定在脚本内。
- 升级流程：`bash scripts/install-mihomo.sh --fetch-hash vX.Y.Z` 获取新哈希 → 更新两个常量 → `sudo bash scripts/install-mihomo.sh`。
- 不要用 `mihomo.gz` 手工放置的方式安装。

## Release

- 版本号权威来源是 `pyproject.toml`；发布流程见 `STATUS.md` 的“发布”一节（bump 版本 → CHANGELOG → git tag → GA 产物）。
- 生产安装使用 `CPROXY_EDITABLE=0 ./scripts/install.sh`（非 editable）；开发实验不要在生产 checkout 直接改。

## Legacy Retirement

- `proxy.sh` 工作流已冻结新功能，只接受安全修复；新功能只进 `cproxy`。
- 退役条件与分阶段步骤见 `docs/plans/2026-07-18-proxy-sh-retirement.md`；当前项目状态见 `STATUS.md`。
