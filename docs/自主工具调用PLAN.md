# 添加工具调用功能开发计划

## Summary

将当前固定顺序研究流程升级为“JSON fallback模型自主选择工具”的受控执行模式。模型输出受控 JSON action，后端校验后执行工具。

第一版开放现有能力对应的全部工具动作：搜索、任务总结、保存笔记、报告生成。记忆系统、上下文工程、反思进化机制仅列入后续待办，本轮不展开具体开发计划。

## Key Changes

- 扩展 LLM 抽象：
  - 保留现有 `complete()`。
  - 新增 `complete_with_tools()`，支持原生 `tools/tool_choice`。
  - 新增 `supports_native_tools` 能力标识。
  - 当原生工具调用不可用或返回“不支持 tools”的供应商错误时，切换到 JSON fallback。
- 新增统一工具调用返回模型：
  - `ToolDefinition`
  - `ToolCallRequest`
  - `ToolCallResult`
  - `ToolCallingTurn`
  - `ToolExecutionError`
- 新增 JSON fallback 协议：
  - 模型必须返回严格 JSON。
  - 格式为：`{"action": "...", "arguments": {...}, "reason": "..."}`
  - 结束格式为：`{"action": "final", "arguments": {...}, "reason": "..."}`
  - 后端只执行白名单 action。
  - JSON 解析失败、action 不存在、参数非法都作为可恢复错误反馈给模型，最多重试 2 次。
- 新增 `ToolRegistry`：
  - 注册并执行 `search_web`、`summarize_task`、`save_note`、`write_report`。
  - 用 Pydantic schema 校验参数。
  - 统一处理超时、异常、错误码和敏感信息脱敏。
- 新增 `ToolCallingResearchExecutor`：
  - 针对每个任务运行工具调用循环。
  - 优先走原生 function calling。
  - 不支持时走 JSON fallback。
  - 每个任务最大工具调用次数默认 6 次。
  - 全局最大工具调用次数默认 `max_tasks * 8 + 4`。
- 改造 `ResearchOrchestrator`：
  - `TodoPlanner` 继续负责初始计划。
  - 每个任务交给 `ToolCallingResearchExecutor` 执行。
  - 报告生成通过 `write_report` 工具完成。
  - 保留当前 fallback report，避免报告生成失败时前端无结果。
- SSE 事件保持兼容：
  - 继续发送 `status`、`task`、`search_results`、`summary`、`report`、`error`、`done`。
  - 新增状态文案可包括“模型正在选择工具”“正在执行工具”“已降级为 JSON 工具调用”。

## Phased Plan With Acceptance Criteria

### Phase 1: LLM 工具调用接口与降级能力

实现 `complete_with_tools()`、原生工具调用解析、能力标识和 JSON fallback 入口。

验收指标：

- 原生 function calling 请求体包含 `tools` 和 `tool_choice`。
- 能解析原生 tool call 响应。
- 能解析普通 assistant content 响应。
- 当 provider 标记不支持原生工具调用时，直接走 JSON fallback。
- 当供应商返回 tools 不支持错误时，自动降级 JSON fallback，并记录状态。
- `complete()` 现有测试全部通过。
- 不改变 `TodoPlanner`、`TaskSummarizer`、`ReportWriter` 的旧调用行为。

### Phase 2: JSON Fallback 协议解析器

实现严格 JSON action 解析、校验和错误反馈机制。

验收指标：

- 合法 action 能解析为统一 `ToolCallRequest`。
- `final` action 能结束工具调用循环。
- 非 JSON 输出返回可恢复解析错误。
- 缺少 `action`、`arguments`、`reason` 时返回可测试错误码。
- 未注册 action 被拒绝。
- 参数 schema 错误被拒绝。
- 连续解析失败超过 2 次后，任务失败并记录原因。
- fallback prompt 明确要求“只返回 JSON，不返回 Markdown 或解释文本”。

### Phase 3: ToolRegistry 与现有工具封装

实现工具注册、参数校验、执行、错误封装和日志摘要。

