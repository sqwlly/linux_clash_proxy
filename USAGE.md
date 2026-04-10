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

安装脚本会检查默认 GeoIP 数据文件：

- `~/.local/share/cproxy/country.mmdb`

如果缺失，会打印警告，但不会阻塞安装。

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

如果 `cproxy test` 提示缺少 `country.mmdb`，先把该文件放到：

- `~/.local/share/cproxy/country.mmdb`

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

- 默认仅在真实终端输出时启用
- 可通过 `FORCE_COLOR=1 cproxy status` 强制开启
- 脚本场景优先使用 `--raw`

适合脚本消费的命令：

```bash
cproxy status --raw
cproxy ai-status --raw
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL" --raw
cproxy test-group "AI-AUTO" --raw
```
