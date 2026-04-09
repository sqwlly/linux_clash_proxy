# AI 状态 OpenAI 探测 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 `cproxy ai-status` 默认展示 `chatgpt.com` 与 `api.openai.com` 的合并探测结果。

**Architecture:** 保持现有 `ai-status` 的路由展示逻辑不变，在展示层前新增一个轻量探测步骤。探测通过本地代理发起 HTTP 请求，返回汇总状态与逐项结果，再由 `cli.py` 统一渲染。

**Tech Stack:** Python, argparse, urllib.request, pytest

---

### Task 1: 为 ai-status 增加失败测试

**Files:**
- Modify: `tests/test_query_commands.py`
- Test: `tests/test_query_commands.py`

**Step 1: Write the failing test**

为 `ai-status` 新增断言，要求输出包含：

- `AI 探测:`
- `OpenAI 连通性`
- `ChatGPT Web`
- `OpenAI API`

并覆盖一个目标成功、一个目标失败的场景。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_query_commands.py -q`
Expected: FAIL，因为 `ai-status` 还未输出 OpenAI 探测结果。

**Step 3: Write minimal implementation**

在服务层增加 OpenAI 探测函数，并在 `ai-status` 中调用。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_query_commands.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_query_commands.py src/cproxy/cli.py src/cproxy/services/diagnostics.py src/cproxy/diagnostics.py
git commit -m "feat: add openai probes to ai-status"
```

### Task 2: 补充实现细节与文档

**Files:**
- Modify: `src/cproxy/services/diagnostics.py`
- Modify: `src/cproxy/diagnostics.py`
- Modify: `src/cproxy/cli.py`
- Modify: `README.md`
- Modify: `USAGE.md`
- Test: `tests/test_query_commands.py`

**Step 1: Write the failing doc expectations**

补充文档描述，明确 `ai-status` 同时展示路由状态与 OpenAI 探测结果。

**Step 2: Run focused tests**

Run: `pytest tests/test_query_commands.py -q`
Expected: PASS

**Step 3: Write minimal implementation**

- 定义两个默认探测目标
- 输出汇总状态 `正常 / 部分异常 / 失败`
- 输出逐项明细

**Step 4: Run broader verification**

Run: `pytest tests/test_query_commands.py tests/test_diagnostics_and_proxy_commands.py tests/test_runtime_and_process.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add README.md USAGE.md
git commit -m "docs: describe ai-status openai probes"
```
