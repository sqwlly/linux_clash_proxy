# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- `AI-MANUAL` 稳定性兜底：组类型由 `select` 改为 `fallback`（故障转移），手动固定的节点挂掉后 mihomo 自动切到下一个存活节点；候选列表按「`ai-manual-prefer` 钉选 → 实测稳定度序 → 静态地区」三级排序
- `cproxy ai-rebalance`：多轮探测 `AI-MANUAL` 全部叶子，按 `rank_key`（成功↑/失败↓/最大延迟↓/平均延迟↓/抖动↓/历史惩罚↓）实测稳定度重排并持久化到 `ai-manual-order.json`，随后重渲染并热重载；配套 `cproxy-ai-rebalance.timer`（30 分钟周期）自动驱动
- 实时网速监控：TUI 仪表盘「实时流量」卡片升级为「实时网速」（下载/上传显示瞬时速率，新增累计量行，连接数不变），并新增 `cproxy speed` 命令（一次性测速 + `--watch` 持续刷新 + `--interval` + `--raw`）；复用 mihomo `/connections` 全局累计计数器做差分，重启归零自动钳位
- AI 出口快速自愈看门狗：新增 `cproxy ai-watch`（配套 `cproxy-ai-watch.timer`，2 分钟周期），对当前 codex/Gemini 出口做**语义探测**——识别 Gemini 的「location not supported」地区门禁（此前 4xx 一律判 ok，测不出「节点通但 AI 用不了」）——连续 2 次语义失败即切到下一个好节点，10 分钟冷却期抗抖，状态持久化到 `ai-watch.json`

### 移除

- **legacy `proxy.sh` 工作流退役（breaking）**：4 周观察期已到期且无 P0/P1，执行退役阶段 3。仓库侧删除 `proxy.sh`、`update_config.sh`、`scripts/install-system-commands.sh`、6 个 legacy helper 脚本、10 个 legacy systemd 文件与 19 个测试；`systemd/install-systemd.sh` 与 `scripts/install.sh` 的 `--with-legacy` 分支一并移除（其非 legacy 能力 `--with-cproxy-alias` 随之失去）。系统侧同步清理：6 个已安装单元、`clash-proxy`/`clash-proxy-update`/`cproxy-update` 三个 PATH wrapper、`/usr/local/lib/clash-proxy/` payload、`/etc/default/clash-proxy-command*` 与 legacy logrotate 配置及其 cron 行（备份 `/root/legacy-system-backup-20261017.tar.gz`）。**不受影响**：活跃的 `clash-proxy-traffic-collector.{service,timer}`、`/etc/logrotate.d/cproxy`、`systemd-user/*` 与全部 cproxy 命令

### 改进

