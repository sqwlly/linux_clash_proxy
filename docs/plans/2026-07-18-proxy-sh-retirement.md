# proxy.sh 工作流退役计划

日期：2026-07-18

范围：本文档只定义退役条件、对账清单和分阶段步骤。本阶段不删除任何文件、不改动 systemd 单元、不切换生产入口。

## 背景

仓库目前双轨并行：

- 旧链路（legacy）：`proxy.sh`、`update_config.sh`、root 级系统命令 `/usr/local/bin/clash-proxy` / `clash-proxy-update`（payload 在 `/usr/local/lib/clash-proxy`）、系统级 `systemd/clash-proxy.service`
- 新链路（cproxy）：`cproxy` CLI（pipx 用户级安装）、`cproxy-tui`、`systemd-user/` 用户级单元

README 已声明 cproxy 的目标是替代 `proxy.sh` 工作流，但没有明确的终点。双轨的代价是：每条修复要评估两个入口，`AGENTS.md` 必须维护"改 `proxy.sh` 同步 `/usr/local` 安装副本"的规则，安全面（系统级 root 服务）持续存在。

## 维护规则（即日起生效）

- `proxy.sh` 及其 helper 脚本只接受安全修复和 blocking bug 修复，不再添加新功能
- 新功能只进 `cproxy`
- 在阶段 3 完成前，`AGENTS.md` 的 "Installed Command Copy" 规则继续有效

## 退役条件（Gate）

全部满足才能进入阶段 3：

1. `docs/enterprise-tui/acceptance.md` 的全部验收命令通过
2. 功能对账表（见下）每一行都有结论：已有等价 / 不再需要 / 已移植
3. `cproxy` 作为唯一操作入口连续运行 4 周，无 P0/P1 事故
4. README / USAGE 已移除以 `proxy.sh` 为默认入口的描述

## 功能对账表

退役前必须逐项核对并填写结论。legacy 侧待核对项：

| legacy 能力 | 位置 | cproxy 等价 | 结论 |
|---|---|---|---|
| 启动/停止/重启/状态 | `proxy.sh` | `cproxy start/stop/restart/status`（`backend/process.py`） | 已有等价 |
| 无 GUI 节点切换 | `proxy.sh` | `cproxy switch` / TUI Proxies 页（`services/query.py`） | 已有等价 |
| 命令级代理注入 | `proxy.sh` | `cproxy proxy-env/with-proxy/proxy-shell`（`proxyenv.py`） | 已有等价 |
| 订阅更新 | `update_config.sh`、`clash-proxy-update` | `cproxy import-subscription/refresh`、TUI 订阅页（`services/refresh.py`） | 已有等价 |
| 稳定节点探测 | `probe_stable_node.py`（`/usr/local/lib`） | `cproxy probe-stable-node/shadow-probe/ai-use`（`services/probe.py`） | 已有等价 |
| AI 路由助手 | `ai-use`、`guard`、`ai-connections`、`incident` | `cproxy ai-use/guard/ai-connections/incident`（`services/ops.py`、`services/probe.py`） | 已有等价 |
| 系统级 systemd 服务 | `systemd/clash-proxy.service` | `systemd-user/cproxy.service`（用户级，`systemd-analyze verify` 通过） | 已有等价 |
| 定时刷新 | `systemd/clash-proxy-refresh.timer` | `systemd-user/cproxy-refresh.timer`（用户级 timer） | 已有等价 |
| 日志轮转 | `logrotate.conf.template`（legacy 段） | `scripts/install.sh` 的 cproxy 段（logrotate 配置） | 已有等价 |

核对方式：逐项在新链路上演练，把结论列改为"已有等价 / 不再需要 / 需移植（附 issue 或提交）"。

## 分阶段步骤

### 阶段 0：冻结 legacy（当前）

- 执行上面的维护规则
- 完成本计划文档和 `STATUS.md` 的状态同步

### 阶段 1：对账与补缺口

- 填写功能对账表
- 标记为"需移植"的项在 cproxy 实现并补测试
- 复跑 `docs/enterprise-tui/acceptance.md` 全部验收

### 阶段 2：切换生产入口

- 新部署一律使用 `cproxy` + `systemd-user/`
- 现有机器停用 `clash-proxy.service`（`systemctl disable --now`），改用 cproxy 链路接管同一 `runtime.yaml`
- 保留回滚路径：legacy 文件与 unit 仅停用不删除，回滚 = 重新 enable 旧 unit
- 开始 4 周观察期

### 阶段 3：退役

- `proxy.sh`、`update_config.sh` 入口打印 deprecation 警告，指向 cproxy 等价命令
- `scripts/install-system-commands.sh` 停止安装 `/usr/local/bin/clash-proxy*`
- 一个版本周期后删除 legacy 入口文件，更新 `AGENTS.md` 移除 "Installed Command Copy" 规则
- README / USAGE 以 cproxy 为唯一入口重写相关段落

## 回滚

阶段 2 之前任何时刻回滚成本为零（什么都没动）。阶段 2/3 的回滚路径：重新 enable 旧 systemd unit，旧文件在 git 历史中始终可用。
