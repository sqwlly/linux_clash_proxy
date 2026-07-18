# 项目状态

更新日期：2026-07-18

## 当前阶段

企业 TUI GA readiness 已完成（测试与验收脚本齐备），项目处于 **GA 收尾 + legacy 双轨维护** 阶段：

- `proxy.sh` 旧链路冻结新功能，只接受安全修复
- 新功能只进 `cproxy`
- 退役条件与分阶段步骤见 [proxy.sh 退役计划](docs/plans/2026-07-18-proxy-sh-retirement.md)

## 最近里程碑

- 2026-07-18 仓库卫生与发布化：`Country.mmdb` 移出 git（安装时改为下载回退）、根目录个人配置清理、proxy.sh 退役计划、`cproxy start` 外来实例预检、`scripts/install-mihomo.sh`（版本固定 + sha256 校验）、`CPROXY_EDITABLE=0` 生产安装约定、版本升到 1.0.0 并新增 `CHANGELOG.md`、测试解除 `/root/clash_proxy` 硬编码且 CI 去掉 sudo 步骤、新增 `cproxy snapshots`/`rollback`（自动快照回滚）与 `cproxy refresh`（订阅更新→render→重启→探测切换一条龙）
- 2026-07-14 render 自动创建区域组、清理 dns fallback-filter geosite
- 2026-06-29 render 自动注入 external-controller / secret 默认值
- 2026-06-01 TUI 布局与响应性改进
- 2026-05-14 生产入口与 reload 评估
- 2026-04-09/10 backend 重构、分发、geodata、状态套件等第一批计划落地

## 下一步（按优先级）

1. 填写退役计划中的功能对账表
2. 复跑 `docs/enterprise-tui/acceptance.md` 全部验收命令
3. 进入退役计划阶段 2：生产入口切到 cproxy，开始 4 周观察期

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
