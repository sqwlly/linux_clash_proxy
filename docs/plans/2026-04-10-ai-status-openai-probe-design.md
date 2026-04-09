# AI 状态 OpenAI 探测设计

**目标**

将 `cproxy ai-status` 扩展为“AI 路由状态 + OpenAI 实际可达性探测”，默认覆盖日常 GPT Web 与 Codex/API 使用场景。

**问题**

当前 `ai-status` 只展示 Mihomo API 返回的代理组 `alive/delay` 状态，但这只能说明路由组健康，不能直接回答：

- `chatgpt.com` 是否真的可达
- `api.openai.com` 是否真的可达

这会导致用户看到 `AI-US` 或 `AI-SG` 正常，却仍不确定 GPT/Codex 是否可用。

**设计原则**

- KISS：只探测最核心的两个目标，不引入额外命令与复杂配置。
- 语义清晰：把“路由状态”和“OpenAI 探测”分开展示，避免混淆 `alive/delay` 与真实连通性。
- 默认可用：不新增命令参数，直接并入 `ai-status`。
- YAGNI：暂不探测控制台、登录页、静态资源等非核心域名。

**探测范围**

- `ChatGPT Web` -> `https://chatgpt.com`
- `OpenAI API` -> `https://api.openai.com`

**目标输出**

```text
AI 路由: 自动切换  当前出口=United States 01  区域=AI-US  延迟=95ms  状态=正常
AI 探测: 部分异常

OpenAI 连通性
正常  ChatGPT Web  https://chatgpt.com
失败  OpenAI API   https://api.openai.com
```

其中：

- 两个目标都成功 -> `AI 探测: 正常`
- 仅部分成功 -> `AI 探测: 部分异常`
- 全部失败 -> `AI 探测: 失败`

**实现范围**

- 为 `ai-status` 增加 OpenAI 探测结果汇总与明细输出
- 复用现有代理端口，通过本地代理发起探测
- 新增 Python 回归测试覆盖成功与部分失败场景
- 更新 README / USAGE 文档

**非目标**

- 不新增 `ai-status --probe`
- 不修改 `status` 或 `test` 的行为
- 不引入并发请求、重试策略、缓存或持久化结果
