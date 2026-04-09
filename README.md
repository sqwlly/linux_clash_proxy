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

## 安装

正式入口优先用 `pipx`：

```bash
pipx install /path/to/clash_proxy
```

仓库内也提供了本地安装脚本：

```bash
./scripts/install.sh
```

安装脚本会：

- 优先使用 `pipx install --force --editable`
- 回退到 `python3 -m pip install --user --editable`
- 初始化用户级 `cproxy` 配置目录

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
cproxy start
cproxy status
cproxy ai-status
```

如果你已经有旧仓库配置，可直接迁移：

```bash
cproxy migrate-from-legacy /root/clash_proxy
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

## 输出策略

默认输出面向人类阅读：

- `status`：`运行摘要 + 连接与资源 + 配置路径`
- `ai-status`：`摘要 + 当前链路 + 备用路径 + 分组状态`
- `list-groups`：`组名 / 类型 / 当前选择`
- `list-nodes`：`当前选择 + 候选列表`
- `test-group`：`检查摘要 + 检查结果`
- `test`：`检查摘要 + 检查结果`

脚本场景可显式加 `--raw`：

```bash
cproxy status --raw
cproxy ai-status --raw
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL" --raw
cproxy test-group "AI-AUTO" --raw
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

这不影响 `cproxy` 作为用户级 CLI 使用，但文档和运维入口应优先以 `cproxy` 为准。

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
