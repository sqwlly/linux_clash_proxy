# List-Nodes And Test-Group Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 优化 `list-nodes` 与 `test-group` 的输出结构，让节点列表和健康检查都先给结论，再给明细。

**Architecture:** 保持现有 API 获取与延迟探测逻辑不变，只重构展示层。`list-nodes` 改为“当前选择 + 候选列表”结构，`test-group` 改为“检查摘要 + 检查结果”结构。新增独立 shell 回归测试，通过临时 HTTP 服务模拟 `/proxies` 和 `/proxies/<name>/delay` 响应，覆盖关键展示字段。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 list-nodes 和 test-group 建立回归测试

**Files:**
- Create: `tests/list_nodes_test_group_test.sh`
- Test: `tests/list_nodes_test_group_test.sh`

**Step 1: Write the failing test**

编写测试脚本，启动本地临时 HTTP 服务，返回固定的 `/version`、`/proxies` 与 `/proxies/<name>/delay` 数据，然后断言：

- `list-nodes` 输出包含 `当前选择`
- `list-nodes` 输出包含 `候选列表`
- `test-group` 输出包含 `检查摘要`
- `test-group` 输出包含 `检查结果`
- `test-group` 输出包含最佳节点与可用统计

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh"`
Expected: FAIL，因为当前实现仍是旧格式。

### Task 2: 实现 list-nodes 新输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/list_nodes_test_group_test.sh`

**Step 1: Write minimal implementation**

在 API 可用时：

- 顶部展示 `当前选择: ...`
- 空行后输出 `候选列表`
- 每个候选项标注 `当前` 或 `候选`
- 节点名走与现有命令一致的规整规则

静态回退时保持最小可读结构，不尝试伪造当前选择。

**Step 2: Run test to verify it progresses**

Run: `bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh"`
Expected: `list-nodes` 相关断言通过，若 `test-group` 尚未改造则整体仍 FAIL。

### Task 3: 实现 test-group 新输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/list_nodes_test_group_test.sh`

**Step 1: Write minimal implementation**

将健康检查改为：

- `检查摘要`
- `目标组`
- `可用`
- `最佳`
- `最慢`
- 空行后输出 `检查结果`

每个成员显示规整后的名称、延迟或失败状态。

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/list_nodes_test_group_test.sh"`
Expected: PASS

### Task 4: 回归现有测试

**Files:**
- Modify: 无
- Test: `tests/status_list_groups_test.sh`
- Test: `tests/ai_status_test.sh`
- Test: `tests/proxy_env_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/status_list_groups_test.sh" && bash "/root/clash_proxy/tests/ai_status_test.sh" && bash "/root/clash_proxy/tests/proxy_env_test.sh"`
Expected: PASS

**Step 2: Commit**

```bash
git add docs/plans/2026-04-09-list-nodes-test-group-output.md tests/list_nodes_test_group_test.sh proxy.sh
git commit -m "feat: improve list-nodes and test-group output"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