验收指标：

- `search_web` 能调用 `TavilySearchTool` 并返回结构化搜索结果。
- `summarize_task` 能调用 `TaskSummarizer`。
- `save_note` 能调用 `NoteTool` 并写入 JSONL。
- `write_report` 能调用 `ReportWriter`。
- 未注册工具名必须拒绝执行。
- 参数缺失、类型错误必须返回可测试错误码。
- 工具异常不能泄露 API key、Bearer token 等敏感信息。
- 原生 tool call 和 JSON fallback 产生的工具请求都通过同一套 registry 执行。

### Phase 4: ToolCallingResearchExecutor

新增模型驱动工具调用循环，先用独立测试验证，不立即替换主流程。

验收指标：

- 原生模式下，模型依次调用 `search_web -> summarize_task -> save_note`，任务成功完成。
- JSON fallback 模式下，同样流程成功完成。
- 模型重复搜索时，executor 合并并去重搜索结果。
- 模型调用不存在工具时，executor 返回失败记录。
- 模型超过最大调用次数时，executor 停止并标记任务失败。
- 模型没有产出 summary 时，任务不能标记 completed。
- 每次工具调用都进入 `tool_logs`。
- 降级事件能被记录，便于排查模型能力问题。

### Phase 5: Orchestrator 集成

用 `ToolCallingResearchExecutor` 替换当前固定 `search -> summarize -> note` 任务执行逻辑，并让报告生成通过工具调用链路完成。

验收指标：

- 完整 mock research flow 通过：3 个任务完成、生成 3 条笔记、最终报告包含来源。
- 原生 function calling mock flow 通过。
- JSON fallback mock flow 通过。
- 单个任务失败时，其他任务继续执行。
- 全部任务失败时，仍返回清晰空报告。
- 报告生成工具失败时，使用现有 fallback report。
- SSE 事件对前端兼容，前端无需修改即可显示任务、来源、总结、报告。
- 原有后端测试全部通过，必要时更新“固定顺序”测试为“受控工具调用顺序”测试。

### Phase 6: 安全边界、回归测试与文档

补齐限制、错误说明、运行文档和验收测试。

验收指标：

- 后端完整测试通过：`uv run --with ".[test]" pytest`。
- 覆盖以下场景：
  - 原生工具调用正常路径。
  - JSON fallback 正常路径。
  - 原生不支持后自动降级。
  - JSON 格式错误。
  - 工具参数非法。
  - 工具调用超限。
  - 搜索失败。
  - 总结失败。
  - 保存笔记失败。
  - 报告校验失败后 fallback report。
  - 敏感信息脱敏。
- README 说明：
  - 默认优先原生 function calling。
  - 不支持时自动降级 JSON fallback。
  - 支持的工具列表。
  - 工具调用次数限制。
  - 常见错误与排查方式。
- 不引入任意 shell、任意文件写入、任意 URL 请求工具。

## Backlog TODO

后续单独制定开发计划：

- 记忆系统：
  - 研究记忆、用户偏好记忆、运行期记忆。
  - 记忆保存、检索、置信度、来源追踪。
- 上下文工程：
  - `ContextBuilder`。
  - token 预算。
  - 相关记忆注入。
  - 工具 schema 注入策略。
- 反思进化机制：
  - 运行后反思。
  - 失败原因归纳。
  - 可复用 query 和策略沉淀。
  - 只生成改进建议，不自动改代码。

## Assumptions

- 优先使用原生 function calling；不支持时自动降级 JSON fallback。
- “全部现有工具”按业务能力暴露为 `search_web`、`summarize_task`、`save_note`、`write_report`。
- 原生模式和 JSON fallback 共用同一个 `ToolRegistry` 和参数 schema。
- `TodoPlanner` 暂不改成工具调用 agent，仍负责初始计划。
- 模型自主性限制在白名单工具、参数 schema、调用次数预算内。
- 本轮不实现“资料是否足够”的 replan/coverage judge；该能力后续可并入反思进化或 plan-and-execute 阶段。
