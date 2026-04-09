# cproxy Geodata Design

**Goal:** 解决 `country.mmdb` 在无代理或受限网络环境下可能无法自动获取的问题，让安装、文档和运行前诊断都给出明确反馈。

## 背景

当前 `cproxy render` 会注入 `GEOIP,CN,DIRECT,no-resolve`，因此运行时隐含依赖 GeoIP 数据文件。现状有两个缺口：

- 安装脚本只安装 CLI 并初始化配置，不检查 GeoIP 数据是否存在
- 文档没有说明 `country.mmdb` 的依赖、自动下载可能失败，以及用户手动准备路径

这会导致首装用户在无代理或受限网络环境下，直到 Mihomo 启动或规则生效时才暴露问题，反馈过晚。

## 设计原则

- KISS：不在安装脚本里引入复杂下载器或额外依赖
- YAGNI：先做检查、提示和运行前诊断，不预留多源下载编排
- DRY：GeoIP 路径解析与检查逻辑集中到一个模块
- SOLID：安装脚本负责安装与提示，CLI 负责运行前检查，文档负责说明边界

## 方案

### 1. 统一 geodata 检查

在 Python 侧新增 geodata 检查模块，负责：

- 解析常见 `country.mmdb` 候选路径
- 判断 GeoIP 规则是否被启用
- 返回可读的检查结果和建议

候选路径优先级：

1. 配置中显式声明的 geodata/mmdb 路径
2. `~/.config/cproxy/country.mmdb`
3. `~/.local/share/cproxy/country.mmdb`

当前只做用户级路径检查，不扩展到系统全局路径，避免模糊边界。

### 2. 安装阶段行为

`scripts/install.sh` 在完成包安装和 `cproxy init` 后：

- 尝试调用一个轻量检查入口
- 如果已找到 `country.mmdb`，打印位置
- 如果未找到，只打印清晰警告和手动放置路径

安装脚本不强制下载 `country.mmdb`。原因是：

- 首次安装阶段未必已有可用代理
- 自动下载失败原因复杂，强行下载会让安装脚本变脆
- 明确提示比静默失败更重要

### 3. 运行前诊断

在 `cproxy test` 中加入 geodata 诊断项：

- 如果配置启用了 `GEOIP,CN` 且未找到 `country.mmdb`，返回失败项
- 诊断信息要给出预期文件名和建议放置位置

不在 `status` 里强行塞入该项，避免把运行状态和准备状态混在一起。

### 4. 文档

README、USAGE、TROUBLESHOOTING 需要新增：

- `country.mmdb` 依赖说明
- 无代理/受限网络下自动获取可能失败
- 推荐手动放置路径
- 排障命令：`cproxy test`

## 验证

- 安装脚本测试覆盖“缺失时打印警告”
- 诊断测试覆盖“缺失时失败，存在时通过”
- 文档更新覆盖安装说明和排障说明
