# cproxy 速查

详细排障见 [TROUBLESHOOTING.md](/root/clash_proxy/TROUBLESHOOTING.md)。

## 安装

```bash
pipx install /path/to/clash_proxy
```

或：

```bash
./scripts/install.sh
```

`./scripts/install.sh` 会自动尝试一键部署（等价于 `cproxy bootstrap`）。
它也会刷新 root 级 `clash-proxy` / `clash-proxy-update` 系统命令；不会默认覆盖
`cproxy` alias。只想安装用户级 Python `cproxy` 时，可设置
`CPROXY_INSTALL_SYSTEM_COMMANDS=0`。

默认以 editable 方式安装（开发便利，改动立即生效）。生产环境应使用非
editable 安装，避免未提交的工作区改动直接影响生产命令：

```bash
CPROXY_EDITABLE=0 ./scripts/install.sh
```

安装脚本会按顺序准备默认 GeoIP 数据文件：复用已有的用户级文件 → 复用仓库根目录的 `Country.mmdb`（本机遗留副本，已不入库）→ 从 [meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) 下载（可用 `CPROXY_GEODATA_DOWNLOAD=0` 关闭）→ 提示手动放置：

- `~/.local/share/cproxy/country.mmdb`

如果缺失，会打印警告，但不会阻塞安装。

## mihomo 二进制

mihomo 的安装/升级统一走 `scripts/install-mihomo.sh`，版本与 sha256 固定在脚本内：

```bash
bash scripts/install-mihomo.sh --check        # 检查已安装版本是否符合固定版本
sudo bash scripts/install-mihomo.sh           # 安装/升级到固定版本（sha256 强制校验）
bash scripts/install-mihomo.sh --fetch-hash vX.Y.Z   # 升级前获取新版本应写入的哈希常量
```

升级到新的 mihomo 版本时，先用 `--fetch-hash` 获取哈希并更新脚本里的
`MIHOMO_VERSION_PIN` / `MIHOMO_SHA256_PIN` 常量，再执行安装。
若 mihomo 正在运行，安装后需重启服务才生效。

## 快照、回滚与一键刷新

`cproxy render` 和订阅更新会在覆盖配置前自动留快照（各保留最近 10 份，位于
`~/.local/state/cproxy/snapshots/`）：

```bash
cproxy snapshots            # 列出快照
cproxy rollback             # 回滚到上一份运行配置（运行中会自动重启）
cproxy rollback <快照名>     # 恢复指定快照（含 config 类快照）
```

`cproxy refresh` 把日常维护串成一条命令：更新订阅（配置了
`subscription-url` 或 `--subscription-url` 时）→ render → 重启应用 →
对 `--group`（或配置里的 `refresh-groups`）指定的 select 组做延迟探测，
当前节点失效时自动切到延迟最低的节点：

```bash
cproxy refresh --group SSRDOG
```

订阅更新只覆盖节点/规则等内容：安全相关键（`program-path`、`secret` 系列、
controller、`allow-lan` 等）即使订阅携带也一律剔除，防止恶意订阅注入；
本地优先键（端口、模式等）保留本地已配置的值；订阅拉取失败不会阻断后续
render 与探测。输出中的订阅地址会脱敏。

## root 生产系统命令

当前生产入口仍是 root 级 `proxy.sh`。在仓库根目录运行：

```bash
sudo ./scripts/install-system-commands.sh
```

默认安装：

- `clash-proxy`：转发到仓库内 `proxy.sh`
- `clash-proxy-update`：转发到仓库内 `update_config.sh`

常用命令：

```bash
clash-proxy status
clash-proxy status --raw
clash-proxy menu
clash-proxy import-subscription "https://example.com/sub" --dry-run
clash-proxy-update --dry-run config_1.yaml
clash-proxy-update --apply config_1.yaml
```

