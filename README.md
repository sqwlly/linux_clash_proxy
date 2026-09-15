# cproxy

> 项目当前阶段、里程碑与下一步见 [STATUS.md](STATUS.md)。

`cproxy` 是面向用户级安装的 Mihomo CLI，**已是本机生产入口**。旧的 `proxy.sh` 工作流已于 2026-09-11 停用（生产入口切换至 cproxy 用户级链路），遗留文件与 systemd unit 保留原地仅供回滚与对照——退役条件与进度见 [STATUS.md](STATUS.md) 与 [proxy.sh 退役计划](docs/plans/2026-07-18-proxy-sh-retirement.md)。

当前已经具备这些能力：

- 用户级目录初始化
- 原始配置渲染为运行配置
- 启动、停止、重启、状态查询
- AI 路由状态查看与手动切换
- 代理组、节点、延迟检查
- 命令级代理环境注入
- 运行配置自动快照与一键回滚
- 一键刷新：订阅更新、重启、分组探测与失效自动切换
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

- **root 下一律装到系统级 `/usr/local`**（`pip install --force-reinstall --no-deps`），
  刻意不用 `--user`：`systemd-user/cproxy.service` 硬编码 `/usr/local/bin/cproxy`，
  而 PATH 里 `~/.local/bin` 排在它前面——同时存在两份副本时，交互命令与 systemd
  服务会跑不同版本的代码
- 非 root 时优先 `pipx install --force --editable`，回退
  `python3 -m pip install --user --editable`
- 初始化用户级 `cproxy` 配置目录
- 安装 GeoIP 数据到默认路径：优先复用已有用户级文件和仓库根目录遗留的 `Country.mmdb`（该文件已不入库），缺失时尝试从 meta-rules-dat 下载，仍失败则打印手动放置警告
- 刷新 root 级 `clash-proxy` / `clash-proxy-update` 系统命令；这两者是 **legacy 入口**（已停用待退役），不需要可设 `CPROXY_INSTALL_SYSTEM_COMMANDS=0` 跳过。脚本不会默认覆盖 `cproxy` alias

安装脚本**不会中断正在运行的代理**：

- 代理进程由 mihomo 独立持有，`cproxy` 只是控制端——换掉 CLI 的代码不影响它
- 脚本末尾的一键部署会调 `cproxy bootstrap`，其中的 `start` 是**幂等**的：
  检测到代理已在运行就直接返回现有 PID，不会重启（`backend/process.py` 的
  `start()` 以 `is_running()` 短路）
