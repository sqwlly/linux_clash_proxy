# cproxy Backend Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `cproxy` 的 API、runtime、process 访问统一到 backend/service 分层，瘦身 CLI，同时保持现有命令行为与测试结果稳定。

**Architecture:** 先引入统一 backend 数据模型，再分阶段迁移 API/runtime/process 读取与编排逻辑，最后让 `cli.py` 只负责参数解析和输出。整个过程按 TDD 执行，保证每一步都是可验证的纯重构。

**Tech Stack:** Python 3.11、PyYAML、现有 pytest 测试集、现有 shell 回归测试

---

### Task 1: 建立 backend 数据模型

**Files:**
- Create: `src/cproxy/backend/models.py`
- Modify: `tests/test_query_commands.py`

**Step 1: Write the failing test**

补一个最小测试，要求 service/backend 返回统一 `name/current/candidates/source` 结构，不允许 API/runtime 两套字段名并存。

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" -q`
Expected: FAIL，因为统一模型尚不存在。

**Step 3: Write minimal implementation**

实现：

- `ProxyGroup`
- `QueryContext`
- `ProcessOwner`

只做数据结构，不迁移行为。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" -q`
Expected: PASS

### Task 2: 迁移 API backend

**Files:**
- Create: `src/cproxy/backend/api.py`
- Modify: `src/cproxy/api.py`
- Modify: `tests/test_query_commands.py`
- Modify: `tests/test_diagnostics_and_proxy_commands.py`

**Step 1: Write the failing test**

要求：

- API backend 返回统一 `ProxyGroup`
- API 不可达时继续抛统一可读错误

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

把下列职责迁入 `backend/api.py`：

- controller URL
- secret
- request
- group 读取
- switch
- delay test

原 `src/cproxy/api.py` 只保留兼容转发或删除后修正引用。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: PASS

### Task 3: 迁移 runtime backend

**Files:**
- Create: `src/cproxy/backend/runtime.py`
- Modify: `src/cproxy/runtime.py`
- Modify: `src/cproxy/runtime_state.py`
- Modify: `tests/test_query_commands.py`
- Modify: `tests/test_runtime_and_process.py`

**Step 1: Write the failing test**

要求：

- runtime backend 返回统一 `ProxyGroup`
- `render_runtime` 仍保持原行为
- API 不可达时查询命令仍能通过 runtime 回退

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

迁移：

- runtime group 读取
- runtime group 统一模型转换
- 保持渲染逻辑仍在 runtime backend

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: PASS

### Task 4: 迁移 process backend

**Files:**
- Create: `src/cproxy/backend/process.py`
- Modify: `src/cproxy/process.py`
- Modify: `tests/test_runtime_and_process.py`

**Step 1: Write the failing test**

要求：

- ownership 校验仍成立
- stale pidfile 不误杀
- status/start/stop/restart 行为不变

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

迁移：

- status snapshot
- process meta
- ownership 校验
- start/stop/restart/status

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: PASS

### Task 5: 引入 query service

**Files:**
- Create: `src/cproxy/services/query.py`
- Modify: `src/cproxy/cli.py`
- Modify: `tests/test_query_commands.py`

**Step 1: Write the failing test**

要求：

- `current/list-groups/list-nodes` 通过 service 完成 API 优先、runtime 回退
- `ai-status` 仍为 API-only

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- `load_query_context()`
- `get_current_group()`
- `list_groups()`
- `list_nodes()`
- `get_ai_status_groups()`

CLI 不再直接写回退逻辑。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_query_commands.py" -q`
Expected: PASS

### Task 6: 引入 diagnostics service

**Files:**
- Create: `src/cproxy/services/diagnostics.py`
- Modify: `src/cproxy/diagnostics.py`
- Modify: `src/cproxy/cli.py`
- Modify: `tests/test_diagnostics_and_proxy_commands.py`

**Step 1: Write the failing test**

要求：

- `test`
- `test-group`

两者都通过 service 编排，不再直接由 CLI 组合细节。

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

实现：

- `run_connectivity_diagnostics()`
- `run_group_diagnostics()`

CLI 只负责渲染结果。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: PASS

### Task 7: 瘦身 CLI 并清理旧模块

**Files:**
- Modify: `src/cproxy/cli.py`
- Modify: `src/cproxy/api.py`
- Modify: `src/cproxy/process.py`
- Modify: `src/cproxy/runtime_state.py`
- Test: `tests/test_cli_bootstrap.py`
- Test: `tests/test_query_commands.py`
- Test: `tests/test_runtime_and_process.py`
- Test: `tests/test_diagnostics_and_proxy_commands.py`

**Step 1: Write the failing test**

要求：

- CLI 仍暴露相同命令
- 行为无回归
- 旧模块只保留必要兼容或被移除

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_cli_bootstrap.py" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: FAIL

**Step 3: Write minimal implementation**

瘦身 `cli.py`：

- 只保留参数解析
- 调 service/backend
- 输出结果
- 统一异常处理

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest "/root/clash_proxy/tests/test_cli_bootstrap.py" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: PASS

### Task 8: 跑全量回归并同步文档

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `TROUBLESHOOTING.md`
- Test: `tests/systemd_user_examples_test.sh`
- Test: `tests/systemd_user_helper_scripts_test.sh`
- Test: `tests/proxy_env_test.sh`

**Step 1: Write the failing test**

如文档与实现不一致，先补文档断言或发现现有断言失败。

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/systemd_user_examples_test.sh"`
Expected: 如有不一致则 FAIL。

**Step 3: Write minimal implementation**

同步文档：

- backend/service 结构
- API 回退边界
- ownership 行为

**Step 4: Run test to verify it passes**

Run:
- `python3 -m pytest "/root/clash_proxy/tests/test_cli_bootstrap.py" "/root/clash_proxy/tests/test_init_command.py" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_switch_command.py" "/root/clash_proxy/tests/test_migrate_from_legacy.py" "/root/clash_proxy/tests/test_runtime_and_process.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" "/root/clash_proxy/tests/test_install_script.py" "/root/clash_proxy/tests/test_logs_command.py" -q`
- `bash "/root/clash_proxy/tests/systemd_user_examples_test.sh"`
- `bash "/root/clash_proxy/tests/systemd_user_helper_scripts_test.sh"`
- `bash "/root/clash_proxy/tests/proxy_env_test.sh"`

Expected: 全部 PASS