`clash-proxy status` 默认显示彩色、带 icon 的产品化状态面板；脚本消费继续使用
`clash-proxy status --raw`。如需关闭 icon，可设置 `CPROXY_ICONS=0`。
`clash-proxy ai-status` 也会默认展示状态 icon 和 `[US]`、`[SG]` 国家徽标，便于直接识别当前 AI
出口区域。
`clash-proxy menu` 会进入交互式控制台，可直接选择查看状态、切换 AI 路由、
导入订阅、重新渲染并重启等常用操作；菜单里的订阅导入默认只做 dry-run。
`clash-proxy import-subscription <url>` 支持导入完整 Clash/Mihomo YAML 订阅，也支持把
Base64 VLESS 节点列表转换为最小可用配置；默认 `--dry-run` 只下载和校验，显式
`--apply` 才会写入并刷新。
要让转换后的订阅配置使用指定分组名，先用
`clash-proxy import-subscription <url> --dry-run --group CyberGuard`；确认后再用
`--apply --group CyberGuard`。
如果只想导入为可选分组并自己手动选择，使用
`clash-proxy import-subscription <url> --dry-run --group CyberGuard --attach-to AI-MANUAL`；
确认后再用 `--apply --group CyberGuard --attach-to AI-MANUAL`。这会把 `CyberGuard`
加入 `AI-MANUAL` 候选列表，不修改 `MATCH` 规则，也不会自动切换。

如果明确要把 `cproxy` 也指向 root 生产入口，可显式安装别名：

```bash
sudo ./scripts/install-system-commands.sh --with-cproxy-alias
```

注意：`cproxy status` 属于用户级 XDG 入口，使用 `~/.config/cproxy` 和
`~/.local/share/cproxy`。如果未初始化用户级 runtime，它会显示
`运行配置状态: 待刷新`，这不代表 root 生产入口不可用。生产排障优先使用
`clash-proxy status` 或在仓库根目录运行 `./proxy.sh status`。

## 初始化

```bash
cproxy init
```

或直接无参数一键：

```bash
cproxy bootstrap
```

配置文件位置：

- `~/.config/cproxy/config.yaml`

常见可调项：

```yaml
program-path: /usr/local/bin/mihomo
api-timeout: 2
external-controller-tls: 127.0.0.1:9443
secret-systemd-credential: controller-secret
# 或使用本地文件引用，避免把真实 secret 写入 YAML:
# secret-file: ~/.config/cproxy/controller-secret
# 或使用 Python keyring 后端:
# secret-keyring-service: cproxy
# secret-keyring-username: controller
audit-journald: true
test-timeout: 5000
connectivity-timeout: 5
```

## 首次启动

```bash
cproxy render
cproxy test
cproxy start
cproxy logs --lines 50
cproxy status
```

GA 本地检查和脱敏支持包：

```bash
cproxy security-check
cproxy security-check --strict
cproxy support-bundle --output /tmp/cproxy-support.tar.gz
```

如果 `cproxy test` 提示缺少 `country.mmdb`，先把该文件放到：

- `~/.local/share/cproxy/country.mmdb`

## 用户级 TUI

```bash
cproxy tui
cproxy-tui
```

TUI 是当前 Python/Textual `cproxy` 的用户级控制台，使用 `~/.config/cproxy`、`~/.local/share/cproxy` 和 `~/.local/state/cproxy`。页签包括 Overview、Nodes、Providers、Connections、AI Route、Subs、Config、Proxy 和 Logs。Providers 支持手动更新 `/providers/proxies`；Connections 支持查看 `/connections`、断开选中连接，并对断开全部连接做二次确认。

## AI 路由

查看状态：

```bash
cproxy ai-status
cproxy ai-status --raw
cproxy current "AI-MANUAL"
cproxy current "AI-MANUAL" --raw
```

查看候选项：

```bash
cproxy list-groups
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL"
cproxy list-nodes "AI-MANUAL" --raw
```

手动切换：

```bash
cproxy switch "AI-MANUAL" "AI-SG"
cproxy switch "AI-MANUAL" "AI-AUTO"
```

检查延迟：

```bash
cproxy test-group "AI-AUTO"
cproxy test-group "AI-AUTO" --raw
```

后端策略：

- `current/list-groups/list-nodes`：API 优先，API 不可达时回退 `runtime.yaml`
- `ai-status/test-group/switch`：只依赖 API
- `ai-status` 默认还会通过本地代理探测 `chatgpt.com` 与 `api.openai.com/v1/models`，失败时会做最多 2 次轻量重试

如需覆盖探测地址：

