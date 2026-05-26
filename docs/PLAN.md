# 自动化深度研究 Agent 开发计划

## Summary
开发目标是交付一个可用 MVP：用户输入研究主题后，系统按 TODO 驱动范式执行“规划 -> 搜索 -> 总结 -> 记录 -> 报告”流程，并通过前端实时展示进度和最终 Markdown 报告。

核心架构采用：
- 前端：Vue3 + TypeScript，全屏研究对话框，展示任务进度、日志、报告和引用。
- 后端：FastAPI，提供 `/research/stream` SSE 流式接口。
- Agent 层：TODO Planner、Task Summarizer、Report Writer 三个顺序协作 Agent。
- 工具层：SearchTool、NoteTool、工具调用日志记录。
- 外部服务：LLM 使用阿里百炼，搜索使用 Tavily。

## Phases And Acceptance Criteria

### Phase 1: 项目初始化与基础骨架
搭建前后端工程结构，建立配置、依赖、测试框架和基础运行命令。

验收指标：
- 后端 FastAPI 可启动，`GET /health` 返回正常。
- 前端 Vue3 项目可启动，显示基础研究入口。
- 项目包含 `.env.example`，说明百炼、Tavily 等配置项。
- 后端和前端都有基础测试命令，并能成功运行。
- README 说明本地启动步骤。

### Phase 2: 数据模型与事件协议
定义研究任务、搜索结果、任务总结、最终报告、SSE 事件、错误响应等核心模型。

验收指标：
- TODO 任务包含 `title`、`intent`、`query`、`status`。
- 搜索结果包含 `title`、`url`、`snippet`、`source`。
- SSE 事件至少支持 `status`、`task`、`search_results`、`summary`、`report`、`error`、`done`。
- 单元测试覆盖模型校验、非法输入、事件序列化。
- 研究主题为空或过长时返回明确错误。

### Phase 3: LLM Provider 与三个 Agent
封装阿里百炼 LLM 调用，并实现 TODO Planner、Task Summarizer、Report Writer 三个 Agent。

验收指标：
- TODO Planner 能把研究主题拆解为 3-5 个子任务。
- Planner 输出必须是可解析 JSON，只包含任务列表。
- Task Summarizer 能基于搜索结果生成 Markdown 总结，并保留来源引用。
- Report Writer 能整合多个任务总结，生成包含标题、概述、分节分析、总结、参考文献的 Markdown 报告。
- 使用 mock LLM 的单元测试全部通过。
- LLM 输出格式异常时有明确错误或重试机制。

### Phase 4: SearchTool 与 NoteTool
实现搜索工具和笔记工具。SearchTool 支持 Tavily；NoteTool 保存每个子任务的总结、来源和执行记录。

验收指标：
- SearchTool 可根据任务 `query` 返回结构化搜索结果。
- 每个子任务默认获取最多 5 条搜索结果。
- 搜索结果按 URL 去重。
- 搜索失败、无结果、超时都有可测试的错误处理。
- NoteTool 能保存任务标题、总结内容、来源列表、标签和时间。
- 单元测试覆盖搜索成功、搜索失败、重复 URL、笔记保存。

### Phase 5: 顺序协作编排器
实现核心协调器，按固定顺序执行：接收主题 -> 规划 TODO -> 逐个搜索 -> 逐个总结 -> 记录笔记 -> 生成最终报告。

验收指标：
- 同一时间只执行一个 Agent，符合顺序协作模式。
- 每个阶段都有明确输入输出，不依赖隐式全局状态。
- 任一子任务失败时，系统记录失败原因，并继续或明确终止。
- 使用 mock LLM 和 mock SearchTool 可完成完整端到端流程。
- 端到端测试验证最终报告中包含所有已完成子任务的内容和来源。

### Phase 6: 工具调用日志与进度追踪
记录 Agent 调用了哪些工具、传入了什么参数、获得了什么结果，并将关键进度通过 SSE 推送给前端。

验收指标：
- 每次搜索、总结、记录笔记、生成报告都有日志。
- 日志包含时间、阶段、工具名、输入摘要、输出摘要、状态。
- SSE 能实时推送“正在规划”“正在搜索”“正在总结”“任务完成”“报告生成完成”等状态。
- 后端测试能验证 SSE 事件顺序。
- 错误事件包含可读错误信息，不暴露 API Key。

### Phase 7: FastAPI 流式接口
实现 `/research/stream` 接口，前端通过 SSE 获取研究过程和最终结果。

验收指标：
- 前端请求后能持续收到后端事件，而不是只收到最终结果。
- 接口支持一次完整研究流程。
- 客户端断开连接时，后端能停止或清理当前任务。
- LLM 或搜索服务异常时，接口返回 `error` 事件并正常结束。
- 集成测试覆盖正常流程、搜索失败、LLM 失败、非法输入。

### Phase 8: 前端研究界面
实现全屏模态研究 UI，包括主题输入、任务列表、实时状态、日志区域、Markdown 报告展示、来源链接展示。

验收指标：
- 用户输入主题后能发起研究任务。
- 任务状态能从 `pending` 更新到 `running`、`completed` 或 `failed`。
- 实时日志能展示当前 Agent 正在做什么。
- 最终 Markdown 报告能正确渲染。
- 来源链接可点击。
- 移动端和桌面端界面无明显遮挡、溢出或错位。

### Phase 9: 可信引用质量验收
围绕“可信引用”做质量控制，确保报告不是无来源生成。

验收指标：
- 每个已完成子任务至少保留 1 条来源；理想情况下 3 条以上。
- 报告参考文献区必须包含所有被引用 URL。
- 报告正文中的关键结论应带 `[1]`、`[2]` 等引用标记。
- 如果某任务搜索结果不足，报告中必须明确标记“资料不足”或“待验证”。
- 自动化测试验证报告结构、引用格式、来源去重和引用完整性。

### Phase 10: MVP 联调与交付
进行真实 API 联调、完善文档、补充使用说明和限制说明。

验收指标：
- 使用真实阿里百炼 API Key 和 Tavily Key 可完成一次真实研究。
- 使用 mock 模式时，所有自动化测试通过。
- README 包含安装、配置、启动、测试、常见错误说明。
- 文档明确说明当前 MVP 限制：不包含登录、多用户历史、生产部署、PDF/DOCX 导出。
- 用户可在本地完成一次完整流程：输入主题 -> 查看进度 -> 获得 Markdown 报告和参考来源。

## Public Interfaces
- 后端接口：`/research/stream`
- 请求输入：`topic`，可选 `max_tasks`
- 输出方式：SSE 事件流
- 报告格式：Markdown
- LLM 配置：阿里百炼 API Key、base URL、模型名
- 搜索配置：Tavily API Key、搜索后端、最大结果数

## Test Plan
- 单元测试：配置、模型校验、Planner、Summarizer、Reporter、SearchTool、NoteTool。
- 集成测试：完整 mock 研究流程、SSE 事件顺序、异常处理。
- 前端测试：状态更新、错误展示、Markdown 渲染、来源链接展示。
- 真实服务 smoke test：使用真实 API Key 跑一个固定主题，确认能生成带引用报告。
- 每个阶段结束前必须判断测试是否覆盖该阶段验收指标，不能只以“命令通过”作为完成标准。

## Assumptions
- 第一版目标是可用 MVP，不做生产级权限、部署、监控和多用户历史。
- Agent 协作采用文档要求的顺序执行模式，不做并发调度。
- 研究任务默认拆成 3-5 个 TODO。
- 每个 TODO 默认搜索最多 5 条结果。
- API Key 由后续手动补充，不提交到代码仓库。
