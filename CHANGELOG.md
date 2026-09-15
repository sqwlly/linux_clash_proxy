# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- legacy 入口弃用警告与安装解耦：`clash-proxy` / `clash-proxy-update` / `cproxy-update` 三个 wrapper 每次调用都向 **stderr** 打印弃用警告并指向 cproxy 等价命令（走 stderr 是为了不污染 stdout——`clash-proxy status --raw` 的脚本消费仍可正常解析）；直接执行 `./proxy.sh` 也会提示，经 wrapper 调用时靠 `CPROXY_LEGACY_WARNED=1` 去重、只打一次。`scripts/install-system-commands.sh` 与 `systemd/install-systemd.sh` 默认**不再安装** legacy（前者需 `--with-legacy`；后者同理，但始终安装活跃的 `clash-proxy-traffic-collector`），`install.sh` 侧对应 `CPROXY_INSTALL_SYSTEM_COMMANDS=legacy`——这样将来任何一次重跑都不会把已停用的入口重新装回 PATH。另修一处隐患：`--with-cproxy-alias` 会把 pip 安装的 `/usr/local/bin/cproxy` 覆盖成指向 `proxy.sh` 的 wrapper、令 cproxy 命令直接失效，现已加保护并在检测到非本脚本产物时拒绝覆盖
- CLI 入口与帮助重做：`cproxy` 不带参数时输出按功能分组的命令清单（此前完全静默、exit 0）；顶层 `--help` 从 49 行平铺列表收敛为分组清单，`usage:` 行不再把 34 个命令名罗列两遍（此前 choices 列表与 positional 列表各列一遍）；命令与参数描述统一为中文（此前命令摘要全英文，`probe-stable-node` 的 8 个参数与 positional 无任何描述）；`usage:` / `options:` / `positional arguments:` 等 argparse 英文段落标题一并中文化。分组名沿用 proxy.sh `usage()` 的既有词汇（配置与进程 / AI 路由控制 / 命令级代理 / 诊断与排障），另加「流量与维护」「交互界面」两组覆盖 cproxy 独有命令
- CLI 错误路径：拼错命令或取值时给出「您是不是想输入 X」建议（此前把全部 34 个命令名整个列一遍）；argparse 报错中文化（`缺少必需参数: group`、`参数 --top 需要整数，收到: 'abc'` 等）；stderr 错误统一着红并随 `NO_COLOR` / `CPROXY_COLOR` 门控，**整条消息包裹**而非只染「错误: 」前缀——只染前缀会把 ANSI 复位码插进 `错误: ...` 子串中间，打爆按子串断言的测试与脚本；散落各处的 `raise SystemExit("错误: ...")` 一并纳入同一出口
- shell 补全（bash/zsh）：`cproxy completion bash|zsh [--install]` 输出或安装补全脚本（bash 装到 XDG 标准目录后自动生效，zsh 装到 `~/.zfunc` 并提示 fpath 配置）；候选由隐藏命令 `cproxy __complete` 提供，命令与选项静态可得，分组名与节点名按需实时拉取——controller 不可达时短超时（0.5s）后静默返回空，退出码恒为 0，绝不卡住 `<Tab>`
- `cproxy switch` 不带参数时进入交互式选择：先选分组（只列可手动切换的 selector 类型）、再选节点，`↑↓`/`j k` 移动、`Enter` 确认、`q`/`Esc`/`Ctrl-C` 取消；纯标准库实现（termios + ANSI），**不依赖可选依赖 textual**，未装 tui 依赖时 switch 依旧可用；非交互终端退化为「打印可选分组 + 退出码 2」，不会挂起等待输入
- 工程改进：`--raw` 的 15 处重复定义收敛到 `_add_command(raw=True)` 单一入口（刻意**不用** `parents=` 挂到根 parser：README 与脚本一律是子命令后置写法 `cproxy status --raw`，上提会让这些调用全部报错）；参数取值枚举（profile / strategy / traffic action / traffic 维度）提为单一常量，供 argparse 的 `choices=` 与补全共用，避免两处各写一份而漂移

