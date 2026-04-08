# Clash Proxy Code Review 记录

日期：2026-04-09

## 结论

当前项目已达到可用状态，核心链路完整：

- `AI` 专用路由已独立于普通默认代理
- 大陆流量具备 `ChinaMax + GEOIP,CN` 双重直连兜底
- `proxy.sh` 已支持无 GUI 节点控制与命令级代理
- `systemd` 主服务与定时刷新已安装并启用
- 文档、排查手册、基础回归测试已齐备

## 本轮已修复的问题

### 1. systemd 接管旧实例不彻底

已修复：

- [clash-proxy.service](/root/clash_proxy/systemd/clash-proxy.service) 的 `ExecStart` 已改为调用 `proxy.sh restart`
- 这样在已有实例存在时，也会切到新的 `runtime.yaml`

### 2. 进程识别过宽

已修复：

- [proxy.sh](/root/clash_proxy/proxy.sh) 现在优先使用 `PID_FILE`
- 配置候选收紧为优先 `runtime.yaml`，只在不存在时退回 `config.yaml`

### 3. 代理服务 drop-in 生成脚本不兼容 `.service` 输入

已修复：

- [generate-proxied-service.sh](/root/clash_proxy/systemd/generate-proxied-service.sh) 现已兼容：
  - `codex`
  - `codex.service`

### 4. systemd 环境模板升级不可见

已修复：

- [install-systemd.sh](/root/clash_proxy/systemd/install-systemd.sh) 现在始终更新：
  - `/etc/default/clash-proxy-command.example`
- 仅在正式文件不存在时，才初始化：
  - `/etc/default/clash-proxy-command`

## 当前运行状态

### AI 路由

当前检查结果：

- `AI-MANUAL = AI-AUTO`
- `AI-AUTO = AI-US`
- `AI-US`、`AI-SG` 当前均可用

### systemd

当前检查结果：

- `clash-proxy.service`
  - `UnitFileState=enabled`
  - `ActiveState=active`
  - `SubState=running`
- `clash-proxy-refresh.timer`
  - `UnitFileState=enabled`
  - `ActiveState=active`
  - `SubState=waiting`

## 当前测试基线

已通过：

- [proxy_env_test.sh](/root/clash_proxy/tests/proxy_env_test.sh)
- [systemd_proxy_examples_test.sh](/root/clash_proxy/tests/systemd_proxy_examples_test.sh)
- [systemd_helper_scripts_test.sh](/root/clash_proxy/tests/systemd_helper_scripts_test.sh)
- [review_fixes_test.sh](/root/clash_proxy/tests/review_fixes_test.sh)
- [render_rules_test.sh](/root/clash_proxy/tests/render_rules_test.sh)

## 剩余风险

### 1. shell 脚本与内嵌 Python 仍偏重

当前实现大量依赖：

- shell 进程控制
- `python3 + PyYAML`
- Mihomo controller API

这在当前规模下是合理的，但后续如果规则种类继续扩张，建议把渲染逻辑拆到独立 Python 脚本，降低 [proxy.sh](/root/clash_proxy/proxy.sh) 的复杂度。

### 2. refresh timer 仍是“定时检查”而非“事件驱动”

当前定时器每 15 分钟触发一次，已经足够实用，但它不是订阅变更事件。  
如果未来你把 `config.yaml` 更新频率提高，可能会希望更快同步；那时再考虑更细粒度触发即可。

## 建议的例行检查

建议在你更新配置、升级脚本、或替换节点后，执行一次：

```bash
./tests/proxy_env_test.sh
./tests/systemd_proxy_examples_test.sh
./tests/systemd_helper_scripts_test.sh
./tests/review_fixes_test.sh
./tests/render_rules_test.sh
./proxy.sh ai-status
systemctl status clash-proxy.service --no-pager -l
systemctl status clash-proxy-refresh.timer --no-pager -l
```
