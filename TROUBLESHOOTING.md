# cproxy 故障排查

## 1. 先做最小体检

优先执行：

```bash
cproxy status
cproxy ai-status
cproxy test-group "AI-AUTO"
cproxy logs --lines 50
```

你应该重点看：

- `status` 是否显示 `运行配置状态: 已就绪`
- `status` 是否显示 `状态: 运行中`
- `status` 是否按 `摘要 / 资源 / 路径` 三段展示
- `status` 是否显示 `AI 路由模式` 与 `AI 当前出口`
- `ai-status` 是否显示 `AI 探测: 正常 / 部分异常 / 失败`
- `ai-status` 是否按 `摘要 / 连通性 / 链路 / 备用 / 分组` 展示
- `ai-status` 是否能看出 `AI-MANUAL -> AI-AUTO -> AI-US/AI-SG` 的当前链路
- `test-group "AI-AUTO"` 是否能测出至少一个可用区域

## 2. 命令不存在或安装后找不到 `cproxy`

排查：

```bash
which cproxy
pipx list | rg cproxy
```

如果你是用仓库脚本安装：

```bash
./scripts/install.sh
```

如果 `pipx` 已安装但命令仍不可见，通常是 PATH 没更新。处理：

```bash
pipx ensurepath
exec "$SHELL" -l
```

如果安装脚本提示未检测到 `country.mmdb`，这不是安装失败。默认手动放置路径是：

- `~/.local/share/cproxy/country.mmdb`

## 3. `start` 失败或状态一直是未运行

排查：

```bash
cproxy render
cproxy start
cproxy status --raw
```

重点看：

- `~/.local/share/cproxy/runtime.yaml` 是否存在
- `program-path` 是否在 `config.yaml` 里配置正确
- `PID` 是否出现

如果你本机 `mihomo` 不在 PATH，可在 `~/.config/cproxy/config.yaml` 里显式设置：

```yaml
program-path: /usr/local/bin/mihomo
```

如果 API 处于坏链路、但你不想让查询命令等待过久，可显式缩短：

```yaml
api-timeout: 2
```

如果要看启动失败前后的上下文，直接查看：

```bash
cproxy logs --lines 200
```

如果你看到 `GEOIP,CN,DIRECT,no-resolve` 已经注入，但 `country.mmdb` 缺失，先执行：

```bash
cproxy test
```

如果此前遗留了 stale pidfile，新版本不会直接复用它，也不会误杀同 PID 的无关进程。

## 4. AI 流量没有走代理

排查：

```bash
cproxy ai-status
cproxy current "AI-MANUAL"
cproxy test-group "AI-AUTO"
```

期望结果：

- `AI-MANUAL` 默认指向 `AI-AUTO`
- `AI-AUTO` 当前指向 `AI-US` 或 `AI-SG`
- `ai-status` 里的 `ChatGPT Web` 与 `OpenAI API` 至少应有一项正常
- `test-group "AI-AUTO"` 能测出延迟

常见原因：

1. 改了 `config.yaml` 后没有重新渲染  
   处理：

   ```bash
   cproxy render
   cproxy restart
   ```

2. 你把 `AI-MANUAL` 手动切到了固定区域  
   处理：

   ```bash
   cproxy switch "AI-MANUAL" "AI-AUTO"
   ```

3. 美国和新加坡区域都不可用  
   处理：

   ```bash
   cproxy test-group "AI-US"
   cproxy test-group "AI-SG"
   ```

补充边界：

- `current/list-groups/list-nodes` 在 API 不可达时仍可用于查看静态 runtime 状态
- `ai-status` 仍然必须依赖 API，因为它需要实时 `alive/delay`

## 5. `with-proxy` 没生效

排查：

```bash
cproxy proxy-env
cproxy with-proxy env | rg "^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)="
```

期望结果至少包含：

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ALL_PROXY=socks5h://127.0.0.1:7890
NO_PROXY=127.0.0.1,localhost
```

如果环境变量存在但命令仍不走代理，通常是命令本身忽略标准代理环境变量。处理方式：

- 改用该命令自带的代理参数
- 先用 `curl` 或 `git` 验证当前代理本身是否可用

## 6. `proxy-shell` 没按预期工作

默认用法：

```bash
cproxy proxy-shell
```

如果你要给 shell 传自己的参数，并且参数以 `-` 开头，使用：

```bash
cproxy proxy-shell -- -c 'env | rg PROXY'
```

这是 `argparse` 的标准分隔方式，不是 `cproxy` 特有约束。

## 7. `test` 检查失败

排查：

```bash
cproxy test
cproxy status
```

`test` 依赖两件事：

- 本地代理进程已经启动
- `mixed-port` 配置正确

此外还依赖：

- 当前 PID 与 ownership 元数据匹配
- 默认 GeoIP 数据文件 `~/.local/share/cproxy/country.mmdb` 存在

如果 stderr 提示“不属于 cproxy 管理的进程”，说明你当前的 pidfile 已陈旧，或目标进程不是由 `cproxy start` 拉起。

如果你需要自定义探测地址，可在 `~/.config/cproxy/config.yaml` 里配置：

```yaml
connectivity-test-urls:
  - https://www.google.com
  - https://github.com
ip-check-urls:
  - https://api.ip.sb/ip
  - https://ifconfig.me/ip
connectivity-timeout: 5
test-timeout: 5000
```

如果输出里出现 `失败  GeoIP 数据`，优先检查：

- `~/.local/share/cproxy/country.mmdb` 是否存在
- 当前机器是否处于无代理或受限网络环境，导致 Mihomo 无法自动获取该文件

## 8. 从旧目录迁移后配置不见了

迁移命令只会复制旧目录里的 `config.yaml`：

```bash
cproxy migrate-from-legacy /root/clash_proxy
```

它不会迁移这些内容：

- `runtime.yaml`
- 旧日志
- PID 文件
- 临时文件

迁移后正确流程是：

```bash
cproxy render
cproxy start
cproxy status
```

## 9. 运行配置与原始配置不一致

这是预期行为。`runtime.yaml` 不是手工维护文件，而是 `render` 的产物。

每次你修改 `~/.config/cproxy/config.yaml` 后，都应该执行：

```bash
cproxy render
cproxy restart
```

如果你要验证 AI 规则是否已经注入，看：

- `~/.local/share/cproxy/runtime.yaml`

重点检查：

- `AI-MANUAL`
- `AI-AUTO`
- `AI-US`
- `AI-SG`
- `DOMAIN-SUFFIX,openai.com,AI-MANUAL`
- `GEOIP,CN,DIRECT,no-resolve`

## 10. 用户级 systemd 没启动

安装：

```bash
./systemd-user/install-systemd-user.sh
```

排查：

```bash
systemctl --user status cproxy.service --no-pager -l
systemctl --user status cproxy-refresh.timer --no-pager -l
journalctl --user -u cproxy.service -n 100 --no-pager
```

关注点：

- `cproxy.service` 应为 `active`
- `cproxy-refresh.timer` 应为 `active (waiting)`
- 如果命令找不到，先确认 `~/.local/bin` 已在 PATH 中
