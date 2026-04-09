# cproxy Backend 抽象设计

**目标**

将当前 `cproxy` 中散落在 `cli.py`、`api.py`、`runtime_state.py`、`process.py` 的后端细节重组为稳定的 backend/service 分层，统一静态配置视图、实时 API 视图和进程视图的数据模型，降低 CLI 分发层的耦合度。

本次设计聚焦：

- backend 数据模型
- API / runtime / process 三类后端边界
- query / diagnostics 两类 service 编排
- CLI 的瘦身路径

不在本次设计内：

- 新命令或新用户能力
- `--json` 输出
- systemd 行为变化

## 一、目标结构

建议目标文件结构：

- `src/cproxy/backend/models.py`
- `src/cproxy/backend/api.py`
- `src/cproxy/backend/runtime.py`
- `src/cproxy/backend/process.py`
- `src/cproxy/services/query.py`
- `src/cproxy/services/diagnostics.py`
- `src/cproxy/services/install.py`
- `src/cproxy/cli.py`
- `src/cproxy/output.py`
- `src/cproxy/proxyenv.py`

原则：

- backend 只负责“取数 / 改状态”
- services 只负责“编排策略”
- CLI 只负责“参数解析 + 输出”

## 二、统一数据模型

### 1. ProxyGroup

统一 API 与 runtime 返回结构：

- `name`
- `type`
- `current`
- `candidates`
- `alive`
- `delay`
- `source`

其中：

- `source=api` 代表实时状态
- `source=runtime` 代表静态配置

runtime 来源不伪造实时状态：

- `alive=None`
- `delay=None`

### 2. ProcessOwner

用于描述 `cproxy` 启动的进程 ownership：

- `pid`
- `program`
- `runtime`

用于 stop/restart/status 的归属校验。

### 3. QueryContext

用于 service 层统一判断数据来源：

- `groups`
- `api_available`
- `runtime_available`

## 三、backend 边界

### 1. API backend

职责：

- controller URL 和 secret 解析
- Mihomo API 请求
- 查询代理组
- 切换代理组
- 延迟检测

不负责：

- 输出
- 回退策略
- CLI 错误文案拼装之外的展示逻辑

### 2. Runtime backend

职责：

- 读取 `runtime.yaml`
- 解析 `proxy-groups`
- 返回统一 `ProxyGroup`
- 继续负责运行配置渲染

不负责：

- API 探活
- 切换代理

### 3. Process backend

职责：

- 启动、停止、重启、状态
- ownership 元数据读写
- `/proc/<pid>/cmdline` 校验

不负责：

- CLI 输出
- 代理连通性检查

## 四、service 编排

### 1. Query service

统一处理查询命令的数据来源策略：

- `current`
- `list-groups`
- `list-nodes`
- `ai-status`

规则：

- `current/list-groups/list-nodes`：API 优先，runtime 回退
- `ai-status`：仅 API

### 2. Diagnostics service

统一处理：

- `test`
- `test-group`

规则：

- `test-group`：仅 API
- `test`：process backend + proxy env

## 五、CLI 收敛目标

最终 `cli.py` 不再：

- 直接决定“API 失败后怎么回退”
- 直接拼 `ProxyGroup` dict
- 直接读取 runtime YAML
- 直接处理 process ownership 细节

`cli.py` 只做：

- 参数解析
- 调 service/backend
- 将结果交给现有输出函数
- 将异常转成退出码与 stderr

## 六、迁移顺序

建议按以下顺序逐步迁移：

1. 先引入 `backend/models.py`
2. 合并 `api.py` 与 `runtime_state.py` 相关读取职责到 backend
3. 迁移 `process.py` 到 backend
4. 引入 `services/query.py`
5. 引入 `services/diagnostics.py`
6. 最后瘦身 `cli.py`

每一步都要求：

- 外部命令行为不变
- 测试先失败再修复
- 不同时引入新功能

## 七、风险与约束

- 这是结构重排，不应顺手扩展新能力
- 最大风险是“纯重构”过程中改变回退策略或错误文案
- runtime 与 API 的数据模型不对齐时，必须在 backend 统一而不是在 CLI 特判
- process ownership 校验不能退化，否则会重新引入误杀风险
