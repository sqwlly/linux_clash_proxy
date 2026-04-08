# Clash Proxy 运维说明

当前目录提供一套基于 `mihomo + proxy.sh` 的轻量代理管理方式，重点解决两件事：

- Linux 无 GUI 服务器上的节点手动控制
- `OpenAI / Codex / Anthropic / Gemini` 相关流量优先走美国/新加坡节点，并在节点故障时自动切换
- 大陆流量优先直连，不进代理
- 默认不做全局环境代理，需要时按命令启用代理

## 快速开始

首次初始化：

```bash
cp ./config.example.yaml ./config.yaml
./proxy.sh render
./proxy.sh restart
```

快速检查：

```bash
./proxy.sh status
./proxy.sh ai-status
```

临时让单条命令走代理：

```bash
./proxy.sh with-proxy curl https://chatgpt.com
```

## 导航

优先看这几份文件：

- [README.md](/root/clash_proxy/README.md)
  完整运维说明
- [USAGE.md](/root/clash_proxy/USAGE.md)
  常用命令速查
- [TROUBLESHOOTING.md](/root/clash_proxy/TROUBLESHOOTING.md)
  故障排查手册
- [CODE_REVIEW.md](/root/clash_proxy/CODE_REVIEW.md)
  当前代码审查结论与风险记录

目录内关键内容：

- [proxy.sh](/root/clash_proxy/proxy.sh)
  主入口脚本，包含渲染、节点切换、命令级代理
- [config.example.yaml](/root/clash_proxy/config.example.yaml)
  脱敏模板，复制后生成本地 `config.yaml`
- [runtime.yaml](/root/clash_proxy/runtime.yaml)
  渲染后的运行配置
- [systemd](/root/clash_proxy/systemd)
  开机启动、定时刷新、服务代理注入示例
- [tests](/root/clash_proxy/tests)
  当前仓库的基础回归测试

## 配置结构

- 原始配置：`config.yaml`
- 运行配置：`runtime.yaml`
- 管理脚本：`proxy.sh`

首次使用时，先从模板生成本地配置：

```bash
cp ./config.example.yaml ./config.yaml
```

不要直接手改 `runtime.yaml`。它是由 `proxy.sh render` 从本地 `config.yaml` 自动生成的运行配置。真实 `config.yaml` 含节点与凭据，不应入库。

故障排查见 [TROUBLESHOOTING.md](/root/clash_proxy/TROUBLESHOOTING.md)。

## AI 路由设计

渲染后的 `runtime.yaml` 会自动注入以下代理组：

- `AI-US`
  作用：美国节点池，区域内自动切换
- `AI-SG`
  作用：新加坡节点池，区域内自动切换
- `AI-AUTO`
  作用：美国优先，新加坡兜底
- `AI-MANUAL`
  作用：手动入口，默认指向 `AI-AUTO`

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

为避免规则被原配置里的 `ChinaMax` 或旧的 `chatgpt/openai -> SSRDOG` 项抢先命中，渲染逻辑会：

- 删除原有冲突规则
- 强制把 AI 规则插在 `RULE-SET,ChinaMax,DIRECT` 前面
- 在 `MATCH` 前补一条 `GEOIP,CN,DIRECT,no-resolve`，作为大陆流量直连兜底

这意味着你即使手动调整了 `config.yaml` 里 `ChinaMax` 的顺序，只要重新执行 `render`，最终 `runtime.yaml` 仍会把 AI 规则放在 `ChinaMax` 前面。

大陆直连策略是两层：

- `RULE-SET,ChinaMax,DIRECT` 负责常见国内域名
- `GEOIP,CN,DIRECT,no-resolve` 负责未被 `ChinaMax` 覆盖到的国内地址

## 常用命令

### 配置与进程

```bash
./proxy.sh render
./proxy.sh start
./proxy.sh stop
./proxy.sh restart
./proxy.sh status
./proxy.sh logs
./proxy.sh test
```

### 无 GUI 节点控制

```bash
./proxy.sh list-groups
./proxy.sh list-nodes "AI-MANUAL"
./proxy.sh current "AI-MANUAL"
./proxy.sh switch "AI-MANUAL" "AI-SG"
./proxy.sh switch "AI-MANUAL" "AI-AUTO"
./proxy.sh ai-status
./proxy.sh test-group "AI-AUTO"
```

### 命令级代理

```bash
./proxy.sh proxy-env
./proxy.sh with-proxy curl https://chatgpt.com
./proxy.sh proxy-shell
```

说明：

- `proxy-env` 输出当前可用的代理环境变量
- `with-proxy` 只给单条命令注入代理环境
- `proxy-shell` 会打开一个临时带代理环境的子 shell，退出后失效
- 默认不会修改你当前登录 shell 的全局代理环境

