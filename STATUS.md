# 项目状态

更新日期：2026-09-11

## 当前阶段

**阶段 2：生产入口已切换到 cproxy 用户级链路，4 周观察期进行中（起算 2026-09-11）**

- `clash-proxy.service`（系统级，proxy.sh 编排）已 `disable --now`，仅停用未删除，回滚 = 重新 enable
- cproxy 用户级 `cproxy.service` 接管代理（linger 已开启）；`cproxy-subscription.timer` 接管每日订阅更新（热重载应用新配置，不中断连接），`cproxy-refresh.timer` 定期重渲染；流量采集 timer（系统级）继续调用 `cproxy traffic collect`
- legacy 文件（`proxy.sh`、`update_config.sh`、`/usr/local/bin/clash-proxy*`）保留原地，阶段 3 才移除
- 退役条件与分阶段步骤见 [proxy.sh 退役计划](docs/plans/2026-07-18-proxy-sh-retirement.md)

## 最近里程碑

- 2026-09-11 `cproxy status` 面板产品化与退役 parity 补齐：`◆`/`▸` 区块（与 proxy.sh 视觉统一）、键值列东亚宽度对齐、长路径压缩；新增「按进程」流量归因（`--top`/`--no-process`），按链路拆分为 `代理↓/代理↑/直连↓/直连↑` 四列；补齐 `连接数/运行时间/内存/日志/配置时效` 五项 proxy.sh 面板指标（新增 `backend/runtime_metrics.py`，纯 `/proc` 只读）。功能对账表「启动/停止/重启/状态」行的"已有等价"结论此前与实际不符，已补齐并附核对依据。安装沿用系统级非 editable 路径（`pip install --force-reinstall --no-deps .`），**未**走 `install.sh` 的 `--user` 分支以免产生 ~/.local 跨副本遮蔽
- 2026-09-11 code review 修复（cproxy）：`--raw` 的 `TRAFFIC_AUDIT_PROCESS` 字段 `down`/`up` 改名 `total_down`/`total_up`（口径已改全量，复用旧名会让脚本静默算错）；直连判据改为「链路末端动作是 DIRECT」（原子串匹配受 LIKE 大小写不敏感影响，且会误判名字含 direct 的代理节点）；`实际配置` 行原为恒不触发的死代码，改为 `配置时效` + 内容指纹；`/proc` 读取加 `errors="replace"` 防 `UnicodeDecodeError` 穿透；`_traffic_bar` 零值行补定宽占位；`_shorten_path` 末段超宽补最终 clamp
- 2026-09-11 渲染规则加固：AI-MANUAL 覆盖扩展、注入规则前移防订阅遮蔽、有害订阅规则清理（裸 GEOIP/safebrowsing/cursor.sh）、大流量下载源直连；流量统计升级（对齐条形图报表、`traffic audit`、进程维度归因 with `find-process-mode: always`、status 今日流量摘要、30s 采集间隔）
- 2026-09-11 阶段 2 切换：cproxy 用户级服务接管生产，修正 `systemd-user/cproxy.service` PIDFile 与实现一致

- 2026-07-29 probe 修复（双方都不稳定时允许切换）、端口冲突防护（cproxy start 检测生产服务、订阅脚本预检用户级 cproxy.service）
- 2026-07-25 cproxy 功能同步完成：Japan 组、probe history、guard/incident/ai-connections、进程管理；ruff/mypy 接入、cli.py 拆分、测试隔离、功能对账表填写
- 2026-07-24 生产入口每日订阅更新 systemd timer（04:00 + 30min 随机延迟）
- 2026-07-18 仓库卫生与发布化：`Country.mmdb` 移出 git（安装时改为下载回退）、根目录个人配置清理、proxy.sh 退役计划、`cproxy start` 外来实例预检、`scripts/install-mihomo.sh`（版本固定 + sha256 校验）、`CPROXY_EDITABLE=0` 生产安装约定、版本升到 1.0.0 并新增 `CHANGELOG.md`、测试解除 `/root/clash_proxy` 硬编码且 CI 去掉 sudo 步骤、新增 `cproxy snapshots`/`rollback`（自动快照回滚）与 `cproxy refresh`（订阅更新→render→重启→探测切换一条龙）
- 2026-07-14 render 自动创建区域组、清理 dns fallback-filter geosite
- 2026-06-29 render 自动注入 external-controller / secret 默认值

## 下一步（按优先级）

1. 观察期（至 2026-10-09）内确认 cproxy 链路无 P0/P1 事故
2. 复跑 `docs/enterprise-tui/acceptance.md` 全部验收命令
3. 观察期满后执行阶段 3：legacy 入口打 deprecation 警告 → 停装 `/usr/local/bin/clash-proxy*` → 删除 legacy 文件

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