- CI 的 Lint 步骤从 `ruff check src/` 扩到 `src/ tests/`，并清掉 `tests/` 下积压的 42 项（6 条超长行、4 处 `l` 歧义变量名、import 顺序与未用导入等）；`tests/*` 整体豁免 E402——测试必须在 `import cproxy` 之前把仓库 `src/` 插进 `sys.path`，那几个 import 不在文件顶部是结构使然
- `cproxy ai-status` 人读面板改为**表格式**：三条出口合成一张表（角色/池/当前节点/延迟/状态），连通性、候选池各一张；带 `│`/`─` 行列分隔线（表头下**与每行之间**都有横线；边框浅色以免压过内容），列宽按内容对齐，区块标题下不再叠分隔线（表框顶边即分隔）。探测详情列只在有失败时出现（全通过时它白占宽度），候选池的「类型」区分 `自动池`/`区域组`/`直连池`
- AI 探测预算分两档，因为代价不对称：**面板**（`ai-status` / TUI 即时探测）收紧到 `3s × 1 次重试`（原 `8s × 2`），实测从 9~19s 降到 5~8s；**看门狗**（`ai_target_verdict`）维持 `8s × 2` 的耐心不变——它判的是「该不该换节点」，一次误判会触发真实切换，代价远高于多等几秒。两档各有测试锁定，防止有人把面板的提速顺手搬到看门狗上
- AI 出口解析收敛为单一实现（`backend.runtime.resolve_ai_route`），CLI 与 TUI 共用：TUI 仪表盘与 AI 路由面板此前各有一份独立解析、且都直读**组自身**的 `delay`/`alive`，而 `AI-MANUAL`/`AI-GEMINI` 是 fixed fallback（mihomo 不做健康检查、`history` 恒空），于是延迟永远显示 `─`、健康永远 `?`；现在与 CLI 面板同样下钻到出口节点取真实值，节点名也统一走 `normalize_name`。顺带修掉手动模式下「备用」的映射口径——它应按**出口节点地区**取另一侧，此前 CLI 传池组名，钉在美国节点时会把备用算成它自己所在的 `AI-US`
- `cproxy ai-status` 输出重构：修掉兜底模式下「出口显示为 `-`」的解析失真——旧实现假定 `AI-MANUAL` 的 `now` 一定是 AI-* 组名，而它现在是一个**真实节点**，于是拿节点名去查组表、取到无 `now` 的节点条目、打出空出口与空叶子。现统一沿 `now` 下钻到真实出口节点（解析逻辑下沉到 `backend.runtime`，与 `incident` 共用），fixed fallback 组（`AI-MANUAL`/`AI-GEMINI`）的延迟回退到出口节点自身 history，`--raw` 不再打出 `delay=None`；版面按「出口」重组为「摘要 / 连通性 / 候选池」三区块，节点名统一走 `normalize_name`（消除同一节点两种写法），「候选池」明确标注 `AI-AUTO` 是自动池指针而非真实出口，且该区块只讲拓扑（有哪些池 / 各多少候选 / 当前指向谁）**不再列延迟**——延迟属于「出口健康」，由摘要三条出口行按各池组自身健康检查给出（那正是 mihomo 做故障转移决策的依据）；此前候选池按各组自己的 tester 另列一遍，同一节点会与摘要出现两个口径的数（如「出口 … 654ms」与「AI-SG … 未测得」同屏互相打架）；`--json` 契约不变（`schema_version` 1、17 键全保留）
- `cproxy switch` 放行 `fallback` 组手动指定：mihomo 尊重手动选择直到节点挂，`AI-MANUAL` 与 `AI-AUTO` 均可在 fallback 下切换（顺带修掉 TUI「切换美/新」在自动模式被类型守卫拦下的隐患）
- TUI / `ai-status` 的 AI 出口「模式」标签由「固定」改为「兜底」，如实反映故障转移语义；非自动池一支进一步细化为 `兜底 · 直连池`——出口是 mihomo 在 `AI-MANUAL` 的直连候选池里按列表顺序取第一个存活节点（池内顺序才体现用户意图，由 `ai-rebalance` 维护），用「手动」描述会让人以为节点被钉住（实测出现过韩国节点挂掉后自动落到新加坡）
- `AI-MANUAL` 的 codex 候选池扩展韩/英：实测 `🇰🇷韩国 01 IEPL` 与 `🇬🇧英国 01 IEPL` 对 codex 稳定（5/5），纳入后 `ai-rebalance` 的 codex 稳定度排序随之覆盖，日本 TE 挂掉后可自动落到韩国 IEPL（仅进 codex 池，不污染 `AI-US`/`AI-SG` 自动池）
- `ai-rebalance` 排序加重历史失败惩罚权重，平滑单轮 codex 探测噪声（削弱「一轮 5/5、下一轮 1/5」的名次翻转）；当候选集未变且前 5 名顺序完全一致时抑制尾部 1 轮样本抖动触发的无意义重写与热重载；若当前 `AI-MANUAL` 出口在多轮实测中已不稳定而榜首稳定，重排后主动切到榜首叶子
- `cproxy render --apply`（及 `cproxy-refresh.service` 15 分钟定时任务与 `bootstrap`）：当磁盘 `runtime.yaml` 落后于运行实例时自动热重载并同步 `runtime_hash` 指纹，消除配置变更后 `status`/`doctor` 长期报「配置时效: 待刷新」的盲区
- `probe_history.jsonl` 限制最多保留最近 200 条记录（原子截断），防止 30 分钟周期定时任务让探测历史无限膨胀
- `agy` 热更新残留适配：`cproxy ai-watch`（2 分钟周期）自动回收终端闲置满 30 分钟且无活跃任务子进程的 `agy.<timestamp>.old` 残留进程（保护当前调用链与活跃终端），消除旧实例长期后台轮询 `daily-cloudcode-pa.googleapis.com` 抢占流式连接配额的问题；`runtime.py` 新增 `PROCESS-NAME-REGEX,^agy\.[0-9]+\.old,AI-MANUAL` 兜底热更新窗口期路由；`traffic.py` 自动剥离 `(deleted)` 并将 `agy.<timestamp>.old` 归并回 `agy`

