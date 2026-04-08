# Clash Proxy 故障排查

这份文档只覆盖当前项目最常见的 4 类问题：

- AI 流量没有走代理
- 节点失效或自动切换异常
- `systemd` 服务/定时器状态异常
- 命令级代理没有生效

## 1. 先做最小体检

优先执行：

```bash
./proxy.sh status
./proxy.sh ai-status
systemctl status clash-proxy.service --no-pager -l
systemctl status clash-proxy-refresh.timer --no-pager -l
```

你应该重点看：

- `status` 是否显示运行配置是 `runtime.yaml`
- `ai-status` 是否显示 `AI-MANUAL -> AI-AUTO -> AI-US/AI-SG`
- `clash-proxy.service` 是否是 `active (running)`
- `clash-proxy-refresh.timer` 是否是 `active (waiting)`

## 2. AI 流量没有走代理

### 现象

- `chatgpt.com`
- `openai.com`
- `anthropic.com`
- `gemini.google.com`

这些域名访问失败，或者日志看起来没有走 `AI-MANUAL`

### 排查命令

```bash
./proxy.sh ai-status
./proxy.sh current "AI-MANUAL"
./proxy.sh test-group "AI-AUTO"
curl -x "http://127.0.0.1:7890" -I -s --connect-timeout 8 "https://chatgpt.com" >/dev/null
tail -n 20 /root/clash_proxy/clash.log
```

### 期望结果

- `AI-MANUAL` 默认是 `AI-AUTO`
- `AI-AUTO` 应该指向 `AI-US` 或 `AI-SG`
- `test-group "AI-AUTO"` 应该能测出延迟
- 日志里应出现类似：

```text
match DomainSuffix(chatgpt.com) using AI-MANUAL[...]
```

### 常见原因

1. 你改了 `config.yaml`，但没重新生成运行配置  
   处理：

   ```bash
   ./proxy.sh render
   ./proxy.sh restart
   ```

   如果本地 `config.yaml` 丢失，再执行：

   ```bash
   cp ./config.example.yaml ./config.yaml
   ```

2. `AI-MANUAL` 被你手动切到了不想要的组  
   处理：

   ```bash
   ./proxy.sh switch "AI-MANUAL" "AI-AUTO"
   ```

3. 美国/新加坡节点都不可用  
   处理：

   ```bash
   ./proxy.sh test-group "AI-US"
   ./proxy.sh test-group "AI-SG"
   ```

4. 日志里命中的不是 `AI-MANUAL`  
   处理：

   ```bash
   ./proxy.sh render
   ./proxy.sh restart
   rg -n "openai.com|chatgpt.com|anthropic.com|claude.ai|gemini.google.com|RULE-SET,ChinaMax|GEOIP,CN|MATCH,SSRDOG" /root/clash_proxy/runtime.yaml
   ```

## 3. 节点失效或自动切换异常

### 排查命令

```bash
./proxy.sh ai-status
./proxy.sh test-group "AI-AUTO"
./proxy.sh test-group "AI-US"
./proxy.sh test-group "AI-SG"
./proxy.sh list-nodes "AI-MANUAL"
```

### 判断逻辑

- `AI-US` 慢或失败，但 `AI-SG` 正常：应切到新加坡链路
- 两边都失败：问题不是切换策略，而是节点池本身
- `AI-MANUAL` 如果不是 `AI-AUTO`，说明你处于手动模式

### 恢复到自动模式

```bash
./proxy.sh switch "AI-MANUAL" "AI-AUTO"
```

## 4. 大陆流量不该走代理却走了

### 排查命令

```bash
rg -n "RULE-SET,ChinaMax|GEOIP,CN|MATCH,SSRDOG" /root/clash_proxy/runtime.yaml
tail -n 30 /root/clash_proxy/clash.log
```

### 期望结果

`runtime.yaml` 里应存在：

```text
RULE-SET,ChinaMax,DIRECT
GEOIP,CN,DIRECT,no-resolve
MATCH,SSRDOG
```

并且顺序应满足：

- `ChinaMax` 在 `MATCH` 前
- `GEOIP,CN` 在 `MATCH` 前

### 修复

```bash
./proxy.sh render
./proxy.sh restart
```

## 5. `systemd` 服务异常

### 排查命令

```bash
systemctl status clash-proxy.service --no-pager -l
systemctl show clash-proxy.service -p UnitFileState -p ActiveState -p SubState
journalctl -u clash-proxy.service -n 100 --no-pager
```

### 关注点

- `UnitFileState=enabled`
- `ActiveState=active`
- 主进程命令里应包含：

```text
/usr/local/bin/mihomo -f /root/clash_proxy/runtime.yaml -d /root/clash_proxy
```

### 常见处理

```bash
./proxy.sh render
systemctl restart clash-proxy.service
```

如果 unit 文件被你手工改乱了：

```bash
sudo /root/clash_proxy/systemd/install-systemd.sh
```

## 6. 定时器不执行

### 排查命令

```bash
systemctl status clash-proxy-refresh.timer --no-pager -l
systemctl list-timers clash-proxy-refresh.timer --no-pager
systemctl status clash-proxy-refresh.service --no-pager -l
journalctl -u clash-proxy-refresh.service -n 100 --no-pager
```

### 期望结果

- timer 是 `active (waiting)`
- `list-timers` 能看到下一次触发时间
- refresh service 通常是 `inactive (dead)`，但最近一次应 `Succeeded`

### 修复

```bash
sudo /root/clash_proxy/systemd/install-systemd.sh
systemctl restart clash-proxy-refresh.timer
```

## 7. `with-proxy` 没生效

### 排查命令

```bash
./proxy.sh proxy-env
./proxy.sh with-proxy env | rg "^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)="
```

### 期望结果

至少应看到：

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ALL_PROXY=socks5h://127.0.0.1:7890
NO_PROXY=127.0.0.1,localhost
```

### 常见原因

1. 命令本身不认环境代理变量  
   处理：改用该命令自身的代理参数，或者包一层 `curl/git` 之类确认代理是否真的可用。

2. 代理本地端口没起来  
   处理：

   ```bash
   ./proxy.sh status
   systemctl status clash-proxy.service --no-pager -l
   ```

## 8. 升级后的推荐检查

每次你更新项目后，建议按这个顺序做：

```bash
./tests/proxy_env_test.sh
./tests/systemd_proxy_examples_test.sh
./tests/systemd_helper_scripts_test.sh
./tests/review_fixes_test.sh
./proxy.sh render
./proxy.sh ai-status
systemctl status clash-proxy.service --no-pager -l
systemctl status clash-proxy-refresh.timer --no-pager -l
```
