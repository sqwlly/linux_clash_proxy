# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-07-18

企业 TUI GA readiness 验收齐备后的首个 GA 版本。

### 主要能力

- `cproxy` 用户级 Mihomo CLI：配置渲染、进程生命周期、状态查询、AI 路由查看与切换、节点/延迟诊断、命令级代理注入
- `cproxy-tui`：策略组、连接、Provider、订阅、日志全键盘操作的 Textual TUI
- 企业安全：controller TLS、secret provider（文件 / keyring / systemd credential）、结构化审计与支持包脱敏、`security-check`
- GA 设施：`scripts/build-ga-artifacts.sh` 源码归档 + SHA256 + provenance，`scripts/verify-ga-artifacts.sh` 校验，供应链 CI

### 本版本变更

- `cproxy start` 新增外来实例预检：controller / mixed-port 已被监听时给出明确报错和迁移提示，替代模糊的 "process exited immediately"
- 新增 `cproxy snapshots` / `cproxy rollback`：render 与订阅更新覆盖配置前自动留快照（各保留 10 份），一键回滚并在运行中自动重启
- 新增 `cproxy refresh`：订阅更新（本地环境键保留、失败不阻断）→ render → 重启 → select 组延迟探测，当前节点失效自动切到最低延迟节点
- `scripts/install-mihomo.sh`：mihomo 二进制版本固定 + sha256 校验安装，替代手工放置 `mihomo.gz`
- `Country.mmdb` 移出 git；安装时按"已有用户文件 → 仓库遗留副本 → meta-rules-dat 下载 → 手动提示"回退
- `scripts/install.sh` 支持 `CPROXY_EDITABLE=0` 非 editable 安装（生产约定，见 STATUS.md）
- 测试不再硬编码 `/root/clash_proxy`，CI 移除 sudo 符号链接步骤
- `proxy.sh` 工作流冻结新功能，退役条件见 `docs/plans/2026-07-18-proxy-sh-retirement.md`

[1.0.0]: https://github.com/sqwlly/linux_clash_proxy/releases/tag/v1.0.0