### 修复

- `cproxy ai-watch` 看门狗无法选出替代节点（P0）：`APIBackend.get_groups()` 返回的 `/proxies` 字典中每个叶子节点也存在一条 `candidates=[]` 的条目，原 `_pick_next` 用 `if cand in groups: continue` 过滤嵌套组时把全部真实叶子一并跳过，导致 `_pick_next` 恒返 `None`；现改为用 `bool(entry.candidates)` 判嵌套组（与 `resolve_ai_leaf` 一致），并优先跳过健康检查已标死/超时的叶子
- 订阅刷新每日滚雪球产生 `#2_`、`#2__` 僵尸重名节点（P1）：`update_source_from_subscription` 原先在对新订阅执行 `_dedupe_proxy_names` 之前就调用 `_preserve_local_extra_proxies`，导致上一轮去重生成的 `Foo #2` 被误判为用户手写自建节点保留下来，随后再次去重被垫成 `Foo #2_`；现改为先对新订阅去重再比对本地附加节点，并在 `_preserve_local_extra_proxies` 中过滤历史去重副本后缀（自动清理残留的 `#2_` 僵尸节点）
- `cproxy ai-rebalance` 热重载成功后同步调用 `mark_runtime_applied(runtime_path)`，修复每 30 分钟重排热重载后 `cproxy status` / `doctor` 误报「运行配置未跟随最近一次 render」的问题
- `cproxy start` 的外来实例提示与 `cproxy incident` 的服务诊断此前指向系统级 legacy `clash-proxy.service`（该单元已随退役删除）。现改指真实生产单元——**用户级** `cproxy.service`（`systemctl --user ...`），同时移除 `start()` 中原用于拦截异构 legacy 服务的 `systemctl is-active` 预检，避免 `cproxy.service` active 时执行 `cproxy restart` / `ExecReload` 在 `stop()` 后被自身单元状态拦死；`restart()` 刻意仍只做 stop+start，以免 `ExecReload` 回头调 `systemctl --user restart` 自我递归
- `ai-status` / TUI 的延迟不再把 mihomo 的**哨兵值**当真实延迟展示：`history` 里的 `5000` 是探测**超时**（本项目组配置不设 `timeout`，取 mihomo 默认 5000ms），`0` 是**未测得**。此前按毫秒渲染，会把「备用节点其实在超时」显示成 `5000ms · 正常`——读起来又快又健康，是比旧版显示 `─` 更糟的错误信息（生产实测 AI-US 的 history 为 `[5000, 861, 5000, 267]`）。现按三态渲染 `NNNms` / `超时` / `未测得`，且非 ok 时不再并排显示健康标记（避免「超时 · 正常」这种自相矛盾）；`--raw` / `--json` 仍保留原始数值供机器解析
- `ai-status` 的连通性明细只在探测失败时显示：成功时的 `HTTP 401/403` 是「可达但需鉴权」，摆在「正常」后面会被读成故障码
- `cproxy snapshot compact` / `snapshots --json` 遇文件名时间戳不可解析的快照（如手工拷入的 `runtime-backup.yaml`）会直接 `TypeError` 崩溃：回退取文件 mtime 时把关键字写成了 `tzinfo=`，而 `datetime.fromtimestamp` 的参数名是 `tz`。真实快照名都合规，所以该分支一直未被触发；已修并补回归测试
- 清掉 `ruff check src/` 与 `mypy src/` 的全部历史报错（8 + 35 项），CI 的这两个步骤此前长期失败。这些多为类型不安全而非线上崩溃（运行时都有 `if`/`dict.get` 兜底）：TUI 表格同步把可能为 `None` 的 `RowKey.value` 当作字典键、`ai-status` 列表渲染依赖中间布尔量传递 `choice is not None` 的收窄、`cli.py` 用一个变量名承载三种不同的 Report 类型、`feedback.py` 的流对象被注解为 `object`。另修掉 `snapshots.py` 中 `weekly_seen` 的注解与实参元数不符、表格同步函数参数过窄（`list`/`dict` 改为 `Sequence`/`Mapping`）等

