# Usage Cleanup Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `--help` 重构为按使用场景分组的帮助输出，并收敛 `proxy.sh` 内重复的节点名展示规整逻辑，降低维护成本。

**Architecture:** `usage` 仅重排帮助文案结构，不改命令语义；节点名规整逻辑抽到一个共享的 Python 代码片段生成函数，由各个内嵌 Python 片段复用。行为层通过现有测试和新增 help 结构测试覆盖，重构不额外引入依赖。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 usage 分组输出建立回归测试

**Files:**
- Create: `tests/usage_output_test.sh`
- Test: `tests/usage_output_test.sh`

**Step 1: Write the failing test**

编写测试脚本，断言 `./proxy.sh --help` 输出包含新的场景分组：

- `配置与进程`
- `AI 路由控制`
- `命令级代理`
- `诊断与排障`

同时保留现有核心命令和示例。

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/usage_output_test.sh"`
Expected: FAIL，因为当前帮助仍是旧的平铺结构。

### Task 2: 实现 usage 新结构

**Files:**
- Modify: `proxy.sh`
- Test: `tests/usage_output_test.sh`

**Step 1: Write minimal implementation**

按使用场景重排 `usage()`：

- 配置与进程
- AI 路由控制
- 命令级代理
- 诊断与排障

示例也按场景组织，避免单块堆叠。

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/usage_output_test.sh"`
Expected: PASS

### Task 3: 收敛重复展示逻辑

**Files:**
- Modify: `proxy.sh`
- Test: `tests/current_switch_color_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/ai_status_test.sh`

**Step 1: Refactor**

新增一个共享的 Bash 函数，用于生成 Python 侧的 `display_name()` 定义，替代脚本内多处重复的内嵌实现。

**Step 2: Run regression tests**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh" && bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh"`
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
git add docs/plans/2026-04-09-usage-cleanup-output.md tests/usage_output_test.sh proxy.sh
git commit -m "refactor: clean up cli output helpers"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
