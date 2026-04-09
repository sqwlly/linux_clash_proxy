# cproxy 可分发工具设计

**目标**

将当前仓库内以 `proxy.sh` 为核心、默认绑定 `/root/clash_proxy` 的单机脚本，重构为一个面向用户级安装与分发的 CLI 工具 `cproxy`，支持：

- `pipx install` 作为正式安装入口
- 本地 `install.sh` 作为简化安装入口
- 用户级配置、运行数据和日志目录
- 从旧仓库目录迁移到新目录布局

**范围**

本次设计聚焦：

- CLI 入口与包结构
- 用户级目录与配置约定
- 安装与迁移路径
- 迁移顺序与测试策略

不在本次设计内：

- 系统级部署默认支持
- 旧 `./proxy.sh` 兼容层
- 一次性迁完全部 systemd 能力

## 一、目标形态

最终工具形态：

- 包名：`cproxy`
- 全局命令：`cproxy`
- 默认安装方式：
  - `pipx install ...`
  - `scripts/install.sh`

默认目录遵循用户级 XDG 思路：

- 配置：`~/.config/cproxy/`
- 运行数据：`~/.local/share/cproxy/`
- 状态与日志：`~/.local/state/cproxy/`

建议关键文件：

- `~/.config/cproxy/config.yaml`
- `~/.local/share/cproxy/runtime.yaml`
- `~/.local/state/cproxy/cproxy.log`
- `~/.local/state/cproxy/cproxy.pid`

## 二、架构设计

主 CLI 迁移到 Python，shell 只保留在安装或少量辅助脚本层。

建议模块划分：

- `src/cproxy/cli.py`
  负责参数解析、子命令分发
- `src/cproxy/config.py`
  负责 XDG 路径、配置加载、默认值
- `src/cproxy/runtime.py`
  负责运行配置生成与 AI 路由增强
- `src/cproxy/process.py`
  负责启动、停止、状态、PID、日志
- `src/cproxy/api.py`
  负责 Mihomo API 请求、代理组读取、切换、延迟测试
- `src/cproxy/output.py`
  负责默认输出、`--raw` 输出、节点名规整
- `src/cproxy/install.py`
  负责用户级初始化、安装、自检与迁移入口

打包入口：

- `pyproject.toml`
  暴露 console script：`cproxy`

## 三、命令设计

首批保留并迁移现有高频命令：

- `init`
- `render`
- `start`
- `stop`
- `restart`
- `status`
- `ai-status`
- `list-groups`
- `list-nodes`
- `current`
- `switch`
- `test`
- `test-group`
- `proxy-env`
- `with-proxy`
- `proxy-shell`
- `migrate-from-legacy`

输出策略保持当前成果：

- 默认输出：面向人类阅读
- `--raw`：面向脚本消费
- 非 TTY：不输出颜色

## 四、安装与迁移设计

### 1. 用户安装

正式方式：

- `pipx install cproxy`

便捷方式：

- `scripts/install.sh`

安装脚本职责：

- 检查 `python3` 与 `pipx`
- 安装或更新 `cproxy`
- 初始化目录
- 生成默认配置模板
- 输出后续使用指引

### 2. 首次使用

推荐路径：

```bash
cproxy init
cproxy render
cproxy start
cproxy ai-status
```

### 3. 旧环境迁移

迁移命令：

```bash
cproxy migrate-from-legacy /root/clash_proxy
```

迁移内容：

- 迁移 `config.yaml`
- 初始化新的用户目录布局
- 迁移必要配置，不迁移旧日志、旧 PID、旧临时文件

## 五、实施原则

- KISS：先构建清晰的 Python CLI 骨架，再逐步迁移行为
- YAGNI：先做用户级模式，不同时引入系统级默认支持
- DRY：输出逻辑、路径逻辑、API 逻辑集中收敛
- SOLID：用模块划分替代 `proxy.sh` 中的大量内聚脚本块

## 六、实施顺序

建议分阶段进行：

1. 建立 Python 包骨架与 `cproxy` 入口
2. 迁移配置路径、输出层与查询类命令
3. 增加 `init` 与 `migrate-from-legacy`
4. 迁移渲染与进程控制
5. 增加安装脚本、更新文档、补用户级 systemd 示例

## 七、风险与约束

- 这是架构级重构，不适合一次性全迁，必须分阶段验证
- 进程控制与 API 行为必须保持与现有实践一致，否则会引入隐性运维回归
- 文档、测试和安装体验必须与代码迁移同步推进，不能等实现完再补
