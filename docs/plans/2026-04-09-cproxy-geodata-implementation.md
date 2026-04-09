# cproxy Geodata Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 `country.mmdb` 增加用户级 geodata 检查、安装提示和运行前诊断。

**Architecture:** 新增 geodata 检查模块统一解析路径和诊断结果；安装脚本只负责调用检查并打印提示；`cproxy test` 增加 geodata 检查项；文档同步说明依赖与手动处理方式。

**Tech Stack:** Python 3.11, Bash, pytest

---

### Task 1: 增加 geodata 失败测试

**Files:**
- Create: `tests/test_geodata.py`
- Modify: `tests/test_diagnostics_and_proxy_commands.py`
- Modify: `tests/test_install_script.py`

**Step 1: Write the failing test**

- 为 geodata 路径解析和缺失场景写测试
- 为 `cproxy test` 缺失 geodata 时的失败输出写测试
- 为安装脚本缺失 geodata 时打印警告写测试

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_geodata.py tests/test_diagnostics_and_proxy_commands.py tests/test_install_script.py -q`

### Task 2: 实现 geodata 检查模块

**Files:**
- Create: `src/cproxy/geodata.py`
- Modify: `src/cproxy/config.py`

**Step 1: Write minimal implementation**

- 增加 geodata 候选路径解析
- 增加是否启用 `GEOIP,CN` 的判断
- 增加可读诊断结果对象

**Step 2: Run focused tests**

Run: `python3 -m pytest tests/test_geodata.py -q`

### Task 3: 接入 diagnostics 和 install

**Files:**
- Modify: `src/cproxy/services/diagnostics.py`
- Modify: `src/cproxy/backend/models.py`
- Modify: `src/cproxy/cli.py`
- Modify: `scripts/install.sh`

**Step 1: Write minimal implementation**

- `cproxy test` 增加 geodata 检查结果
- 安装脚本在初始化后执行 geodata 检查并打印状态

**Step 2: Run focused tests**

Run: `python3 -m pytest tests/test_diagnostics_and_proxy_commands.py tests/test_install_script.py -q`

### Task 4: 更新文档并跑回归

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `TROUBLESHOOTING.md`

**Step 1: Update docs**

- 说明 `country.mmdb` 依赖、常见路径和受限网络提示

**Step 2: Run verification**

Run: `python3 -m pytest tests/test_geodata.py tests/test_api_backend.py tests/test_backend_services.py tests/test_cli_bootstrap.py tests/test_init_command.py tests/test_query_commands.py tests/test_switch_command.py tests/test_migrate_from_legacy.py tests/test_runtime_and_process.py tests/test_diagnostics_and_proxy_commands.py tests/test_install_script.py tests/test_logs_command.py -q`

Run: `bash tests/systemd_user_examples_test.sh`

Run: `bash tests/systemd_user_helper_scripts_test.sh`

Run: `bash tests/proxy_env_test.sh`
