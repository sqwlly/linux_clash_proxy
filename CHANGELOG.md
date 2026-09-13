# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- 多机场订阅支持：`config.yaml` 新增 `subscriptions` 列表（`name` + `url`），`cproxy refresh` 在主订阅之外逐一更新附加机场，各自生成独立分组（`{name}` select 入口 + `{name}-HK/JP/...` 地区 url-test 组），节点以 `{订阅名} ` 前缀命名避免跨机场撞名；单项机场下载失败保留其原有节点与分组，不影响主订阅与其它机场
- 订阅负载解析新增 base64 分享链接 nodelist 形态（`services/nodelist.py`）：vless（reality/grpc/ws）、hysteria2、tuic、trojan、ss 分享链接统一转 mihomo proxies，单条畸形链接跳过不否定整份订阅
- 完整型订阅合并时保留本地附加分组：本地存在而订阅未提供的分组（自建分组、附加机场分组）整体保留，其名称引用（如主订阅组里挂的机场入口）按本地原位置插回同名组，不再随每日订阅更新丢失

- `cproxy status` 面板升级：`◆ cproxy` 主标题与 `▸` 区块标签（与 proxy.sh 面板视觉统一，随 `CPROXY_ICONS` 门控）、键值列按东亚宽度对齐、长路径压缩为 `$HOME` → `~` 与 `…/` 形式（保留辨识尾段，跳过 `node_modules`/`bin`/版本号等噪声段）；`--raw` 输出逐字不变
- `cproxy status` 新增「按进程」流量明细（`--top N`，默认 5；`--no-process` 关闭）：每进程给出占总流量比例，以及按链路拆分的 `代理↓ / 代理↑ / 直连↓ / 直连↑` 四个字节列，直接暴露"谁在消耗流量、谁在绕开代理"（`直连 = 总量 − 代理`；用字节而非单个代理占比，是因为 0.1% 这类低占比进程在百分比下看不出量，`0 B` vs `324 MB` 才说明问题）
- 流量表渲染器泛化为「占比 + 任意数值列 + 流量条 + 标签」，`代理↓/代理↑/直连↓/直连↑` 与 `↓下载/↑上传` 共用同一套排版
- `cproxy traffic audit` 的「按进程」小节改用同一口径（全量 + 链路拆分），此前 `chain NOT LIKE '%DIRECT%'` 过滤会漏掉占 ~92% 的直连流量
- 直连判据由子串匹配改为「**链路末端动作是 DIRECT**」：mihomo 若某分组当前选中节点就是 DIRECT（链路形如 `Group -> ... -> DIRECT`），该流量同样不经代理；而原 `chain LIKE '%DIRECT%'` 既受 SQLite LIKE 大小写不敏感影响，又会把名字含 direct 的代理节点（如 `DIRECT-US`）误判为直连。生产数据上两种判据结果一致（已核对）
- `cproxy status` 补齐 proxy.sh 面板的运行时指标（退役 parity）：`连接数` / `运行时间` / `内存` / `日志` / `配置时效`。其中运行时间/内存/日志由新增的 `backend/runtime_metrics.py` 以纯 `/proc` 只读采集，不依赖 `ps`/`ss`/`du`，取不到时该行自动省略；`配置时效` 通过启动时记录的 runtime 内容指纹（sha256）与磁盘当前指纹比对，不一致才显示「运行实例未跟随最近一次 render」
- 区块标题样式收敛到单一入口 `_section_heading()`：`rollback`/`switch`/`probe-stable-node` 等此前直接调用 `_section_title()` 而绕开图标门控的路径一并统一
- 渲染规则加固：AI-MANUAL 域名覆盖扩展（claudeusercontent / sora / grok / openai.azure.com / githubcopilot / cdn.auth0.com / challenges.cloudflare.com）；注入规则前移到订阅规则之前防遮蔽；AI 冲突清理泛化到任意订阅商组名；清理有害规则（裸 `GEOIP,CN`、`safebrowsing.googleapis.com` 直连、`cursor.sh` 直连）；大流量下载源（pytorch / pypi / pythonhosted / npmjs / npmmirror）直连
- 进程维度流量归因：渲染注入 `find-process-mode: always`，新增 `traffic_process_samples` 表（天×小时×进程×链路，90 天），`cproxy traffic show --by process`、audit 的"按进程"小节，人读展示保留完整路径并剥离内核 ` (deleted)` 标记
- `cproxy traffic audit`：代理/直连占比 + 仅代理流量的目标主机与进程明细，快速发现"不该走代理的流量"
- `cproxy traffic show` 报表升级：列对齐（东亚宽度感知）、ASCII 流量条、占比列、表头加粗、ANSI 颜色（NO_COLOR 自动降级）
- `cproxy status` 增加今日流量摘要（总量/代理/直连占比），数据库缺失时静默跳过
- 流量采集 timer 间隔 1 分钟 → 30 秒，缩小短连接采样漏记窗口
- refresh 自愈与免中断：代理未运行时先用现有 runtime 拉起再拉订阅（bootstrap 死锁修复）；订阅下载显式走本机代理并回退直连（timer 环境无需代理变量）；新配置经 mihomo `PUT /configs` 热重载应用，长会话/长任务不中断，API 不可达才回退进程重启；热重载后照常执行分组探测
- `systemd-user/cproxy-subscription.{service,timer}`：每日 04:00（随机延迟）自动订阅更新；修正 `cproxy-refresh.service` ExecStart 路径（此前 203/EXEC 静默失败）与 `cproxy.service` PIDFile 与实现一致
- proxy.sh 退役计划阶段 2 落地：生产入口切至 cproxy 用户级服务（linger），旧 `clash-proxy.service` 停用保留回滚，观察期至 2026-10-09

