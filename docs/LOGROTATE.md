# 日志轮转配置

## 概述

cproxy 自动配置日志轮转功能，防止日志文件无限增长占用磁盘空间。

## 配置详情

- **日志大小限制**: 10MB
- **保留历史**: 7 个轮转文件
- **检查频率**: 每 6 小时
- **压缩**: 自动压缩旧日志
- **轮转方式**: 使用 `copytruncate`，兼容当前通过 `stdout/stderr` 重定向写日志的启动方式

## 安装时自动配置

使用 `./scripts/install.sh` 安装时，会自动配置：

- **配置文件**: `/etc/logrotate.d/cproxy`
- **日志路径**: `$HOME/.local/state/cproxy/cproxy.log`
- **定时任务**: 自动添加到 crontab

> 旧版 `proxy.sh` 部署（`/etc/logrotate.d/clash_proxy` 与 `$HOME/.local/state/clash_proxy/clash.log`）
> 已于 2026-10-17 随退役阶段 3 一并移除，本节不再覆盖。

## 手动配置

如果自动配置失败，可以手动配置：

```bash
# 创建 logrotate 配置
sudo tee /etc/logrotate.d/cproxy > /dev/null << 'EOF'
/home/your-user/.local/state/cproxy/cproxy.log {
    daily
    size 10M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

# 添加定时任务
(crontab -l 2>/dev/null | grep -v "logrotate.*cproxy"; echo "0 */6 * * * /usr/sbin/logrotate /etc/logrotate.d/cproxy >/dev/null 2>&1") | crontab -
```

## 验证配置

### 检查配置文件

```bash
cat /etc/logrotate.d/cproxy
```

### 检查定时任务

```bash
crontab -l | grep logrotate
```

### 手动测试轮转

```bash
sudo logrotate -f /etc/logrotate.d/cproxy
```

### 查看日志文件

```bash
ls -lh ~/.local/state/cproxy/cproxy.log*
```

## 故障排除

### 权限问题

如果遇到权限错误，确保：

1. `/etc/logrotate.d` 目录可写
2. 有权限修改 crontab

### 模板文件缺失

如果提示模板文件缺失，确保仓库中存在：

- `logrotate.conf.template`

### 为什么用 copytruncate 而不是发信号

`cproxy` 启动 `mihomo` 时直接把追加模式打开的日志文件描述符赋给其 `stdout/stderr`，且 `mihomo`
本身不支持通过信号重新打开日志文件，所以采用 `copytruncate` 原地截断。

## 自定义配置

如需修改轮转策略，编辑对应的配置文件：

- **大小限制**: 修改 `size 10M` 为所需大小
- **保留数量**: 修改 `rotate 7` 为所需数量
- **检查频率**: 修改 crontab 中的时间表达式
