# AI Status Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `./proxy.sh ai-status` 改造成“摘要 + 当前链路 + 备用路径 + 分组状态”的终端输出，提升可读性与排障效率。

**Architecture:** 保持现有 `ai_status()` 的 API 获取方式不变，只替换内嵌 Python 的格式化逻辑。新增一个 shell 回归测试，通过本地临时 HTTP 服务模拟 Mihomo API，验证关键输出结构，确保行为先失败后通过。

**Tech Stack:** Bash、Python 3、现有 shell 测试脚本

---

### Task 1: 为 ai-status 建立回归测试

**Files:**
- Create: `tests/ai_status_test.sh`
- Modify: 无
- Test: `tests/ai_status_test.sh`

**Step 1: Write the failing test**

编写测试脚本，启动一个本地临时 HTTP 服务，返回固定的 `/version` 与 `/proxies` 数据，然后断言 `./proxy.sh ai-status` 输出包含：

- 顶部摘要行 `AI 路由:`
- `当前链路`
- `备用路径`
- `分组状态`

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/ai_status_test.sh"`
Expected: FAIL，因为当前实现仍是扁平键值输出。

**Step 3: Write minimal implementation**

不改测试，只准备好实现所需的输出结构和文案。

**Step 4: Run test to verify it still captures the gap**

Run: `bash "/root/clash_proxy/tests/ai_status_test.sh"`
Expected: FAIL，且失败原因仍然是缺少新结构字段。

### Task 2: 改造 ai-status 输出

**Files:**
- Modify: `proxy.sh`
- Test: `tests/ai_status_test.sh`

**Step 1: Write minimal implementation**

在 `ai_status()` 内的 Python 逻辑中：

- 解析 `AI-MANUAL`、`AI-AUTO`、`AI-US`、`AI-SG`、区域组与节点信息
- 计算当前手动入口是否处于自动模式
- 识别当前实际区域与节点
- 输出摘要、链路、备用路径、分组状态四个区块

**Step 2: Run test to verify it passes**

Run: `bash "/root/clash_proxy/tests/ai_status_test.sh"`
Expected: PASS

**Step 3: Refactor**

在内嵌 Python 中提炼小函数，避免重复读取 group 字段；保持输出逻辑简单直接，不做过度抽象。

**Step 4: Run test to verify it stays green**

Run: `bash "/root/clash_proxy/tests/ai_status_test.sh"`
Expected: PASS

### Task 3: 回归现有测试

**Files:**
- Modify: 无
- Test: `tests/proxy_env_test.sh`
- Test: `tests/render_rules_test.sh`

**Step 1: Run regression tests**

Run: `bash "/root/clash_proxy/tests/proxy_env_test.sh" && bash "/root/clash_proxy/tests/render_rules_test.sh"`
Expected: PASS

**Step 2: Verify no unintended regressions**

确认输出改造没有影响现有命令帮助和渲染逻辑。

**Step 3: Commit**

```bash
git add docs/plans/2026-04-09-ai-status-design.md docs/plans/2026-04-09-ai-status-output.md tests/ai_status_test.sh proxy.sh
git commit -m "feat: improve ai-status output"
```

注意：`git commit` 属于高风险操作，执行前必须得到用户明确确认。
