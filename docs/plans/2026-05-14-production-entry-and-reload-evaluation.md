# 生产入口与 Reload 评估

日期：2026-05-14

范围：仅做文档补充和 reload 评估。本次没有重启服务、访问生产 API、修改代码、修改测试或修改 systemd 文件。

## 结论

当前生产运行态是 root 级 `clash-proxy.service` 路径，不是用户级 `cproxy` XDG 路径。

Mihomo 热 reload 目前不适合作为当前 restart 流程的替代方案。仓库现有安全边界围绕 restart 建立了校验、探活和回滚流程；热 reload 在本仓库内没有实现入口、回滚路径或回归验证。

## 证据

Direct Evidence:

- `systemctl cat clash-proxy.service --no-pager` 显示 live unit 来自 `/etc/systemd/system/clash-proxy.service`。
- live unit 使用 `WorkingDirectory=/root/clash_proxy`。
- live unit 使用 `ExecStartPre=/root/clash_proxy/proxy.sh render`。
- live unit 使用 `ExecStart=/root/clash_proxy/proxy.sh restart`。
- live unit 使用 `ExecStop=/root/clash_proxy/proxy.sh stop`。
- live unit 使用 `ExecReload=/root/clash_proxy/proxy.sh restart`。
- live unit 使用 `PIDFile=/root/clash_proxy/mihomo.pid`。
- `systemctl is-active clash-proxy.service` 返回 `active`。
- `systemctl --user is-active cproxy.service` 返回 `inactive`。
- `ps -p "$(cat mihomo.pid)" -o "pid=,ppid=,args="` 返回 `/usr/local/bin/mihomo -f /root/clash_proxy/runtime.yaml -d /root/clash_proxy`。
- `mihomo -t -f runtime.yaml -d /root/clash_proxy` 返回当前运行配置校验成功。
- `/usr/local/bin/mihomo` 的 `mihomo -h` 输出包含 `-f`、`-d`、`-t`、`-v`、controller override 和 secret override，没有 CLI reload 参数。
- `mihomo -v` 返回 `Mihomo Meta v1.19.21 linux amd64`。
- `systemd-user/cproxy.service` 使用 `%h/.local/bin/cproxy`、`%h/.local/state/cproxy/cproxy.pid` 和用户级路径。
- `src/cproxy/config.py` 定义默认路径为 `~/.config/cproxy`、`~/.local/share/cproxy` 和 `~/.local/state/cproxy`。
- `proxy.sh restart` 调用 `stop`，sleep 后再调用 `start`。
- `systemd/clash-proxy-refresh.sh` 会备份 `runtime.yaml`、render、比较 hash、用 `mihomo -t` 校验、restart `clash-proxy.service`、探活 API，并在失败时恢复上一份 runtime。

Inference:

- 当 `clash-proxy.service` 为 active 且 `cproxy.service` 为 inactive 时，`cproxy status`、`cproxy restart` 和用户级 `cproxy.service` 不能代表当前生产服务。
- 当前生产安全模型建立在配置校验后的完整进程 restart 上。
- 如果直接用热 reload 替代 restart，会绕开现有进程级恢复假设，除非新增 reload 专属回滚和一致性校验。

## 安全更新流程

针对当前 root 级生产路径：

1. 操作前先确认 active 入口：

   ```bash
   systemctl cat clash-proxy.service --no-pager
   systemctl is-active clash-proxy.service
   systemctl --user is-active cproxy.service
   ps -p "$(cat /root/clash_proxy/mihomo.pid)" -o "pid=,ppid=,args="
   ```

2. 把 `/root/clash_proxy/config.yaml` 视为源输入，把 `/root/clash_proxy/runtime.yaml` 视为生成产物。

3. 校验前先 render：

   ```bash
   /root/clash_proxy/proxy.sh render
   ```

4. 任何服务重启前，先校验生成后的运行配置：

   ```bash
   mihomo -t -f /root/clash_proxy/runtime.yaml -d /root/clash_proxy
   ```

5. 只有校验通过且确实需要生效时，才进入现有受控 restart 边界。

6. 优先使用现有 refresh 脚本或等价手工流程，保留这些性质：

   - 备份当前 `runtime.yaml`
   - render
   - 用 `mihomo -t` 校验
   - 校验通过后才 restart
   - restart 后探活 `/version` 和关键 AI proxy groups
   - 刷新后不健康时恢复上一份 runtime

`update_config.sh` 已收敛为安全更新入口：默认 `--dry-run` 只校验候选 YAML，不写源配置；
`--apply` 会写入候选配置并调用 refresh 脚本，refresh 失败时恢复上一份源配置。
AI groups/rules 不在 `update_config.sh` 中注入，统一由 `proxy.sh render` 生成。
root 级 `clash-proxy-refresh.path` 会监听 `/root/clash_proxy/config.yaml` 变化并触发同一条 refresh 链路，timer 保留为周期性兜底。

## 热 Reload 评估

Verdict: 不能作为生产 restart 的替代方案。

Direct Evidence:

- root 级 systemd unit 当前把 reload 映射为 restart：`ExecReload=/root/clash_proxy/proxy.sh restart`。
- 用户级 systemd unit 也把 reload 映射为 restart：`ExecReload=%h/.local/bin/cproxy restart`。
- `proxy.sh` 暴露 `restart`，没有暴露 `reload` 命令。
- `src/cproxy/backend/process.py` 暴露 `restart`，没有 reload 操作。
- 仓库搜索未找到已实现的 `/configs` reload 路径、signal reload 路径或 reload 专用测试。
- `mihomo -h` 没有展示 CLI reload 参数。

Inference:

- Mihomo 某些版本可能支持通过 controller API 修改运行配置，但本仓库当前没有实现或验证该路径。
- 即使 controller API 支持 reload，本仓库仍需要证明哪些字段 reload-safe、失败如何检测、旧 runtime 状态如何恢复。

考虑热 reload 前至少需要补齐：

1. 基于当前安装版本确认官方支持的 reload 机制，不能依赖未文档化行为。
2. reload 前保留 `mihomo -t` dry-run 校验。
3. 定义 reload 失败和 reload 后探活失败的回滚行为。
4. 验证代理组、规则、DNS、监听端口、controller、geodata 和既有连接在 reload 后是否收敛。
5. 增加面向一次性 Mihomo 实例的非生产 smoke 或 focused test。

## 剩余未知

- Unknown: Mihomo Meta v1.19.21 是否有适合当前配置形态的受支持 controller API reload 端点。
- Unknown: 当前所有配置字段是否都能在不重启进程的情况下安全生效。
- Unknown: 热 reload 会保留、重置还是部分修改当前代理组选择和既有连接。
- Unknown: reload 失败后，进程会保留旧配置、部分应用新配置，还是进入不健康混合状态。
- Unknown: 下游服务恢复是否依赖当前 restart 语义。

## 可能出错的地方

1. Mihomo 可能存在 `mihomo -h` 不展示的受支持 controller reload 端点；本次评估按要求没有访问生产 controller API。
2. 用户级 `cproxy.service` 的状态可能在此快照之后变化；执行运维前应重新运行只读 `systemctl` 命令确认。
3. 如果 `/etc/systemd/system` 或生产文件曾在 checkout 外被手工修改，本地仓库文件可能不能完全代表部署内容。