- 配置层面同样支持热重载：`cproxy refresh` 经 Mihomo 的 `PUT /configs` 应用新
  配置，长会话与长任务不中断，仅在 API 不可达时回退进程重启（见
  [安全更新流程](#安全更新流程)）

## GeoIP 数据

`cproxy render` 会注入：

- `GEOIP,CN,DIRECT,no-resolve`

因此运行时依赖 `country.mmdb`。默认手动放置路径是：

- `~/.local/share/cproxy/country.mmdb`

这是因为 `cproxy start` 会用用户级数据目录作为 Mihomo 的 `-d` 工作目录。

需要注意：

- 在无代理或受限网络环境下，Mihomo 不一定能自动获取 `country.mmdb`
- `./scripts/install.sh` 会优先复用仓库根目录的 `Country.mmdb`（本机遗留副本，已不入库），缺失时尝试从 meta-rules-dat 下载
- 如果以上都不可用，先手动放到上面的默认路径，再执行 `cproxy test`

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

诊断与排障：

```bash
cproxy test
```

交互界面：

```bash
cproxy tui
cproxy-tui
cproxy completion bash --install
```

分组名与 `cproxy --help` 保持一致；完整命令清单见 `cproxy` 或 `cproxy --help`，shell 补全见 [USAGE.md](USAGE.md#shell-补全)。

TUI 使用 Python/Textual 实现，不是单独的 Go/Bubble Tea 重写。当前页签覆盖 Overview、Nodes、Providers、Connections、AI Route、Subs、Config、Proxy 和 Logs；其中 Providers 可手动更新 `/providers/proxies`，Connections 可查看 `/connections` 并断开选中连接，断开全部连接需要二次确认。

## 输出策略

默认输出面向人类阅读：

- `status`：`摘要 + 资源 + 流量 + 路径`
- `ai-status`：`摘要 + 连通性 + 链路 + 备用 + 分组`
- `list-groups`：`摘要 + 列表`
- `list-nodes`：`摘要 + 列表`
- `test-group`：`摘要 + 结果`
- `test`：`摘要 + 结果`

统一规则：

- 默认输出优先给结论，再给明细
- 区块标题统一使用 `摘要 / 资源 / 流量 / 路径 / 连通性 / 链路 / 备用 / 分组 / 列表 / 结果`，
  开启图标时（`CPROXY_ICONS=1` 或配置 `output-icons: true`）每个标题带 `▸` 前缀，
  `status` 顶部另有一行 `◆ cproxy` 主标题。`status` 的流量区块带统计窗口限定词，
  实际渲染为 `流量 (今日)`
- `status` 的值列按显示宽度对齐，超长路径压缩为 `~` 与 `…/` 形式（不传 `--raw` 时）
- `status` 的 `流量 (今日)` 区块内置「按进程」明细（`--top N`，`--no-process` 关闭）：
  每个进程给出占总流量比例，以及按链路拆分的 `代理↓ / 代理↑ / 直连↓ / 直连↑`
  四个字节列，便于直接定位"谁在消耗流量、谁在绕开代理"。用四列而非单个代理占比，
  是因为低占比进程（如 0.1%）在百分比下几乎看不出量，拆成字节后
  `0 B` 与 `324 MB` 的对比一目了然
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

`proxy.sh` 工作流已于 2026-09-11 停用；其文件与 systemd unit 保留原地供回滚对照，
退役条件与分阶段步骤见 [proxy.sh 退役计划](docs/plans/2026-07-18-proxy-sh-retirement.md)。
日常使用与排障只看 `cproxy` 即可。

另外，`cproxy` 只管理自己启动的 mihomo 进程：

- `start` 会写入 PID 和 ownership 元数据
- `stop/restart/status/test` 会校验该进程是否仍属于当前 `cproxy`
- stale pidfile 不会再误杀无关进程

## 生产入口识别

生产入口是 **cproxy 用户级链路**：

- 用户级服务：`systemd-user/cproxy.service`
- 用户级路径：`~/.config/cproxy`、`~/.local/share/cproxy`、`~/.local/state/cproxy`
- 定时任务：`cproxy-subscription.timer`（每日订阅更新）、`cproxy-refresh.timer`（周期性重渲染）

legacy 侧（已停用，保留待退役，**不要**再作为入口使用）：

| 组件 | 位置 | 当前状态 |
|---|---|---|
| `clash-proxy.service` | `/etc/systemd/system/` | `inactive` + `disabled` |
| `clash-proxy-refresh.path` / `.timer` / `.service` | `/etc/systemd/system/` | `inactive` + `disabled`（`.path` 的监听已于 2026-09-15 停掉） |
| `clash-proxy-subscription.timer` / `.service` | `/etc/systemd/system/` | `inactive` + `disabled` |
| `clash-proxy`、`clash-proxy-update` | `/usr/local/bin/` | wrapper 仍在 PATH，但其指向的 unit 已停用 |
| `proxy.sh`、`update_config.sh` | 仓库根目录 | 保留对照，不再作为入口 |

> ⚠️ `/usr/local/bin/cproxy-update` 名字形似 cproxy 的子命令，实际转发到 legacy 的
> `update_config.sh`——不要把它当成 cproxy 的一部分。

只读确认：

```bash
systemctl --user is-active cproxy.service    # 预期 active
systemctl is-active clash-proxy.service      # 预期 inactive
```

### legacy 命令对照

legacy 入口的子命令与 cproxy **绝大多数同名同义**——`status`、`switch`、`list-groups`、
`list-nodes`、`current`、`probe-stable-node`、`ai-use`、`shadow-probe`、`shadow-history`、
`guard`、`ai-connections`、`incident`、`test-group`、`proxy-env`、`with-proxy`、
`proxy-shell` 等直接换成 `cproxy <同名>` 即可。只有两处映射不同：

| legacy | cproxy 等价 |
|---|---|
| `clash-proxy import-subscription <url>` | `cproxy refresh`（订阅更新→渲染→热重载→探测） |
| `clash-proxy menu` | `cproxy tui` |

探测策略（`conservative` / `balanced` / `aggressive`）、场景预设（`codex` / `chatgpt` /
`github` / `claude`）、评分口径与自动切换门槛等细节，由 `cproxy <命令> --help` 与
[USAGE.md](USAGE.md) 覆盖——两套实现共用同一份逻辑，故此处不再重复。

> 若 `cproxy status` 显示 `运行配置    待刷新`，确认它读的是
> `~/.local/share/cproxy/runtime.yaml`；那是用户级入口的正常路径。

## 安全更新流程

生产入口以 **热重载** 作为生效边界，长会话与长任务不中断：

1. 先确认入口：`systemctl --user is-active cproxy.service`。
2. 只修改原始配置或订阅产物，不手工编辑 `runtime.yaml`。
3. 更新订阅并应用：

   ```bash
   cproxy refresh
   ```

   它会依次完成：订阅更新（保留本地环境键；单个附加机场失败不影响主订阅与其它机场）
   → render → 经 Mihomo `PUT /configs` 热重载（API 不可达才回退进程重启）→
   分组探测与自动切换。加 `--raw` 得到机器可读输出；`--group <名>` 可重复指定要
   探测并自动切换的分组。

4. 需要回滚时：

   ```bash
   cproxy snapshots           # 列出快照
   cproxy rollback [文件名]    # 省略文件名则回滚最近一份运行时快照
   ```

   render 与订阅更新在覆盖配置前各留一份快照（各保留 10 份）；运行中回滚会自动重启。

每日订阅更新由用户级 `cproxy-subscription.timer` 触发（默认 04:00 + 随机延迟），
`cproxy-refresh.timer` 作为周期性重渲染兜底，定义见
[systemd-user/](/root/clash_proxy/systemd-user/)。

> legacy 侧原先由 `clash-proxy-refresh.path` 监听 `/root/clash_proxy/config.yaml`
> 变化触发刷新，该监听已于 2026-09-15 停掉，对应 unit 全部 `disabled`。

## Mihomo 热 reload

2026-05-14 的历史评估结论是「热 reload 不宜作为生产方案」，理由是缺 CLI 级 reload
参数、也没有失败回滚。**该结论已被取代**：

- `mihomo -h` 确实没有 CLI 级 reload 参数，但 controller API 提供 `PUT /configs`。
  `cproxy refresh` 正是经这条路应用新配置——长会话与长任务不中断。
- 失败回滚改由快照承担：render 与订阅更新在覆盖配置前各留一份
  （`cproxy snapshots` / `cproxy rollback`，各保留 10 份，运行中回滚自动重启）。
- 仅当 API 不可达时才回退到进程重启。

原评估记录见
[docs/plans/2026-05-14-production-entry-and-reload-evaluation.md](/root/clash_proxy/docs/plans/2026-05-14-production-entry-and-reload-evaluation.md)，保留供追溯。

## 用户级 systemd（生产入口）

生产运行使用的用户级单元位于：

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
