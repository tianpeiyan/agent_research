# 自动化深度研究 Agent

这是一个自动化深度研究 Agent 项目。用户输入研究主题后，后端会运行受控的工具调用研究工作流，前端实时展示进度、任务摘要、来源和最终 Markdown 报告。

## 当前 MVP

- 后端：FastAPI，提供 `GET /health` 和 `GET /research/stream`。
- 前端：Vue 3 + TypeScript 研究工作台。
- Agent 流程：TODO Planner -> 受控工具调用 Executor -> Evidence Judge -> 可选补充搜索 -> 最终 Report Writer。
- 工具调用：优先使用 OpenAI 兼容的原生 `tools/tool_choice`；如果供应商不支持或拒绝工具调用，后端会自动降级为严格 JSON fallback action。
- LLM 供应商：阿里云百炼 / DashScope OpenAI 兼容 Chat Completions。
- 搜索供应商：Tavily。
- 输出：Server-Sent Events，以及通过引用质量校验的 Markdown 报告。
- 笔记：任务摘要会以 JSONL 形式写入 `NOTES_PATH`。

## 研究工作流

`ResearchOrchestrator` 使用 LangGraph `StateGraph` 编排工作流。当前图包含以下节点：

```text
plan -> execute_task -> judge_evidence -> write_report
                         │
                         └─ 证据不足且尚未补充搜索
                            -> rewrite_queries -> supplemental_search -> judge_evidence
```

各节点保持窄职责：

- `plan`：调用 `TodoPlanner` 生成 3 到 5 个研究任务。
- `execute_task`：将每个任务交给受控工具调用执行器处理。
- `judge_evidence`：调用注册的 `judge_evidence` 业务工具判断证据充分性。
- `rewrite_queries`：调用注册的 `rewrite_queries` 业务工具生成补充搜索查询。
- `supplemental_search`：对每条补充查询执行一次 `search_web`。
- `write_report`：将任务摘要和证据状态传给 `ReportWriter` 生成报告。

LangGraph 负责状态路由和有界循环。业务行为仍通过项目服务和 `ToolRegistry` 执行，因此参数校验、日志、脱敏、工具预算和可测试错误码都保留在项目代码中。

## 环境要求

- Python 3.12+
- uv
- Node.js 20+
- npm

## 配置

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

填写真实 API Key：

- `DASHSCOPE_API_KEY`：真实 LLM 调用必填。
- `LLM_BASE_URL`：默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- `LLM_MODEL`：默认 `qwen-plus`。
- `TAVILY_API_KEY`：真实搜索调用必填。
- `TAVILY_BASE_URL`：默认 `https://api.tavily.com`。
- `MAX_SEARCH_RESULTS`：MVP 流程中最大值限制为 5。
- `NOTES_PATH`：默认 `data/notes.jsonl`。
- `CORS_ORIGINS`：前端来源，例如 `http://localhost:5173,http://127.0.0.1:5174`。

`SERPAPI_API_KEY` 仅为后续供应商支持预留；当前实现使用 Tavily。

## 本地运行

启动后端：

```bash
uv sync --extra test
uv run uvicorn app.main:app --app-dir backend --reload
```

在另一个终端启动前端：

```bash
cd frontend
npm install
npm run dev
```

打开 Vite 输出的前端地址，通常是：

```text
http://localhost:5173/
```

开发环境下，前端通过 Vite 的 `/api` 代理访问后端。请保持后端运行在 `http://localhost:8000`，浏览器仍应打开 Vite 前端地址，例如 `http://localhost:5173/`。

如果后端不是运行在 `http://localhost:8000`，创建 `frontend/.env.local` 并设置：

```text
VITE_API_BASE_URL=http://your-backend-host:8000
```

使用 `VITE_API_BASE_URL` 时，请确保前端来源已加入 `CORS_ORIGINS`，然后重启后端。

## 手动验证

后端健康检查：

```bash
curl http://localhost:8000/health
```

期望响应：

```json
{"status":"ok","service":"automated-deep-research-agent","environment":"local"}
```

SSE 研究流：

```bash
curl -N "http://localhost:8000/research/stream?topic=AI%20agents&max_tasks=3"
```

期望事件序列包含：

```text
event: status
event: task
event: search_results
event: summary
event: report
event: done
```

前端验证：

1. 打开 Vite 前端地址。
2. 输入 `AI agents`。
3. 保持 `3 tasks`。
4. 点击 `Start`。
5. 确认 TODO Planner、Runtime log、Sources、Task Summarizer 和 Report Writer 会随运行进度更新。
6. 确认任务摘要完成后，`data/notes.jsonl` 中写入了任务笔记。

## 工具调用

Planner 仍负责生成初始 TODO 列表。每个 TODO 由有界执行器处理，模型只能选择白名单 action：

- `search_web`：通过已有搜索工具调用 Tavily。
- `summarize_task`：总结当前任务累计搜索结果。
- `save_note`：将完成的任务摘要写入 JSONL notes。
- `write_report`：基于完成的任务摘要生成最终 Markdown 报告。
- `load_skill`：从 `skills/<skill_name>/SKILL.md` 读取外部认知 skill 手册。
- `judge_evidence`：判断完成的摘要和来源是否足够生成最终报告。
- `rewrite_queries`：将证据缺口改写为最多两条补充搜索查询。

