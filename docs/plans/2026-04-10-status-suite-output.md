# Status Suite Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 统一 `status`、`ai-status`、`list-groups`、`list-nodes`、`test-group`、`test` 的默认终端输出风格，同时保持 `--raw` 稳定不变。

**Architecture:** 这次改动只作用于展示层，不调整后端查询与探测逻辑。Python CLI 与 `proxy.sh` 需要同时更新，统一采用“摘要区 + 明细区”的输出结构；测试分为 Python CLI 回归和 shell 回归两条线，确保双入口行为一致。

**Tech Stack:** Python 3、Bash、pytest、现有 shell 测试脚本

---

### Task 1: 为统一输出补充失败测试

**Files:**
- Modify: `tests/test_query_commands.py`
- Modify: `tests/test_diagnostics_and_proxy_commands.py`
- Modify: `tests/ai_status_test.sh`
- Modify: `tests/status_list_groups_test.sh`
- Modify: `tests/list_nodes_test_group_test.sh`
- Test: `tests/test_query_commands.py`
- Test: `tests/test_diagnostics_and_proxy_commands.py`
- Test: `tests/ai_status_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`

**Step 1: Write the failing test**

补充断言，要求默认输出统一包含目标区块标题，例如：

- `摘要`
- `资源`
- `路径`
- `连通性`
- `链路`
- `结果`

并验证 `--raw` 输出不引入这些标题。

**Step 2: Run test to verify it fails**

Run: `"/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" -q`
Expected: FAIL，因为默认输出还未统一。

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh"`
Expected: FAIL，因为 shell 输出仍含旧标题或缺少新区块。

**Step 3: Write minimal implementation**

仅准备实现所需的标题与结构，不扩展行为。

**Step 4: Run test to verify it still fails for the right reason**

Run: 同上
Expected: FAIL，且失败原因与展示结构相关。

**Step 5: Commit**

```bash
git add tests/test_query_commands.py tests/test_diagnostics_and_proxy_commands.py tests/ai_status_test.sh tests/status_list_groups_test.sh tests/list_nodes_test_group_test.sh
git commit -m "test: cover status suite output layout"
```

### Task 2: 改造 Python CLI 输出

**Files:**
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_query_commands.py`
- Test: `tests/test_diagnostics_and_proxy_commands.py`
- Test: `tests/test_runtime_and_process.py`

**Step 1: Write minimal implementation**

统一以下命令的默认输出结构：

- `status`
- `ai-status`
- `list-groups`
- `list-nodes`
- `test-group`
- `test`

规则：

- 第一块总是 `摘要`
- 后续块按命令语义命名
- `--raw` 逻辑不变

**Step 2: Run focused tests**

Run: `"/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: PASS

**Step 3: Refactor**

提炼小型渲染辅助函数，避免重复 `print()` 和状态标签逻辑，但不要做过度抽象。

**Step 4: Run tests to verify it stays green**

Run: 同上
Expected: PASS

**Step 5: Commit**

```bash
git add src/cproxy/cli.py
git commit -m "feat: unify python cli status output"
```

### Task 3: 改造 proxy.sh 输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/ai_status_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Write minimal implementation**

将 `proxy.sh` 中对应命令改为与 Python CLI 相同的默认区块结构：

- 去掉混杂的 `=== 标题 ===`
- 统一为 `摘要 / 资源 / 路径 / 连通性 / 链路 / 结果 / 列表`
- 保留 `--raw` 老格式

**Step 2: Run shell tests**

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: PASS

**Step 3: Refactor**

提炼 shell 内嵌 Python 的共用格式逻辑，减少文案漂移点。

**Step 4: Run shell tests to verify it stays green**

Run: 同上
Expected: PASS

**Step 5: Commit**

```bash
git add proxy.sh
git commit -m "feat: unify proxy.sh status output"
```

### Task 4: 更新文档并做总回归

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `TROUBLESHOOTING.md`
- Test: `tests/test_query_commands.py`
- Test: `tests/test_diagnostics_and_proxy_commands.py`
- Test: `tests/test_runtime_and_process.py`
- Test: `tests/ai_status_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Write minimal documentation**

说明统一后的区块结构与 `--raw` 不变原则。

**Step 2: Run full verification**

Run: `"/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q && bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: PASS

**Step 3: Commit**

```bash
git add README.md USAGE.md TROUBLESHOOTING.md docs/plans/2026-04-10-status-suite-design.md docs/plans/2026-04-10-status-suite-output.md
git commit -m "docs: describe unified status output"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
