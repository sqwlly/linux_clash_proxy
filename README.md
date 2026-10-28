# cproxy

面向 AI 编程工具（Codex / Claude Code / Gemini / Antigravity / Cursor）与开发工作流的 **Mihomo 智能路由调度与代理管理工具**。

支持两种运行形态：
- **Linux 独立托管模式**：完整管理 Mihomo 进程、订阅拉取、配置渲染、TUN 透明代理与 `systemd --user` 自动化定时器。
- **Windows / Clash Verge 旁路增强模式**：由 Clash Verge (Rev) 管理内核与界面，`cproxy` 自动注入全局扩展脚本并通过 External Controller API 提供后台看门狗、长连接防断流与节点稳定性优选。

---

## 核心特性

- **AI 流量精细分流与地区防封锁**
  - 自动构建 `AI-GEMINI`（针对 Google / Gemini / Cloud Code 自动优选韩、台、英、欧等合规低延迟地区）与 `AI-MANUAL`（针对 OpenAI / Codex / Claude / Copilot 优选美、日、新等主力节点）。
  - 自动清洗机场订阅中的冲突 AI 规则，置顶注入 AI 域名与进程规则，开发下载源（PyPI / npm / PyTorch）与国内流量自动直连。
- **长连接抗断流优选（Anti-EOF）**
  - 自动识别在 15~60s 模型思考或工具执行空闲期易被网关掐断的节点（如 `IEPL` / `IPLC` 专线或裸 TCP `xtls-rprx-vision`），优先将 `1X`、`WebSocket`、`gRPC` 等长连接稳态节点排在首位，并将高倍率（`>= 3.0X`）节点自动沉底。
- **语义级看门狗（`ai-watch`）**
  - 突破传统 `generate_204` 仅测连通性的局限：每 20 秒对活跃出口做 AI 接口语义探测，精准捕获「节点能 ping 通但返回地区不支持（400/403）或 TLS 假死」，连续失败自动切往下一个健康节点。
- **连接池治理与 In-Flight 零中断保护**
  - 基于 TCP 队列/字节时序状态机实时识别 `uploading`（上下文上传）、`thinking`（模型长思考）、`streaming`（SSE 推流）与 `idle_completed`（已完成闲置）：
  - **活跃保护**：在大包上传或流式生成期间严禁强杀连接或触发破坏性重载；
  - **闲置回收**：自动清理已完成响应并静默老化的 HTTP/2 连接，防止下一轮对话复用半衰期连接撞上 50s~94s 超时断流。
- **多轮实测重排（`ai-rebalance`）**
  - 每 30 分钟执行多轮并发延迟探测，结合历史失败惩罚权重与 Gemini 15.5s SSE 保持验毒（Hold Probe），动态重排候选优先级。

---

## 快速开始

### 方式一：Windows + Clash Verge (Rev)（一键旁路模式）

在已安装 Python（`>= 3.11`）和 Clash Verge (Rev) 的 Windows 机器上：

1. **双击运行仓库根目录的 [`setup-windows-verge.bat`](setup-windows-verge.bat)**
   脚本将自动完成以下全部操作：
   - 安装 `cproxy` Python 包；
   - 自动发现 Clash Verge 配置目录（`%APPDATA%\io.github.clash-verge-rev.clash-verge-rev`），读取控制器端口与密钥；
   - 将 AI 策略拓扑脚本直接写入 Clash Verge 的 `profiles\Script.js` 并在 `profiles.yaml` 中自动挂载启用；
   - 注册 Windows 登录无窗口后台自启任务（`cproxy-verge-daemon`）并立即拉起守护进程。
2. 打开 Clash Verge 点击一次 **「订阅 (Profiles) → 刷新」** 即可生效。
3. 在终端随时运行 **`cproxy verge`**（等价于 `cproxy verge status`），核验控制器、策略拓扑、分流规则、守护心跳、AI 探测健康与近期切流情况；持续探测异常或切流风暴会显示为降级。

