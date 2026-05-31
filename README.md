# cproxy

`cproxy` 是一个面向用户级安装的 Mihomo CLI，目标是替代当前仓库里默认绑定 `/root/clash_proxy` 的 `proxy.sh` 工作流。

当前已经具备这些能力：

- 用户级目录初始化
- 原始配置渲染为运行配置
- 启动、停止、重启、状态查询
- AI 路由状态查看与手动切换
- 代理组、节点、延迟检查
- 命令级代理环境注入
- 从旧仓库目录迁移 `config.yaml`

当前内部结构也已经完成第一版 backend 重构：

- `backend/api.py` 负责 Mihomo API
- `backend/runtime.py` 负责 `runtime.yaml` 读取与渲染
- `backend/process.py` 负责进程 ownership 与生命周期
- `services/query.py` 负责 `API 优先 / runtime 回退`
- `services/diagnostics.py` 负责 `test` 与 `test-group`

## 安装

正式入口优先用 `pipx`：

```bash
pipx install /path/to/clash_proxy
```

仓库内也提供了本地安装脚本：

```bash
./scripts/install.sh
```

这条脚本现在会在安装后自动尝试无参一键部署流程（等价于 `cproxy bootstrap`）。

安装脚本会：

- 优先使用 `pipx install --force --editable`
- 回退到 `python3 -m pip install --user --editable`
- 初始化用户级 `cproxy` 配置目录
- 安装仓库自带的 `Country.mmdb` 到默认 GeoIP 数据路径；如果仓库没有该文件，则打印缺失警告
- 刷新 root 级 `clash-proxy` / `clash-proxy-update` 系统命令；不会默认覆盖 `cproxy` alias

## GeoIP 数据

`cproxy render` 会注入：

- `GEOIP,CN,DIRECT,no-resolve`

因此运行时依赖 `country.mmdb`。默认手动放置路径是：

- `~/.local/share/cproxy/country.mmdb`

这是因为 `cproxy start` 会用用户级数据目录作为 Mihomo 的 `-d` 工作目录。

需要注意：

- 在无代理或受限网络环境下，Mihomo 不一定能自动获取 `country.mmdb`
- `./scripts/install.sh` 会优先复用仓库根目录的 `Country.mmdb`
- 如果仓库没有该文件，先手动放到上面的默认路径，再执行 `cproxy test`

常用可选配置项：

- `program-path`
  指定 `mihomo` 可执行文件路径
- `api-timeout`
  控制 Mihomo API 请求超时，默认 `2` 秒
- `external-controller-tls`
  配置后 `cproxy` 优先使用 HTTPS controller，例如 `127.0.0.1:9443`
- `secret-systemd-credential`
  从 systemd `$CREDENTIALS_DIRECTORY` 读取指定 credential 文件
- `secret-file`
  从本地文件读取 controller secret；优先级高于兼容用的 `secret`
- `secret-keyring-service`
  从 Python keyring 后端读取 controller secret，用户名默认 `controller`
- `audit-journald`
  为 `true` 时把操作审计同步写入 `systemd-cat`
- `test-timeout`
  控制 `test-group` 延迟检测超时
- `connectivity-timeout`
  控制 `test` 连通性检查超时

## 快速开始

首次使用：

```bash
cproxy init
```

把你的节点配置写入：

- `~/.config/cproxy/config.yaml`

然后执行：

```bash
cproxy render
cproxy test
cproxy start
cproxy status
cproxy ai-status
```

如果你已经有旧仓库配置，可直接迁移：

```bash
cproxy migrate-from-legacy /root/clash_proxy
```

如果你希望无参数一键完成初始化、自动迁移、渲染和启动：

```bash
cproxy bootstrap
```

## 用户级目录

默认使用 XDG 用户目录：

- 配置：`~/.config/cproxy/config.yaml`
- 运行配置：`~/.local/share/cproxy/runtime.yaml`
- PID：`~/.local/state/cproxy/cproxy.pid`
- 日志：`~/.local/state/cproxy/cproxy.log`