- `cproxy ip-check`：基于 ipok.io 免费 API 的出口 IP 纯净度检测——多源风险评分（ip-api/Scamalytics/proxycheck/AbuseIPDB/ipapi.is/StopForumSpam 交叉）、DNSBL 黑名单、IP 类型/原生性、共享用户质量、AI 服务可用性快照；支持 `--node`（临时切换节点检测后自动恢复）与 `--ip`（检测任意 IP）
- `cproxy traffic`：代理流量统计报表，按出口链路 / 命中规则 / 按目标主机与按日汇总（`--days`、`--by`、`--top`、`--raw`）
- `cproxy traffic collect`：单次流量采集（对 Mihomo `/connections` 做连接级增量记账），数据落 `~/.local/state/cproxy/traffic.db`，跨重启累计，保留 90 天
- `systemd/clash-proxy-traffic-collector.{service,timer}`：每分钟自动采集的定时单元，由 `systemd/install-systemd.sh` 一并安装启用

### 修复

- **`--raw` 破坏性变更（需脚本注意）**：`TRAFFIC_AUDIT_PROCESS` 行的 `down`/`up` 改为 `total_down`/`total_up`。该行口径已从「仅代理」改为「全量」，复用旧字段名会让外部脚本静默拿到含 ~92% 直连的数字；改名使破坏在解析期即可见，而非悄悄算错。`proxy_down`/`proxy_up` 为新增字段。`cproxy status --raw` 字段不变
- `实际配置` 行原本恒不渲染：`process.start()` 与 `status()` 都取 `runtime_file(paths)`，路径比对恒等。改为 `配置时效` + 内容指纹判据
- `runtime_metrics` 的 `/proc` 读取加 `errors="replace"`：`UnicodeDecodeError` 是 `ValueError` 子类，不被 `except OSError` 捕获，会穿透「绝不阻塞 status」的契约
- `_traffic_bar` 对 0 字节行返回定宽空白占位（`pad=True` 时），否则该行标签左移一整个条形宽度、整表错位
- `_shorten_path` 增加最终 clamp：末段自身超宽时（如极长可执行名）循环压不进 `width`，原先会返回超宽串突破列宽契约

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
