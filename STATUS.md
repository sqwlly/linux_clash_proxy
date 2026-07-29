# 项目状态

更新日期：2026-07-29

## 当前阶段

legacy proxy.sh → cproxy 功能迁移已基本完成，项目处于 **GA 收尾 + legacy 双轨维护** 阶段：

- `proxy.sh` 旧链路冻结新功能，只接受安全修复
- 新功能只进 `cproxy`
- 生产入口仍为 `clash-proxy.service`（proxy.sh 编排），cproxy 作为用户级 CLI 工具使用
- 退役条件与分阶段步骤见 [proxy.sh 退役计划](docs/plans/2026-07-18-proxy-sh-retirement.md)

## 最近里程碑

- 2026-07-29 probe 修复（双方都不稳定时允许切换）、端口冲突防护（cproxy start 检测生产服务、订阅脚本预检用户级 cproxy.service）
- 2026-07-25 cproxy 功能同步完成：Japan 组、probe history、guard/incident/ai-connections、进程管理；ruff/mypy 接入、cli.py 拆分、测试隔离、功能对账表填写
- 2026-07-24 生产入口每日订阅更新 systemd timer（04:00 + 30min 随机延迟）
- 2026-07-18 仓库卫生与发布化：`Country.mmdb` 移出 git（安装时改为下载回退）、根目录个人配置清理、proxy.sh 退役计划、`cproxy start` 外来实例预检、`scripts/install-mihomo.sh`（版本固定 + sha256 校验）、`CPROXY_EDITABLE=0` 生产安装约定、版本升到 1.0.0 并新增 `CHANGELOG.md`、测试解除 `/root/clash_proxy` 硬编码且 CI 去掉 sudo 步骤、新增 `cproxy snapshots`/`rollback`（自动快照回滚）与 `cproxy refresh`（订阅更新→render→重启→探测切换一条龙）
- 2026-07-14 render 自动创建区域组、清理 dns fallback-filter geosite
- 2026-06-29 render 自动注入 external-controller / secret 默认值

## 下一步（按优先级）

1. 复跑 `docs/enterprise-tui/acceptance.md` 全部验收命令
2. 进入退役计划阶段 2：生产入口切到 cproxy，开始 4 周观察期
3. 观察期内验证 cproxy refresh 替代 clash-proxy-subscription.sh 的可行性

## 发布

1. 更新 `pyproject.toml` 的 `version` 并在 `CHANGELOG.md` 记录变更
2. 提交后打 tag：`git tag -a vX.Y.Z -m "vX.Y.Z"`
3. 构建与校验 GA 产物：`scripts/build-ga-artifacts.sh` + `scripts/verify-ga-artifacts.sh`（版本号自动取 `git describe`，可用 `CPROXY_VERSION` 覆盖）
4. 生产环境按"生产/开发分离约定"重新安装

## 文档地图

- 安装与日常使用：[USAGE.md](USAGE.md)
- 故障排查：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- GA 验收：[docs/enterprise-tui/acceptance.md](docs/enterprise-tui/acceptance.md)
- 历史设计：[docs/plans/](docs/plans/)（日期快照，只用于追溯）

## 仓库卫生约定

- 个人订阅/候选配置含凭据，不要放仓库根目录；可放 `.tmp/`（已 gitignore）
- 根目录 `/*.yaml` 默认被 gitignore（`config.example.yaml` 除外）
- GeoIP/GeoSite 数据文件（`Country.mmdb`、`GeoSite.dat`）不入库
- 运行中的 legacy mihomo 以仓库根目录为 `-d` 工作目录，清理根目录文件前先确认运行依赖

## 生产/开发分离约定

`/root/clash_proxy` 同时是开发沙盒和生产运行目录，editable 安装会让未提交的
改动直接影响生产 `cproxy` 命令。约定如下：

- 生产安装一律使用干净的工作区 + `CPROXY_EDITABLE=0 ./scripts/install.sh`（非 editable）
- 日常开发实验在另一个 clone 或 `git worktree` 里进行，不在生产目录直接改
- 生产目录只通过 `git pull`（或 checkout 固定 tag）+ 重新安装来变更
- legacy mihomo 仍以 `/root/clash_proxy` 为 `-d` 工作目录，退役前该目录保持只读使用