## 常用命令

配置与进程：

```bash
cproxy init
cproxy render
cproxy start
cproxy stop
cproxy restart
cproxy logs
cproxy logs --lines 200
cproxy status
cproxy status --raw
cproxy security-check
cproxy support-bundle --output /tmp/cproxy-support.tar.gz
```

AI 路由控制：

```bash
cproxy list-groups
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL"
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL"
cproxy current "AI-MANUAL" --raw
cproxy switch "AI-MANUAL" "AI-AUTO"
cproxy ai-status
cproxy ai-status --raw
cproxy test-group "AI-AUTO"
cproxy test-group "AI-AUTO" --raw
```

命令级代理：

```bash
cproxy proxy-env
cproxy with-proxy curl https://chatgpt.com
cproxy proxy-shell
cproxy proxy-shell -- -c 'env | rg "PROXY"'
```

连通性检查：

```bash
cproxy test
```

用户级 TUI：

```bash
cproxy tui
cproxy-tui
```

TUI 使用 Python/Textual 实现，不是单独的 Go/Bubble Tea 重写。当前页签覆盖 Overview、Nodes、Providers、Connections、AI Route、Subs、Config、Proxy 和 Logs；其中 Providers 可手动更新 `/providers/proxies`，Connections 可查看 `/connections` 并断开选中连接，断开全部连接需要二次确认。

## 输出策略

默认输出面向人类阅读：

- `status`：`摘要 + 资源 + 路径`
- `ai-status`：`摘要 + 连通性 + 链路 + 备用 + 分组`
- `list-groups`：`摘要 + 列表`
- `list-nodes`：`摘要 + 列表`
- `test-group`：`摘要 + 结果`
- `test`：`摘要 + 结果`

统一规则：

- 默认输出优先给结论，再给明细
- 区块标题统一使用 `摘要 / 资源 / 路径 / 连通性 / 链路 / 备用 / 分组 / 列表 / 结果`
- `--raw` 仍保持脚本友好，不引入这些人类阅读区块
- 颜色默认开启；可用 `FORCE_COLOR=1` 或 `CPROXY_COLOR=always` 显式强制开启
- 可用 `NO_COLOR=1` 或 `CPROXY_COLOR=never` 禁用颜色
- `cproxy` 默认启用状态 icon；可用 `CPROXY_ICONS=0` 或 `output-icons: false` 关闭

`cproxy` 也可以在配置里设置输出偏好：

```yaml
output-color: always   # auto / always / never
output-icons: true
```

`test` 还会额外检查：

- `~/.local/share/cproxy/country.mmdb` 是否存在

脚本场景可显式加 `--raw`：

```bash
cproxy status --raw
cproxy ai-status --raw
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL" --raw
cproxy test-group "AI-AUTO" --raw
```

## 查询后端策略

查询命令现在有明确的后端边界：

- `current`：API 优先，API 不可达时回退 runtime
- `list-groups`：API 优先，API 不可达时回退 runtime
- `list-nodes`：API 优先，API 不可达时回退 runtime
- `ai-status`：只依赖 API
- `switch`：只依赖 API
- `test-group`：只依赖 API

这意味着：

- `render` 后、`start` 前，`current/list-groups/list-nodes` 仍然可用
- `ai-status/switch/test-group` 仍要求 Mihomo API 可访问
- `ai-status` 会额外通过本地代理探测 `chatgpt.com` 与 `api.openai.com/v1/models`，失败时会做最多 2 次轻量重试

如需覆盖默认探测地址，可在 `config.yaml` 里配置：

```yaml
ai-chatgpt-url: https://chatgpt.com
ai-openai-api-url: https://api.openai.com/v1/models
ai-probe-timeout: 8
```

## AI 路由设计

渲染时会自动注入：

- `AI-US`
- `AI-SG`
- `AI-AUTO`
- `AI-MANUAL`

默认 AI 规则覆盖：

- `openai.com`
- `chatgpt.com`
- `oaistatic.com`
- `oaiusercontent.com`
- `anthropic.com`
- `claude.ai`
- `gemini.google.com`
- `aistudio.google.com`
- `ai.google.dev`
- `generativelanguage.googleapis.com`

