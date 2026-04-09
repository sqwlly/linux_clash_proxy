# Current Switch Color Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 优化 `current` 与 `switch` 的人类可读输出，并在非 TTY 场景下禁用 ANSI 颜色转义，避免 `--help`、测试和管道场景出现原始转义字符。

**Architecture:** 保持现有 API 交互逻辑不变，只重构展示层。`current` 改为摘要式输出，`switch` 在切换成功后补充当前选择摘要；颜色控制集中在脚本顶部初始化，默认仅在交互终端启用。新增独立 shell 回归测试，通过临时 HTTP 服务模拟 `/proxies` 的读取与切换状态。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 current、switch 和颜色输出建立回归测试

**Files:**
- Create: `tests/current_switch_color_test.sh`
- Test: `tests/current_switch_color_test.sh`

**Step 1: Write the failing test**

编写测试脚本，启动临时 HTTP 服务，支持：

- `GET /version`
- `GET /proxies`
- `PUT /proxies/<group>`

断言：

- `current "AI-MANUAL"` 输出包含 `当前选择:`
- `current "AI-MANUAL"` 输出包含规整后的当前值
- `switch "AI-MANUAL" "AI-SG"` 输出包含切换摘要
- 切换后再次执行 `current`，应反映新状态
- `--help` 非 TTY 捕获输出中不应包含 ANSI 转义

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh"`
Expected: FAIL，因为当前实现仍输出裸值，且非 TTY 仍含颜色转义。

### Task 2: 实现非 TTY 颜色控制

**Files:**
- Modify: `proxy.sh`
- Test: `tests/current_switch_color_test.sh`

**Step 1: Write minimal implementation**

在脚本顶部增加颜色初始化逻辑：

- 交互终端启用颜色
- 非 TTY 置空颜色变量

**Step 2: Run test to verify help no longer leaks ANSI**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh"`
Expected: 至少颜色相关断言通过，`current/switch` 相关断言仍可能 FAIL。

### Task 3: 实现 current 与 switch 新输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/current_switch_color_test.sh`

**Step 1: Write minimal implementation**

将：

- `current` 改为输出 `当前选择: <value>`
- `switch` 改为输出 `切换结果`、`代理组`、`当前选择`

名称规整规则与现有命令保持一致。

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh"`
Expected: PASS

### Task 4: 回归现有测试

**Files:**
- Modify: 无
- Test: `tests/list_nodes_test_group_test.sh`
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/ai_status_test.sh`
- Test: `tests/proxy_env_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh" && bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/proxy_env_test.sh"`
Expected: PASS

**Step 2: Commit**

```bash
git add docs/plans/2026-04-09-current-switch-color-output.md tests/current_switch_color_test.sh proxy.sh
git commit -m "feat: improve current and switch output"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
