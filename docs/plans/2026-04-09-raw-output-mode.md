# Raw Output Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为高频查询命令补充 `--raw` 输出模式，兼容脚本消费场景，同时保留现有人类友好的默认输出。

**Architecture:** 在各命令内新增一个可选参数 `--raw`，只影响展示层，不改变现有 API 或运行逻辑。`--raw` 模式返回接近旧版的纯值或纯列表格式，默认模式保持当前摘要式输出。通过独立 shell 测试覆盖 `current`、`list-nodes`、`list-groups`、`status`、`ai-status`、`test-group` 的原始输出行为。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 --raw 模式建立回归测试

**Files:**
- Create: `tests/raw_output_mode_test.sh`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Write the failing test**

编写测试脚本，构造临时 API 服务和最小运行态，断言：

- `current <group> --raw` 只输出当前值
- `list-nodes <group> --raw` 输出纯候选列表
- `list-groups --raw` 输出制表分隔的旧格式
- `status --raw` 输出旧式字段列表
- `ai-status --raw` 输出旧式平铺状态
- `test-group <group> --raw` 输出逐行结果，不含摘要区块

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: FAIL，因为当前尚不支持 `--raw`。

### Task 2: 实现 --raw 模式

**Files:**
- Modify: `proxy.sh`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Write minimal implementation**

为以下命令增加可选参数：

- `status [--raw]`
- `list-groups [--raw]`
- `list-nodes <group> [--raw]`
- `current <group> [--raw]`
- `ai-status [--raw]`
- `test-group <group> [--raw]`

默认输出保持不变，`--raw` 走旧格式或纯值格式。

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: PASS

### Task 3: 回归现有测试

**Files:**
- Modify: 无
- Test: `tests/current_switch_color_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/ai_status_test.sh`
- Test: `tests/usage_output_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh" && bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/usage_output_test.sh"`
Expected: PASS

### Task 4: 全量回归

**Files:**
- Modify: 无
- Test: `tests/proxy_env_test.sh`
- Test: `tests/render_rules_test.sh`
- Test: `tests/review_fixes_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/proxy_env_test.sh" && bash "/root/clash_proxy/tests/render_rules_test.sh" && bash "/root/clash_proxy/tests/review_fixes_test.sh"`
Expected: PASS

**Step 2: Commit**

```bash
git add docs/plans/2026-04-09-raw-output-mode.md tests/raw_output_mode_test.sh proxy.sh
git commit -m "feat: add raw output mode"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