## [1.1.0] - 2026-10-15

### 改进

- Gemini 单独分流：新增 `AI-GEMINI`（fallback，优先韩/台/德/法/英/巴/迪拜/澳），Google / Gemini / Antigravity 域名规则排在 `PROCESS-NAME,agy` 之前；ChatGPT/Claude 与 agy 非 Google 流量仍走 `AI-MANUAL`（美日新），避免美日新出口触发 `User location is not supported`
- `cproxy status` / `ai-status`（及 TUI）同步展示通用出口与 Gemini 出口；AI 探测新增 Gemini API 目标（可用 `ai-gemini-api-url` 覆盖）
- `cproxy list-groups` / `list-nodes` 人读输出改走用途树：入口显示「默认流量 / AI 出口」，`list-nodes 默认流量` 可用；`cproxy group` 作为 `list-groups` 别名。`--raw` / `--json` 仍是 Mihomo 原始组名
- TUI 去掉与页签重复的页面大标题，状态条收进各面板标题行；操作按钮改为单行内容宽度的短标签（主操作实心、次要幽灵、危险描边），工具栏不再撑满整行
- `cproxy switch` 与 TUI 代理页共用三层用途树（默认流量 / AI 出口 / 附加订阅 → 地区或自动 → 节点）；render 时去掉与中文国家组重复的 `🇯🇵 Japan` / `🇺🇸 United States` / `🇸🇬 Singapore`，`AI-MANUAL` 直接挂日美新节点，主订阅仍按国家注入独立 selector。热重载会把旧的 `AI-US` / `🇯🇵 Japan` 选择映射到对应节点
- TUI 完成中文化、80 列响应式布局与后台任务化；配置保存增加 YAML 校验、快照和原子替换，重启/丢弃修改/shell rc 变更增加二次确认与恢复备份
- 安装默认包含 `textual` TUI extra，root clean-host 路径先安装运行依赖；可用 `CPROXY_INSTALL_TUI=0` 选择纯 CLI
- `cproxy switch` 交互式选择器增强：`/` 键进入搜索模式（模糊匹配，大小写不敏感），输入即时过滤候选列表，`Esc` 退出搜索恢复完整列表；延迟标注着色（≤200ms 绿、≤500ms 黄、>500ms 或超时红、无记录暗灰）；当前活跃节点标 `✓`；列表超出窗口时显示 `▲`/`▼` 滚动提示与剩余项数；标题行含位置指示 `[3/15]`（搜索过滤后显示匹配数与总数）；底部显示按键提示行；切换结果输出改为 `旧选择 → 新选择` 对比格式并附延迟信息

### 修复

