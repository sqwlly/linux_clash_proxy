# Clash Proxy 速查

详细故障排查见 [TROUBLESHOOTING.md](/root/clash_proxy/TROUBLESHOOTING.md)。
代码审查结论见 [CODE_REVIEW.md](/root/clash_proxy/CODE_REVIEW.md)。

## 最常用

```bash
./proxy.sh status
./proxy.sh ai-status
./proxy.sh list-groups
./proxy.sh with-proxy curl https://chatgpt.com
```

## 首次初始化

```bash
cp ./config.example.yaml ./config.yaml
```

## 更新配置后

```bash
./proxy.sh render
./proxy.sh restart
```

说明：

- `config.example.yaml` 是脱敏模板
- `config.yaml` 是本地原始配置，不入库
- `runtime.yaml` 是渲染后的运行配置
- 不要手改 `runtime.yaml`
- 大陆流量会通过 `ChinaMax + GEOIP,CN` 双重规则优先直连

## AI 路由

默认链路：

```text
AI-MANUAL -> AI-AUTO -> AI-US / AI-SG
```

含义：

- `AI-US`：美国节点池内自动切换
- `AI-SG`：新加坡节点池内自动切换
- `AI-AUTO`：美国优先，新加坡兜底
- `AI-MANUAL`：人工切换入口

## 查看和切换

查看 AI 当前状态：

```bash
./proxy.sh ai-status
./proxy.sh current "AI-MANUAL"
```

查看候选项：

```bash
./proxy.sh list-nodes "AI-MANUAL"
./proxy.sh list-nodes "AI-US"
./proxy.sh list-nodes "AI-SG"
```

切到新加坡：

```bash
./proxy.sh switch "AI-MANUAL" "AI-SG"
```

切回自动：

```bash
./proxy.sh switch "AI-MANUAL" "AI-AUTO"
```

## 命令级代理

输出代理环境：

```bash
./proxy.sh proxy-env
```

仅让一条命令走代理：

```bash
./proxy.sh with-proxy curl https://chatgpt.com
./proxy.sh with-proxy git clone https://github.com/owner/repo.git
```

打开临时代理 shell：

```bash
./proxy.sh proxy-shell
```

## 健康检查

```bash
./proxy.sh test
./proxy.sh test-group "AI-AUTO"
./proxy.sh test-group "AI-US"
./proxy.sh test-group "AI-SG"
```

## 关键结论

- AI 规则会固定放在 `ChinaMax` 前面
- 大陆流量还会额外命中 `GEOIP,CN,DIRECT,no-resolve`
- 你手动调整 `config.yaml` 里的 `ChinaMax` 顺序不会影响最终 AI 路由
- 但改完原始配置后，仍然要重新执行：

```bash
./proxy.sh render
./proxy.sh restart
```

## systemd 示例

项目内示例文件：

- `systemd/clash-proxy.service`
- `systemd/clash-proxy-refresh.service`
- `systemd/clash-proxy-refresh.timer`
- `systemd/clash-proxy-command.env.example`
- `systemd/example-proxied-service.override.conf`
- `systemd/install-systemd.sh`
- `systemd/generate-proxied-service.sh`

安装后建议：

```bash
sudo systemctl enable --now clash-proxy.service
sudo systemctl enable --now clash-proxy-refresh.timer
```

或直接执行：

```bash
sudo /root/clash_proxy/systemd/install-systemd.sh
```

仅让某个现有服务走代理：

```bash
sudo install -m 644 /root/clash_proxy/systemd/clash-proxy-command.env.example /etc/default/clash-proxy-command
sudo mkdir -p /etc/systemd/system/<your-service>.service.d
sudo install -m 644 /root/clash_proxy/systemd/example-proxied-service.override.conf /etc/systemd/system/<your-service>.service.d/proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart <your-service>
```

也可以先生成指引：

```bash
/root/clash_proxy/systemd/generate-proxied-service.sh <your-service>
```

说明：

- `/etc/default/clash-proxy-command.example` 是仓库同步模板
- `/etc/default/clash-proxy-command` 是系统实际生效文件