并且会在 `MATCH` 前补一条：

- `GEOIP,CN,DIRECT,no-resolve`

## 当前边界

当前 Python CLI 还没有把旧仓库里的所有外围资产一起迁完，尤其是：

- `proxy.sh` 仍保留在仓库内，便于对照和渐进迁移

这不影响 `cproxy` 作为用户级 CLI 使用，但当前生产运行态不能只看
`cproxy`。截至 2026-05-14，本机只读核验显示当前 active 的生产入口仍是
root 级 `clash-proxy.service`，并由 `/root/clash_proxy/proxy.sh` 管理。

另外，`cproxy` 只管理自己启动的 mihomo 进程：

- `start` 会写入 PID 和 ownership 元数据
- `stop/restart/status/test` 会校验该进程是否仍属于当前 `cproxy`
- stale pidfile 不会再误杀无关进程

## 生产入口识别

当前仓库同时存在两套入口，排查运行态时必须先确认正在使用哪一套：

- root 级生产入口：`/etc/systemd/system/clash-proxy.service`
- root 级管理脚本：`/root/clash_proxy/proxy.sh`
- root 级运行配置：`/root/clash_proxy/runtime.yaml`
- root 级 PID：`/root/clash_proxy/mihomo.pid`
- root 级系统命令：`clash-proxy`、`clash-proxy-update`
- 用户级 `cproxy` 入口：`systemd-user/cproxy.service`
- 用户级 `cproxy` 路径：`~/.config/cproxy`、`~/.local/share/cproxy`、`~/.local/state/cproxy`

可用下面的一键脚本安装 root 级系统命令：

```bash
sudo ./scripts/install-system-commands.sh
```

`./scripts/install.sh` 也会默认调用该脚本刷新 `clash-proxy` / `clash-proxy-update`；
如果只想安装用户级 Python `cproxy`，可设置 `CPROXY_INSTALL_SYSTEM_COMMANDS=0`。

默认安装：

- `clash-proxy`：转发到仓库内 `proxy.sh`
- `clash-proxy-update`：转发到仓库内 `update_config.sh`

如果确实需要把 `cproxy` 和 `cproxy-update` 也指向 root 生产入口，使用显式别名模式：

```bash
sudo ./scripts/install-system-commands.sh --with-cproxy-alias
```

默认不覆盖 `cproxy`，因为仓库里还保留了用户级 Python CLI。若
`cproxy status` 显示 `运行配置状态: 待刷新`，先确认它看的是否是
`~/.local/share/cproxy/runtime.yaml`；生产入口状态应优先使用：

```bash
clash-proxy status
clash-proxy status --raw
clash-proxy import-subscription "https://example.com/sub" --dry-run
clash-proxy probe-stable-node --switch
clash-proxy menu
```