- 订阅 TUI 不再调用冻结的 legacy 导入器，改为原生预览与应用，并支持附加订阅及选择器挂载
- 安装脚本默认不再为已停用 legacy 日志写入轮转配置；缺少 Textual 时 `cproxy-tui` 给出可执行安装指引而非 traceback
- `cproxy switch` 交互选择器：重绘先擦旧帧再画，退出时清掉菜单，避免分组/节点两级画面叠在一起、滚动时短行留下残字；节点列表 `q` / `Esc` / `←` 返回分组选择（底栏改为「返回」），再取消才退出
- `cproxy switch` 卡顿：每次打 Mihomo API 都把约 60KB 的 config.yaml 用纯 Python YAML 解析三遍（单次约 120ms，一次切换合计约 2s）；现改为 C 解析器 + mtime 缓存，同一次选择复用 `/proxies` 快照，确认后不再重复拉延迟
- AI-MANUAL 覆盖 Google / Gemini / Antigravity：原先只精确匹配 `gemini.google.com` 等四个主机，`agy` 实际请求的 `daily-cloudcode-pa.googleapis.com`、`oauth2.googleapis.com`、`antigravity.google` 会落到订阅的 `DOMAIN-KEYWORD,google` 走 CyberGuard，与 ChatGPT/Claude 出口 IP 不一致。现改为 `google.com` / `googleapis.com` / `appspot.com` 等后缀 + `antigravity`/`gemini` 关键字 + **`PROCESS-NAME,agy` 兜底**，并让 `ai-connections` 能看到这些连接
- 切换 AI 出口组时断开旧连接：mihomo 换组不会拆掉已建立的 TCP，Google 会按新旧 IP 混用把会话判成 `User location is not supported`；`cproxy switch` / TUI 切 `AI-MANUAL` 等组后主动 close 相关连接
- AI-MANUAL 热重载默认改为日本（不再是 AI-AUTO→美国）；区域组把 IEPL 节点排到 1X 前面，避免每次 render 后又落到 Google 不认的数据中心 IP

### 新增

- 配置快照阶梯管理与静默去重：`cproxy snapshots` 列表展示人性化相对时间、保留阶梯标签（`[近期全量]`、`[每小时]`、`[每日采样]`、`[每周采样]`）与格式化大小；基于内容 SHA-256 哈希实现零改动时自动跳过；在生成快照时全自动后台静默分级清理，无需用户手动维护
- 交互式安全回滚与 Diff 预览：`cproxy rollback` 在终端下自动唤起交互选择器与快照差异（Unified Diff）彩色对比，提供 `[Y/n]` 防手抖确认；支持独立比对命令 `cproxy snapshots diff [target]`
- CLI 全命令视觉排版规范化：重构 `security-check` 为结构化安全体检面板并提供精准修复建议；`ai-connections` 增加汇总摘要与域名列宽自动对齐；`shadow-history` 引入易读时间戳与状态徽标；`current` 补充组类型、延迟与存活状态；`test-group`、`test`、`doctor` 延迟与体检项全面实现垂直对齐
- TUN 透明代理服务与内核状态管理：提供 `cproxy tun` 命令用于管理并查看 TUN 网卡及透明代理状态
- `cproxy doctor` 提供配置、进程、配置时效、Mihomo API 与 GeoIP 的统一体检和恢复命令；状态、查询、诊断、刷新与流量命令新增带版本信封的 `--json`
- `cproxy bootstrap --subscription-url URL` 支持在没有 legacy 配置时直接完成首次初始化；新增 `scripts/test.sh` 固定使用同一 Python 的 `python -m pytest`
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
- `ai-gemini-prefer` 配置项：按给定顺序把指定节点钉为 `AI-GEMINI`（fallback 组）的首选出口。fallback 组不可用 `cproxy switch` 手动指定，此前只能靠 render 的硬编码排序（1X 恒排在 IEPL 前）；该键让实测更稳的节点成为默认出口，未入选的名字静默忽略。已纳入 `LOCAL_PREFERRED_KEYS`，刷新订阅时不会被合并冲掉

