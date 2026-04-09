# cproxy Distribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将当前 `proxy.sh` 工具重构为用户级可分发 CLI `cproxy`，支持 `pipx` 安装、安装脚本和旧目录迁移。

**Architecture:** 先建立 Python 包骨架与新的用户级路径模型，再优先迁移查询类命令与输出层，最后迁移渲染、进程控制和安装体验。默认输出继续面向人类，保留 `--raw` 供脚本消费。

**Tech Stack:** Python 3、`pipx`、现有 `mihomo` 运行模式、少量 shell 安装辅助脚本

---

### Task 1: 建立 Python 包骨架

**Files:**
- Create: `pyproject.toml`
- Create: `src/cproxy/__init__.py`
- Create: `src/cproxy/cli.py`
- Create: `src/cproxy/config.py`
- Create: `src/cproxy/output.py`
- Test: `tests/` 下新增 Python CLI 基础测试

**Step 1: Write the failing test**

编写最小 CLI 测试，断言：

- `python -m cproxy.cli --help` 可运行
- 暴露命令组骨架
- 默认命令名为 `cproxy`

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_bootstrap.py -q`
Expected: FAIL，因为包骨架尚不存在。

**Step 3: Write minimal implementation**

创建最小可运行包：

- CLI 主入口
- 基础参数解析
- 统一路径对象
- 基础输出帮助

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_bootstrap.py -q`
Expected: PASS

### Task 2: 建立用户级目录与 init 命令

**Files:**
- Modify: `src/cproxy/config.py`
- Create: `src/cproxy/install.py`
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_init_command.py`

**Step 1: Write the failing test**

断言：

- `cproxy init` 会创建配置目录
- 生成默认 `config.yaml`
- 不污染旧仓库目录

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_init_command.py -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- 用户级 XDG 路径解析
- 目录初始化
- 配置模板写入

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_init_command.py -q`
Expected: PASS

### Task 3: 迁移查询类命令与输出层

**Files:**
- Create: `src/cproxy/api.py`
- Modify: `src/cproxy/output.py`
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_query_commands.py`

**Step 1: Write the failing test**

覆盖：

- `status`
- `ai-status`
- `list-groups`
- `list-nodes`
- `current`
- `test-group`
- `--raw`

使用临时 API server fixture 复刻当前 shell 测试行为。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_query_commands.py -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- API 访问层
- 默认输出
- `--raw` 输出
- 节点名规整

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_query_commands.py -q`
Expected: PASS

### Task 4: 迁移 switch 命令

**Files:**
- Modify: `src/cproxy/api.py`
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_switch_command.py`

**Step 1: Write the failing test**

断言：

- `switch` 能验证目标组类型与候选范围
- 切换后返回摘要输出
- 后续 `current` 能读到更新状态

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_switch_command.py -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现最小切换逻辑与结果输出。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_switch_command.py -q`
Expected: PASS

### Task 5: 增加 migrate-from-legacy

**Files:**
- Modify: `src/cproxy/install.py`
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_migrate_from_legacy.py`

**Step 1: Write the failing test**

断言：

- 能从旧目录复制配置到新用户目录
- 不迁移日志、PID、临时文件
- 缺少旧配置时给出明确错误

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_migrate_from_legacy.py -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- 旧目录检测
- 配置迁移
- 最小结果反馈

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_migrate_from_legacy.py -q`
Expected: PASS

### Task 6: 迁移 render/start/stop/restart

**Files:**
- Create: `src/cproxy/runtime.py`
- Create: `src/cproxy/process.py`
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_runtime_and_process.py`

**Step 1: Write the failing test**

覆盖：

- `render`
- `start`
- `stop`
- `restart`
- PID、日志、运行配置路径

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime_and_process.py -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- 运行配置生成
- 进程启动与停止
- 状态检测

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime_and_process.py -q`
Expected: PASS

### Task 7: 增加安装脚本与 pipx 体验

**Files:**
- Create: `scripts/install.sh`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `USAGE.md`
- Test: `tests/test_install_script.sh`

**Step 1: Write the failing test**

断言：

- 安装脚本会检查 `python3` 与 `pipx`
- 安装脚本会初始化用户目录
- 安装脚本会输出后续使用指引

**Step 2: Run test to verify it fails**

Run: `bash tests/test_install_script.sh`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- `pipx` 安装/更新流程
- 目录初始化
- 默认配置生成

**Step 4: Run test to verify it passes**

Run: `bash tests/test_install_script.sh`
Expected: PASS

### Task 8: 收口文档与迁移说明

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `TROUBLESHOOTING.md`
- Modify: `docs/plans/2026-04-09-cproxy-distribution-design.md`

**Step 1: Update docs**

将文档入口统一切换为 `cproxy`，明确：

- 用户级目录
- 安装方式
- 迁移方式
- 默认输出与 `--raw`

**Step 2: Verify docs consistency**

Run: `rg -n "proxy\\.sh|/root/clash_proxy|pipx|cproxy init|migrate-from-legacy" README.md USAGE.md TROUBLESHOOTING.md`
Expected: 旧入口只在迁移说明中保留。

### Task 9: 全量验证

**Files:**
- Modify: 无
- Test: 新增 Python 测试与保留的 shell 测试

**Step 1: Run full test suite**

Run: `pytest tests -q`
Expected: PASS

**Step 2: Run install smoke check**

Run: `bash scripts/install.sh --help`
Expected: PASS

**Step 3: Commit**

```bash
git add pyproject.toml src/cproxy scripts/install.sh tests README.md USAGE.md TROUBLESHOOTING.md docs/plans/2026-04-09-cproxy-distribution-design.md docs/plans/2026-04-09-cproxy-distribution-implementation.md
git commit -m "feat: introduce distributable cproxy cli"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
