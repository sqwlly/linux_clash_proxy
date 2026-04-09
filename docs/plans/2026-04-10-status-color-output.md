# Status Color Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为状态相关命令增加可控着色，提升层级感，同时保持非 TTY 和 `--raw` 输出稳定。

**Architecture:** 颜色只属于展示层。Python CLI 新增一组轻量样式辅助函数；`proxy.sh` 复用现有 ANSI 变量并补足 `CYAN/BOLD` 语义。通过 `FORCE_COLOR=1` 触发测试，确保默认捕获输出仍然无颜色。

**Tech Stack:** Python 3、Bash、pytest、现有 shell 测试脚本

---

### Task 1: 补充失败测试

**Files:**
- Modify: `tests/current_switch_color_test.sh`
- Modify: `tests/test_switch_command.py`
- Test: `tests/current_switch_color_test.sh`
- Test: `tests/test_switch_command.py`

**Step 1: Write the failing test**

- shell 侧增加 `FORCE_COLOR=1` 断言，验证 `current` / `switch` 输出出现 ANSI 转义
- Python CLI 增加 `FORCE_COLOR=1` 断言，验证 `switch` 输出出现 ANSI 转义

**Step 2: Run test to verify it fails**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh" && "/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_switch_command.py" -q`
Expected: FAIL

### Task 2: 实现 Python CLI 着色

**Files:**
- Modify: `src/cproxy/cli.py`
- Test: `tests/test_switch_command.py`

**Step 1: Write minimal implementation**

- 增加颜色启用判断
- 区块标题蓝色加粗
- 状态词与结果词按语义着色
- `current` / `switch` 的关键值使用青色

**Step 2: Run tests**

Run: `"/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_switch_command.py" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q`
Expected: PASS

### Task 3: 实现 proxy.sh 着色

**Files:**
- Modify: `proxy.sh`
- Test: `tests/current_switch_color_test.sh`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Write minimal implementation**

- 增加 `CYAN` 与 `BOLD`
- 统一区块标题和状态词着色
- `FORCE_COLOR=1` 生效
- `--raw` 保持无颜色

**Step 2: Run tests**

Run: `bash "/root/clash_proxy/tests/current_switch_color_test.sh" && bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: PASS

### Task 4: 更新文档并做总回归

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Test: `tests/current_switch_color_test.sh`
- Test: `tests/test_switch_command.py`
- Test: `tests/raw_output_mode_test.sh`

**Step 1: Update docs**

写明默认仅 TTY 开启着色，`FORCE_COLOR=1` 可强制启用。

**Step 2: Run full verification**

Run: `"/root/clash_proxy/.venv/bin/pytest" "/root/clash_proxy/tests/test_switch_command.py" "/root/clash_proxy/tests/test_query_commands.py" "/root/clash_proxy/tests/test_diagnostics_and_proxy_commands.py" "/root/clash_proxy/tests/test_runtime_and_process.py" -q && bash "/root/clash_proxy/tests/current_switch_color_test.sh" && bash "/root/clash_proxy/tests/raw_output_mode_test.sh"`
Expected: PASS