## 推荐运维流程

### 更新原始配置后

每次你替换或手工更新 `config.yaml` 后，执行：

```bash
./proxy.sh render
./proxy.sh restart
```

说明：

- `render` 会重新生成带 AI 路由增强的 `runtime.yaml`
- `restart` 才会让 `mihomo` 真正加载新的运行配置

### 临时手动切换 AI 出口

切到新加坡：

```bash
./proxy.sh switch "AI-MANUAL" "AI-SG"
```

恢复自动模式：

```bash
./proxy.sh switch "AI-MANUAL" "AI-AUTO"
```

查看当前状态：

```bash
./proxy.sh ai-status
```

## 关于 ChinaMax 顺序

当前脚本已经做了兜底处理：

- 原始 `config.yaml` 里的 `ChinaMax` 顺序可以继续按你的习惯维护
- 生成 `runtime.yaml` 时，AI 规则会固定放到 `ChinaMax` 前面

所以最终运行时不会再受你手工调整 `ChinaMax` 顺序的影响。

唯一前提是：你改完 `config.yaml` 后，要重新执行一次 `./proxy.sh render`，再执行 `./proxy.sh restart`。

## systemd 示例

项目内已提供示例文件，位于 [systemd/clash-proxy.service](/root/clash_proxy/systemd/clash-proxy.service)、[systemd/clash-proxy-refresh.service](/root/clash_proxy/systemd/clash-proxy-refresh.service)、[systemd/clash-proxy-refresh.timer](/root/clash_proxy/systemd/clash-proxy-refresh.timer) 和 [systemd/clash-proxy-refresh.sh](/root/clash_proxy/systemd/clash-proxy-refresh.sh)。

用途：

- `clash-proxy.service`
  负责开机启动与常规 `start/stop/reload`
- `clash-proxy-refresh.service`
  负责执行一次“render，并在有变化时重启主服务”
- `clash-proxy-refresh.timer`
  负责定时触发 refresh service
- `clash-proxy-command.env.example`
  给“只想让某个服务走代理”的场景提供环境变量模板
- `example-proxied-service.override.conf`
  给已有 systemd 服务做 drop-in 代理注入的示例
- `install-systemd.sh`
  一键安装本项目的 systemd service/timer
- `generate-proxied-service.sh`
  按服务名生成 drop-in 安装指引

### 安装示例

```bash
sudo install -m 644 /root/clash_proxy/systemd/clash-proxy.service /etc/systemd/system/clash-proxy.service
sudo install -m 644 /root/clash_proxy/systemd/clash-proxy-refresh.service /etc/systemd/system/clash-proxy-refresh.service
sudo install -m 644 /root/clash_proxy/systemd/clash-proxy-refresh.timer /etc/systemd/system/clash-proxy-refresh.timer
sudo systemctl daemon-reload
sudo systemctl enable --now clash-proxy.service
sudo systemctl enable --now clash-proxy-refresh.timer
```

如果你希望直接一键安装，也可以执行：

```bash
sudo /root/clash_proxy/systemd/install-systemd.sh
```

### 行为说明

- 开机时，`clash-proxy.service` 会先执行 `render`，再启动代理
- 定时器默认在开机 3 分钟后首次执行，之后每 15 分钟执行一次
- `clash-proxy-refresh.sh` 只会在 `runtime.yaml` 内容发生变化时重启主服务，避免无意义抖动

### 仅给指定服务注入代理

如果你不想开全局代理，而只想让某个 systemd 服务走代理，建议使用 drop-in：

```bash
sudo install -m 644 /root/clash_proxy/systemd/clash-proxy-command.env.example /etc/default/clash-proxy-command
sudo mkdir -p /etc/systemd/system/<your-service>.service.d
sudo install -m 644 /root/clash_proxy/systemd/example-proxied-service.override.conf /etc/systemd/system/<your-service>.service.d/proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart <your-service>
```

说明：

- `/etc/default/clash-proxy-command.example` 会随项目更新，作为最新模板
- `/etc/default/clash-proxy-command` 负责统一维护实际生效的代理环境变量
- drop-in 只给目标服务注入代理，不会影响其它服务
- 目标服务会在 `clash-proxy.service` 之后启动，并依赖它

建议：

- 升级项目后，先对比 `.example` 与正式环境文件差异
- 只在你确认需要时，再把新增默认值合并进 `/etc/default/clash-proxy-command`

如果你不想手写 drop-in，可以直接生成安装指引：

```bash
/root/clash_proxy/systemd/generate-proxied-service.sh <your-service>
```
