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

## 初始化

```bash
cproxy init
```

配置文件位置：

- `~/.config/cproxy/config.yaml`

## 首次启动

```bash
cproxy render
cproxy start
cproxy logs --lines 50
cproxy status
```

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

适合脚本消费的命令：

```bash
cproxy status --raw
cproxy ai-status --raw
cproxy list-groups --raw
cproxy list-nodes "AI-MANUAL" --raw
cproxy current "AI-MANUAL" --raw
cproxy test-group "AI-AUTO" --raw
```