> 如需卸载后台自启任务，在 PowerShell 中运行：`.\setup-windows-verge.bat -Uninstall`

---

### 方式二：Linux 独立部署（托管模式）

```bash
# 1. 安装 cproxy 与 Mihomo 地理数据
CPROXY_EDITABLE=0 ./scripts/install.sh

# 2. 使用机场订阅地址一键初始化并启动
cproxy bootstrap --subscription-url 'https://your-airport.example.com/sub'

# 3. 安装并启用用户级 systemd 守护服务与定时调度器
./systemd-user/install-systemd-user.sh
```

查看运行状态与 AI 出口：

```bash
cproxy status
cproxy ai-status
```

---

## 常用命令速查

| 分类 | 命令 | 说明 |
| :--- | :--- | :--- |
| **状态与切换** | `cproxy status` | 查看代理运行状态、实时资源与按进程流量归因 |
| | `cproxy ai-status` | 查看 `AI-MANUAL` / `AI-GEMINI` 当前出口、备用组与连通性 |
| | `cproxy switch` | 交互式选择代理组与出口节点（也可传参 `cproxy switch <组> <节点>`） |
| | `cproxy tui` | 启动终端可视化交互界面（TUI） |
| **AI 调度控制** | `cproxy ai-use [codex\|gemini\|claude]` | 立即多轮探测并切换到最稳定的 AI 出口 |
| | `cproxy ai-watch` | 执行一次 AI 出口语义健康巡检与闲置连接回收 |
| | `cproxy ai-rebalance` | 多轮探测全部候选节点，按稳定度重排优先级 |
| | `cproxy ban <关键词> --hours 24` | 临时或永久屏蔽故障节点 |
| | `cproxy rule add <域名/进程> <策略>` | 免改 YAML 添加自定义置顶分流规则 |
| **订阅与维护** | `cproxy refresh` | 更新订阅、重渲染配置、无感热重载并探测分组 |
| | `cproxy snapshots` / `cproxy rollback` | 查看历史配置快照 / 一键回滚到上一份可用配置 |
| | `cproxy tun on --apply` | 开启 Linux TUN 透明代理并热重载生效 |
| **Clash Verge** | `cproxy verge` (`status`) | 查看旁路控制面、AI 出口、守护健康与近期切流状态 |
| | `cproxy verge install` | 自动对接 Clash Verge 并注入全局扩展脚本 |
| | `cproxy verge daemon` | 启动 Clash Verge 旁路常驻调度守护进程 |
| **诊断与排障** | `cproxy why <域名或URL>` | 追查目标域名/IP 命中的分流规则与实际出口链路 |
| | `cproxy doctor` | 检查本地配置、内核与控制器状态并给出修复建议 |
| | `cproxy traffic` | 查看按节点、规则、域名或进程统计的流量报表 |

> 运行 `cproxy --help` 或 `cproxy <命令> --help` 查看完整参数；支持通过 `--json` 输出结构化 JSON，或通过 `--raw` 输出脚本友好的纯文本。

---

## 配置文件与目录说明

| 平台 | 配置目录 (`config.yaml`) | 数据目录 (`runtime.yaml` / GeoIP) | 状态与日志目录 (`state`) |
| :--- | :--- | :--- | :--- |
| **Linux / macOS** | `~/.config/cproxy/` | `~/.local/share/cproxy/` | `~/.local/state/cproxy/` |
| **Windows** | `%APPDATA%\cproxy\` | `%LOCALAPPDATA%\share\cproxy\` | `%LOCALAPPDATA%\state\cproxy\` |

可通过环境变量 `CPROXY_CONFIG_DIR`、`CPROXY_DATA_DIR`、`CPROXY_STATE_DIR` 自定义目录路径。

---

## 进阶文档

- **完整使用指南**：[USAGE.md](USAGE.md)
- **故障排查手册**：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- **项目演进与发布状态**：[STATUS.md](STATUS.md)