`clash-proxy status` 默认显示彩色、带 icon 的产品化状态面板；自动化脚本应使用
`clash-proxy status --raw`。如需关闭 icon，可设置 `CPROXY_ICONS=0`。
`clash-proxy ai-status` 同样默认展示状态 icon 和 `[US]`、`[SG]` 国家徽标，方便直接识别当前 AI
出口区域。
`clash-proxy probe-stable-node` 默认会递归展开 `AI-MANUAL` 可达的 AI 路由池，对所有叶子节点至少做一轮只读延迟探测，随后按逐轮淘汰保留更优候选，并按成功率、失败数、
最大延迟、平均延迟、抖动和最近历史表现计算 `score`，推荐更稳定的节点；人类模式会预览如果请求切换将执行还是跳过。加上 `--switch`
后只有推荐节点满足稳定门槛且明显优于当前稳定节点才会沿对应分组路径自动切换。
人类模式会在探测期间通过 `tqdm` 输出逐轮进度条；`--raw` 不输出进度，保持脚本友好。
内置 `conservative`、`balanced`、`aggressive` 三档策略，以及 `codex`、`chatgpt`、`github`、`claude` 场景预设。
默认 `codex` 场景使用保守策略：至少 5 轮、全成功、0 失败、最大延迟不超过 3000ms、平均延迟不超过 1500ms；如果当前节点也稳定，
推荐节点还需要带来至少 100ms 或 20% 的平均延迟改善。可用 `--profile`、`--strategy`、`--url`、`--rounds`、`--timeout` 覆盖；
降低 `--rounds` 不会降低自动切换的最少轮数要求。
`clash-proxy ai-use codex` 会按场景探测并切换，`clash-proxy shadow-probe codex` 只记录历史不切换，
`clash-proxy shadow-history` 会摘要展示最近探测历史。
`clash-proxy guard codex` 只选择稳定出口，不启动 `codex`；也可以用 `clash-proxy guard codex -- <cmd>` 包裹一条命令。`clash-proxy ai-connections`
展示 AI/GitHub 相关活动连接，`clash-proxy incident codex` 输出故障排查报告。
`clash-proxy import-subscription <url>` 会下载完整 Clash/Mihomo YAML 订阅，或把 Base64 VLESS 节点列表转换为最小可用配置后走安全更新校验；默认
`--dry-run` 不写入配置，只有显式 `--apply` 才会调用现有 `update_config.sh --apply`。当前不转换 `ss://`、`vmess://`、`trojan://` 等其他节点 URI。
如果要让转换后的订阅配置使用指定分组名，使用
`clash-proxy import-subscription <url> --dry-run --group CyberGuard`；生成的配置会使用 `CyberGuard` 和
`CyberGuard-Auto` 两个组。确认后再用 `--apply --group CyberGuard`。
如果只想导入后自己手动选择，不希望订阅立即接管默认流量，使用
`clash-proxy import-subscription <url> --dry-run --group CyberGuard --attach-to AI-MANUAL`；
它会把 `CyberGuard` 加到 `AI-MANUAL` 候选列表，不修改现有 `MATCH` 规则，也不会执行切换。确认后再用
`--apply --group CyberGuard --attach-to AI-MANUAL`。
`clash-proxy menu` 会进入交互式控制台，适合重复查看状态、切换 AI 路由、导入订阅或执行
常用维护动作；菜单里的订阅导入默认也是 dry-run，只有确认立即应用才写入本地配置。

本机可用这些只读命令确认当前运行入口：

```bash
systemctl cat clash-proxy.service --no-pager
systemctl is-active clash-proxy.service
systemctl --user is-active cproxy.service
ps -p "$(cat /root/clash_proxy/mihomo.pid)" -o "pid=,ppid=,args="
```

当前核验结果是：root 级 `clash-proxy.service` 为 `active`，用户级
`cproxy.service` 为 `inactive`，mihomo 进程参数为
`/usr/local/bin/mihomo -f /root/clash_proxy/runtime.yaml -d /root/clash_proxy`。
因此生产排障应优先看 root 级 unit、`proxy.sh`、root 级 `runtime.yaml` 和
`mihomo.pid`；`cproxy status` 只代表用户级 XDG 入口的状态。

## 安全更新流程

root 级生产入口当前仍以 restart 作为生效边界。安全更新应保持以下顺序：

1. 先确认入口：用上面的只读命令确认当前是否仍由 `clash-proxy.service` 管理。
2. 只修改原始配置或订阅产物，不手工编辑 `runtime.yaml`。
3. 先 dry-run 候选配置：

   ```bash
   /root/clash_proxy/update_config.sh --dry-run /root/clash_proxy/config_1.yaml
   ```

   如果候选配置来自完整 YAML 订阅，也可以先运行：

   ```bash
   clash-proxy import-subscription "https://example.com/sub" --dry-run
   ```

   如果需要让转换后的订阅配置使用指定分组名，先运行：

   ```bash
   clash-proxy import-subscription "https://example.com/sub" --dry-run --group CyberGuard
   ```

   如果只导入为可选分组、不切换当前出口，先运行：

   ```bash
   clash-proxy import-subscription "https://example.com/sub" --dry-run --group CyberGuard --attach-to AI-MANUAL
   ```