### 修复

- `scripts/install.sh` 在 root 下改走**系统级**安装（`pip install --force-reinstall --no-deps` 装到 `/usr/local`），不再用 pipx / `--user`：`cproxy.service` 硬编码 `/usr/local/bin/cproxy`，而 PATH 里 `~/.local/bin` 排在它前面，两份副本会让交互命令与 systemd 服务分派到不同版本的代码。本机此前已实际处于该状态（`/root/.local` 与 `/usr/local` 各一份 1.0.0），现统一为单副本；非 root 用户仍走原有 pipx / `--user` 逻辑
- 阶段 2 的一处遗漏：`clash-proxy-refresh.path` 此前仍是 `active (waiting)`——阶段 2 只 `disable --now` 了 `clash-proxy.service`，没停这个独立 unit，于是它持续监听 `/root/clash_proxy/config.yaml`，一旦该文件变化就会拉起 legacy 刷新流程去操作已由 cproxy 接管的运行时。已停掉（未 disable、未删文件，回滚 = `systemctl start`）；至此 legacy 侧全部 unit 为 `inactive` + `disabled`
- README / USAGE 里「截至 2026-05-14……当前 active 的生产入口仍是 root 级 `clash-proxy.service`」与 2026-09-11 已切换的事实相反，照此排障会去翻一个已停用的服务。已改写为以 cproxy 用户级链路为唯一生产入口，legacy 侧保留**带状态的对照表**，并提示 `/usr/local/bin/cproxy-update` 名字形似 cproxy 子命令、实际转发到 legacy 的 `update_config.sh`
- **`--raw` 破坏性变更（需脚本注意）**：`TRAFFIC_AUDIT_PROCESS` 行的 `down`/`up` 改为 `total_down`/`total_up`。该行口径已从「仅代理」改为「全量」，复用旧字段名会让外部脚本静默拿到含 ~92% 直连的数字；改名使破坏在解析期即可见，而非悄悄算错。`proxy_down`/`proxy_up` 为新增字段。`cproxy status --raw` 字段不变
- `实际配置` 行原本恒不渲染：`process.start()` 与 `status()` 都取 `runtime_file(paths)`，路径比对恒等。改为 `配置时效` + 内容指纹判据
- `runtime_metrics` 的 `/proc` 读取加 `errors="replace"`：`UnicodeDecodeError` 是 `ValueError` 子类，不被 `except OSError` 捕获，会穿透「绝不阻塞 status」的契约
- `_traffic_bar` 对 0 字节行返回定宽空白占位（`pad=True` 时），否则该行标签左移一整个条形宽度、整表错位
- `_shorten_path` 增加最终 clamp：末段自身超宽时（如极长可执行名）循环压不进 `width`，原先会返回超宽串突破列宽契约
- **刷新订阅后代理静默宕机**：机场订阅会给出同名不同 server 的节点（实测 CyberGuard 的 `🇯🇵日本 01 | 1X | 推荐` 就是两条），mihomo 遇重名直接以 `duplicate name` fatal 退出。刷新是「订阅更新 → render → 重启」，进程解析失败发生在 `start()` 那个仅 `sleep(0.1)` 的存活检测窗口**之后**（实测约 156ms 才退出），于是 `cproxy refresh` 回报「已重启应用新配置」而代理其实已经挂掉。现有三层防护：合并订阅时 `_dedupe_proxy_names` 对重名追加 `#k` 后缀并按出现顺序同步组引用（生成的新名还会避让订阅里本就存在的同名节点）；重启分支后校验内核是否真正就绪，失败则回滚上一版 runtime 快照并重启；热重载成功即证明配置已被 mihomo 接受，无需重复校验

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

[1.1.0]: https://github.com/sqwlly/linux_clash_proxy/releases/tag/v1.1.0
[1.0.0]: https://github.com/sqwlly/linux_clash_proxy/releases/tag/v1.0.0