JSON fallback 协议只接受如下严格 JSON：

```json
{"action":"search_web","arguments":{"query":"AI agents"},"reason":"Need sources."}
```

任务完成使用：

```json
{"action":"final","arguments":{},"reason":"Summary and note are complete."}
```

非法 JSON、未知 action 和不符合 schema 的参数都会被拒绝，并作为可恢复错误反馈给模型。连续两次 JSON 解析失败后，当前任务失败，编排器继续执行剩余任务。

每个任务默认最多 6 次工具调用。每次研究运行还有全局预算 `max_tasks * 8 + 4` 次工具调用，包含最终报告生成。补充搜索单独限制为一轮，且最多两条查询。执行器不暴露任意 shell 执行、任意文件写入或任意 URL 抓取工具。

## 外部 Skills

外部 skills 是可选的只读认知手册，Agent 可以通过 `load_skill` 业务工具加载。业务工具负责实际工作，例如搜索、写笔记、判断证据或生成报告；skills 只提供说明，不会被执行。

每个 skill 放在项目根目录下：

```text
skills/
└── <skill_name>/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── assets/
```

当前限制：

- 必须包含 `SKILL.md`。
- `skill_name` 只能包含字母、数字、下划线和连字符。
- 只读取 `skills/<skill_name>/SKILL.md`。
- `scripts/` 永远不会被执行。
- 不会读取 skill 目录外的文件。

项目包含 `skills/research-task-summarizer/SKILL.md`，作为 skill-enabled summarization 配置下 `TaskSummarizer` 使用的认知手册。如果该 skill 加载失败，summarizer 会回退到普通摘要 prompt，并记录失败的工具调用。

## 证据缺口

所有计划任务完成后，`EvidenceJudge` 会综合任务摘要、来源和失败任务判断证据是否充分。证据充分时，工作流直接生成报告。

如果证据不足且尚未执行补充搜索：

- `QueryRewriter` 最多生成 2 条新查询。
- 每条查询触发一次 `search_web` 调用。
- 补充搜索结果会按 URL 去重后合并到 workflow sources。
- 工作流会再次运行 `judge_evidence`。

补充搜索循环最多运行一次。如果 query rewrite 或补充搜索失败，工作流仍会生成报告。如果证据仍然不足，`ReportWriter` 必须在报告中标注 `证据不足`、`资料不足`、`待验证` 或 `仅供参考`。

## 测试

运行后端测试：

```bash
uv run --with ".[test]" pytest
```

运行前端测试：

```bash
cd frontend
npm test
```

运行前端类型检查和构建：

```bash
cd frontend
npm run build
```

## 引用质量

报告返回前会经过引用质量校验：

- 已完成任务必须至少保留一个来源。
- 报告必须包含 `References` 或 `参考文献` 章节。
- 正文结论必须包含类似 `[1]` 的引用标记。
- 正文中使用的引用标记必须存在于参考文献章节。
- 参考文献章节必须包含每个 source URL。
- 重复 source URL 会被拒绝。
- 如果任一任务来源少于 3 条，报告必须标注 `资料不足`、`待验证`、`insufficient` 或 `needs verification`。
- 如果 `EvidenceJudge` 判断整体证据不足，报告必须显式标注 `证据不足`、`资料不足`、`待验证` 或 `仅供参考`。

如果校验失败，Report Writer 会要求 LLM 修订一次。如果仍然失败，编排器会生成结构化兜底报告，并保留来源、引用标记和证据状态。

## 开发说明

添加 LangGraph 节点、业务工具、外部 skills 和证据感知报告行为时，请参考 [docs/开发指南.md](docs/开发指南.md)。

## 常见错误

`DASHSCOPE_API_KEY is required`

请在 `.env` 中添加 `DASHSCOPE_API_KEY`，然后重启后端。

`TAVILY_API_KEY is required`

请在 `.env` 中添加 `TAVILY_API_KEY`，然后重启后端。

Provider does not support native tools

正常使用时无需手动处理。后端会发出状态更新，并回退到严格 JSON tool action。如果 fallback action 反复解析失败，请检查模型输出，确认所选模型能够遵循 JSON-only 指令。

Frontend shows connection error

请确认后端运行在 `http://localhost:8000`。本地开发时前端使用 Vite 的 `/api` 代理，因此打开 `http://localhost:5173/` 是正常的，不代表后端运行在 5173 端口。如果配置了 `VITE_API_BASE_URL`，请确认它指向后端，并且前端来源已加入 `CORS_ORIGINS`。

Report fails citation validation

LLM 返回的报告缺少引用标记、参考文献或来源 URL。系统会自动重试一次。如果仍然失败，可以重新运行该主题，或降低主题歧义。

No notes are written

检查 `NOTES_PATH`。目录只会在至少一条任务摘要被保存后按需创建。

## MVP 限制

- 没有登录或用户账户。
- 没有多用户历史。
- 没有生产部署配置。
- 没有 PDF 或 DOCX 导出。
- 尚未实现 SerpAPI。
- 没有持久化任务队列。
- 进程重启后不支持后台任务恢复。
- 引用质量校验是规则型校验，不能完全判断来源可信度。
- 真实 API 行为依赖外部 LLM 和搜索供应商可用性。