- 订阅余量展示：refresh 时解析订阅响应的 `subscription-userinfo` 头（机场账户用量，支持度比面板信息节点更广）并落盘 `~/.local/state/cproxy/subscription-info.json`（原子写，头缺失保留旧记录）；`cproxy status` 新增「订阅」区块——主订阅在前、附加机场按配置顺序，各自给出 `剩余 x / 总量（已用 n%）· 到期 yyyy-mm-dd`，余量 <20% 转黄、耗尽或已过期转红、到期 ≤7 天转黄，记录超 48h 未更新标注「数据截至」；`--raw` 输出逐订阅的原始字段；已从配置移除的机场不再展示
- 多机场订阅支持：`config.yaml` 新增 `subscriptions` 列表（`name` + `url`），`cproxy refresh` 在主订阅之外逐一更新附加机场，各自生成独立分组（`{name}` select 入口 + `{name}-HK/JP/...` 地区 url-test 组），节点以 `{订阅名} ` 前缀命名避免跨机场撞名；单项机场下载失败保留其原有节点与分组，不影响主订阅与其它机场
- 订阅负载解析新增 base64 分享链接 nodelist 形态（`services/nodelist.py`）：vless（reality/grpc/ws）、hysteria2、tuic、trojan、ss 分享链接统一转 mihomo proxies，单条畸形链接跳过不否定整份订阅
- 完整型订阅合并时保留本地附加分组：本地存在而订阅未提供的分组（自建分组、附加机场分组）整体保留，其名称引用（如主订阅组里挂的机场入口）按本地原位置插回同名组，不再随每日订阅更新丢失

- `cproxy status` 面板升级：`◆ cproxy` 主标题与 `▸` 区块标签（与 proxy.sh 面板视觉统一，随 `CPROXY_ICONS` 门控）、区块标题下加定长蓝色细分隔线（增强区块分隔感，NO_COLOR 下为普通横线）、键值列按东亚宽度对齐、长路径压缩为 `$HOME` → `~` 与 `…/` 形式（保留辨识尾段，跳过 `node_modules`/`bin`/版本号等噪声段）；`--raw` 输出逐字不变
- `cproxy status` 新增「按进程」流量明细（`--top N`，默认 5；`--no-process` 关闭）：每进程给出占总流量比例，以及按链路拆分的 `代理↓ / 代理↑ / 直连↓ / 直连↑` 四个字节列，直接暴露"谁在消耗流量、谁在绕开代理"（`直连 = 总量 − 代理`；用字节而非单个代理占比，是因为 0.1% 这类低占比进程在百分比下看不出量，`0 B` vs `324 MB` 才说明问题）
- 流量表渲染器泛化为「占比 + 任意数值列 + 流量条 + 标签」，`代理↓/代理↑/直连↓/直连↑` 与 `↓下载/↑上传` 共用同一套排版
- 流量按链路着色：代理 ↓绿/↑青、直连 ↓黄/↑洋红，覆盖 `status` 今日摘要（直连行流量条同黄）、按进程四列拆分表（表头同色作图例）与 `traffic audit` 汇总行；「流量」列升级双色条——同一条内绿段=代理、黄段=直连，条宽仍按该行总量。上色在列宽 pad 之后进行，对齐不受 ANSI 转义干扰，`CPROXY_COLOR=never` / `NO_COLOR` 下逐字不变
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

- 阶段 2 的一处遗漏：`clash-proxy-refresh.path` 此前仍是 `active (waiting)`——阶段 2 只 `disable --now` 了 `clash-proxy.service`，没停这个独立 unit，于是它持续监听 `/root/clash_proxy/config.yaml`，一旦该文件变化就会拉起 legacy 刷新流程去操作已由 cproxy 接管的运行时。已停掉（未 disable、未删文件，回滚 = `systemctl start`）；至此 legacy 侧全部 unit 为 `inactive` + `disabled`
- README / USAGE 里「截至 2026-05-14……当前 active 的生产入口仍是 root 级 `clash-proxy.service`」与 2026-09-11 已切换的事实相反，照此排障会去翻一个已停用的服务。已改写为以 cproxy 用户级链路为唯一生产入口，legacy 侧保留**带状态的对照表**，并提示 `/usr/local/bin/cproxy-update` 名字形似 cproxy 子命令、实际转发到 legacy 的 `update_config.sh`
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
