# Status And List-Groups Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 优化 `status` 与 `list-groups` 的输出结构，让常用运维命令先给结论，再给细节，降低扫读成本。

**Architecture:** `status` 保持现有运行态检查链路不变，只重排输出为“运行摘要 / 连接与资源 / 配置路径”。`list-groups` 在 API 可用时展示“组名 / 类型 / 当前选择”，API 不可用时回退到静态配置，当前选择显示为 `-`。新增独立 shell 测试，通过临时 HTTP 服务和命令桩模拟运行态，覆盖关键文案与表格结构。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 status 和 list-groups 建立回归测试

**Files:**
- Create: `tests/status_list_groups_test.sh`
- Test: `tests/status_list_groups_test.sh`

**Step 1: Write the failing test**

编写测试脚本，构造：

- 临时 `runtime.yaml`
- 临时 HTTP API 服务，返回 `/version` 与 `/proxies`
- 命令桩 `ps`、`ss`、`du`，模拟稳定运行态
- 带 `-f <config>` 参数的后台 `bash` 进程，供 `status` 识别为“运行中”

断言：

- `status` 输出包含 `运行摘要`、`连接与资源`、`配置路径`
- `status` 输出包含 `AI 当前出口`
- `list-groups` 输出包含表头 `组名`、`类型`、`当前选择`
- `list-groups` 输出包含 AI 组与当前选择

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh"`
Expected: FAIL，因为当前实现仍是旧格式。

### Task 2: 实现 status 新输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/status_list_groups_test.sh`

**Step 1: Write minimal implementation**

将 `status` 改为三个区块：

- `运行摘要`
- `连接与资源`
- `配置路径`

在 API 可用时补充 `AI 当前出口=<区域> -> <节点>`。

**Step 2: Run test to verify it passes relevant assertions**

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh"`
Expected: 至少 `status` 相关断言通过，若 `list-groups` 仍未改造则整体仍 FAIL。

### Task 3: 实现 list-groups 新输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/status_list_groups_test.sh`

**Step 1: Write minimal implementation**

在 API 可用时输出三列表格：

- `组名`
- `类型`
- `当前选择`

静态回退时 `当前选择` 显示为 `-`。

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh"`
Expected: PASS

### Task 4: 回归现有测试

**Files:**
- Modify: 无
- Test: `tests/ai_status_test.sh`
- Test: `tests/proxy_env_test.sh`
- Test: `tests/render_rules_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/proxy_env_test.sh" && bash "/root/clash_proxy/tests/render_rules_test.sh"`
Expected: PASS

**Step 2: Commit**

```bash
git add docs/plans/2026-04-09-status-list-groups-output.md tests/status_list_groups_test.sh proxy.sh
git commit -m "feat: improve status and list-groups output"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