```yaml
ai-chatgpt-url: https://chatgpt.com
ai-openai-api-url: https://api.openai.com/v1/models
ai-probe-timeout: 8
```

## 稳定性探测

多轮延迟探测，找出组内最稳定的叶子节点；可选自动切换：

```bash
cproxy probe-stable-node                          # 默认 AI-MANUAL, codex profile
cproxy probe-stable-node AI-MANUAL --profile chatgpt --strategy aggressive --rounds 3
cproxy probe-stable-node --url https://github.com --timeout 5000
cproxy probe-stable-node --switch                 # 合格时自动切换
cproxy probe-stable-node --raw                    # TSV 格式，适合脚本消费
```

选项：

- `group`（位置参数，默认 `AI-MANUAL`）：目标代理组
- `--profile`：`codex`（默认）/ `chatgpt` / `github` / `claude`，预设探测 URL 和策略
- `--strategy`：`conservative` / `balanced` / `aggressive`，覆盖 profile 默认策略
- `--url`：自定义探测 URL
- `--rounds`：探测轮数（默认由策略决定，conservative 为 5 轮）
- `--timeout`：单次延迟测试超时（毫秒，默认 8000）
- `--switch`：探测完成后，若推荐节点合格则自动切换
- `--raw`：TSV 输出（GROUP / PROFILE / STRATEGY / ROUNDS / URL / CURRENT / CURRENT_STABLE / STABLE / BEST / SWITCH 或 SKIP_SWITCH / NODE 前缀）

算法要点：

- 递归展开嵌套组到叶子节点，跟踪当前叶子，带 cycle 保护
- 每轮淘汰排名下半的节点（保留当前节点）
- 稳定门槛：全成功 + 最大/平均延迟不超过策略阈值
- 当前节点也稳定时，按绝对值和比例防抖，避免频繁切换
- `--switch` 时按嵌套路径反序切换（从最内层组到最外层组）

## 命令级代理

输出环境变量：

```bash
cproxy proxy-env
```

仅让单条命令走代理：

```bash
cproxy with-proxy curl https://chatgpt.com
cproxy with-proxy env | rg "^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)="
```

打开临时代理 shell：

```bash
cproxy proxy-shell
```

如果 shell 参数以 `-` 开头，要显式加 `--`：

```bash
cproxy proxy-shell -- -c 'env | rg PROXY'
```

## 连通性与状态

```bash
cproxy test
cproxy logs
cproxy logs --lines 200
cproxy status
cproxy status --raw
```

说明：

- `test` 会先校验当前运行进程是否属于 `cproxy`
- `test` 会检查默认 GeoIP 数据文件 `~/.local/share/cproxy/country.mmdb`
- `stop` 和 `restart` 也会做同样的 ownership 校验

## 用户级 systemd

安装用户级服务：

```bash
./systemd-user/install-systemd-user.sh
```

查看状态：

```bash
systemctl --user status cproxy.service --no-pager -l
systemctl --user status cproxy-refresh.timer --no-pager -l
```

为其它用户级服务生成代理 drop-in 指引：

```bash
./systemd-user/generate-proxied-service.sh my-service
```

## 旧目录迁移

```bash
cproxy migrate-from-legacy /root/clash_proxy
```

只迁移必要配置，不迁移旧日志、PID 和临时文件。

## 输出模式

默认输出面向人类阅读，`--raw` 只用于脚本消费。

默认输出区块已统一为：

- `摘要`
- `资源`
- `路径`
- `连通性`
- `链路`
- `备用`
- `分组`
- `列表`
- `结果`

其中不同命令只显示自己需要的区块，`--raw` 保持原始稳定格式。

着色规则：

- 默认启用
- 可通过 `FORCE_COLOR=1 cproxy status` 强制开启
- 可通过 `CPROXY_COLOR=always|never|auto` 明确控制
- 可通过 `NO_COLOR=1` 禁用颜色
- `cproxy` 默认启用状态 icon，可通过 `CPROXY_ICONS=0` 或配置 `output-icons: false` 关闭
- 脚本场景优先使用 `--raw`

对应配置项：

```yaml
output-color: always
output-icons: true
```

适合脚本消费的命令：

```bash
cproxy status --raw
cproxy ai-status --raw
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL" --raw
cproxy test-group "AI-AUTO" --raw
```