4. 需要应用时再执行：

   ```bash
   /root/clash_proxy/update_config.sh --apply /root/clash_proxy/config_1.yaml
   ```

   或者对指定分组订阅执行：

   ```bash
   clash-proxy import-subscription "https://example.com/sub" --apply --group CyberGuard
   ```

   或者只导入为可选分组、不切换当前出口：

   ```bash
   clash-proxy import-subscription "https://example.com/sub" --apply --group CyberGuard --attach-to AI-MANUAL
   ```

5. `--apply` 写入源配置后会进入受控 refresh 流程；也可以等待 systemd path/timer 触发。

`systemd/clash-proxy-refresh.sh` 的现有流程是：备份当前 `runtime.yaml`，render，
比较 hash，执行 `mihomo -t`，仅在校验通过且配置变化时 restart 服务，并在 API
探活失败后回滚上一份 runtime 并尝试恢复服务。API 探活会检查 `/version` 和
`/proxies` 中的 `AI-MANUAL`、`AI-AUTO`、`AI-US`、`AI-SG`。

root 级 systemd 安装还提供 `clash-proxy-refresh.path` 和
`clash-proxy-refresh.timer`。`.path` 监听 `/root/clash_proxy/config.yaml` 变化后
触发 refresh，timer 作为周期性兜底。

`update_config.sh` 不再注入 AI groups/rules；AI 路由由 `proxy.sh render` 统一生成。
默认模式是 `--dry-run`，只校验候选 YAML，不写源配置、不触发 refresh。

## Mihomo 热 reload 评估

当前仓库证据不足以把 Mihomo 热 reload 作为替代 restart 的生产方案：

- `mihomo -h` 只展示 `-f`、`-d`、`-t`、`-v`、controller override 等启动和校验参数，没有 CLI 级 reload 参数。
- `clash-proxy.service` 的 `ExecReload` 当前等价于 `/root/clash_proxy/proxy.sh restart`。
- `proxy.sh restart` 当前实现是 `stop` 后 `start`。
- `systemd/clash-proxy-refresh.sh` 的安全边界依赖 restart 后 API 探活和失败回滚，没有热 reload 回滚实现。

结论：热 reload 可以作为后续小范围实验方向，但当前不适合作为生产替代方案。
在补齐可验证入口、失败回滚、运行态一致性检查和测试前，继续使用现有
render + `mihomo -t` + restart 流程更可控。

## 用户级 systemd

仓库里新增了一套用户级示例，位于：

- [cproxy.service](/root/clash_proxy/systemd-user/cproxy.service)
- [cproxy-refresh.service](/root/clash_proxy/systemd-user/cproxy-refresh.service)
- [cproxy-refresh.timer](/root/clash_proxy/systemd-user/cproxy-refresh.timer)
- [install-systemd-user.sh](/root/clash_proxy/systemd-user/install-systemd-user.sh)
- [generate-proxied-service.sh](/root/clash_proxy/systemd-user/generate-proxied-service.sh)

安装：

```bash
./systemd-user/install-systemd-user.sh
```

启用后使用：

```bash
systemctl --user status cproxy.service --no-pager -l
systemctl --user status cproxy-refresh.timer --no-pager -l
```

## 相关文档

- [USAGE.md](/root/clash_proxy/USAGE.md)
- [TROUBLESHOOTING.md](/root/clash_proxy/TROUBLESHOOTING.md)
- [docs/plans/2026-04-09-cproxy-distribution-design.md](/root/clash_proxy/docs/plans/2026-04-09-cproxy-distribution-design.md)
- [docs/plans/2026-04-09-cproxy-distribution-implementation.md](/root/clash_proxy/docs/plans/2026-04-09-cproxy-distribution-implementation.md)
- [docs/plans/2026-05-14-production-entry-and-reload-evaluation.md](/root/clash_proxy/docs/plans/2026-05-14-production-entry-and-reload-evaluation.md)
