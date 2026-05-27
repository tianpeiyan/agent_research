# Automated Deep Research Agent

MVP for an automated deep research agent. A user enters a research topic, the backend runs a controlled tool-calling research workflow, and the frontend streams progress, task summaries, sources, and a final Markdown report.

## Current MVP

- Backend: FastAPI with `GET /health` and `GET /research/stream`.
- Frontend: Vue 3 + TypeScript research workspace.
- Agent flow: TODO Planner -> controlled tool-calling executor -> Evidence Judge -> optional supplemental search -> final Report Writer.
- Tool calling: native OpenAI-compatible `tools/tool_choice` is preferred. If the provider is marked unsupported or rejects tools, the backend automatically downgrades to strict JSON fallback actions.
- LLM provider: Alibaba Bailian / DashScope OpenAI-compatible chat completions.
- Search provider: Tavily.
- Output: Server-Sent Events plus a Markdown report with citation quality checks.
- Notes: JSONL records written to `NOTES_PATH`.

## Research Workflow

`ResearchOrchestrator` currently uses LangGraph `StateGraph` for workflow
orchestration. The compiled graph contains these nodes:

```text
plan -> execute_task -> judge_evidence -> write_report
                         │
                         └─ insufficient evidence, first round only
                            -> rewrite_queries -> supplemental_search -> judge_evidence
```

Node responsibilities stay narrow:

- `plan`: asks `TodoPlanner` for 3 to 5 research tasks.
- `execute_task`: runs each task through the controlled tool-calling executor.
- `judge_evidence`: calls the registered `judge_evidence` business tool.
- `rewrite_queries`: calls the registered `rewrite_queries` business tool.
- `supplemental_search`: calls `search_web` once per supplemental query.
- `write_report`: passes summaries and evidence status to `ReportWriter`.

LangGraph owns state routing and bounded loops. Business behavior still goes
through project services and `ToolRegistry`, so validation, logging, redaction,
tool budgets, and testable error codes stay in project code.

## Requirements

- Python 3.12+
- uv
- Node.js 20+
- npm

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Fill in the real API keys:

- `DASHSCOPE_API_KEY`: required for real LLM calls.
- `LLM_BASE_URL`: defaults to `https://dashscope.aliyuncs.com/compatible-mode/v1`.
- `LLM_MODEL`: defaults to `qwen-plus`.
- `TAVILY_API_KEY`: required for real search calls.
- `TAVILY_BASE_URL`: defaults to `https://api.tavily.com`.
- `MAX_SEARCH_RESULTS`: capped to 5 in the MVP flow.
- `NOTES_PATH`: defaults to `data/notes.jsonl`.
- `CORS_ORIGINS`: frontend origins, for example `http://localhost:5173,http://127.0.0.1:5174`.

`SERPAPI_API_KEY` is documented for later provider support. The current implementation uses Tavily.

## Run Locally

Start the backend:

```bash
uv sync --extra test
uv run uvicorn app.main:app --app-dir backend --reload
```

Start the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the frontend URL printed by Vite, usually:

```text
http://localhost:5173/
```

In development, the frontend calls the backend through Vite's `/api` proxy. Keep the
backend running on `http://localhost:8000`; the browser should still open the Vite
frontend URL such as `http://localhost:5173/`.

If the backend is not running on `http://localhost:8000`, create `frontend/.env.local`
and set:

```text
VITE_API_BASE_URL=http://your-backend-host:8000
```

When using `VITE_API_BASE_URL`, make sure that frontend origin is included in
`CORS_ORIGINS`, then restart the backend.

## Verify Manually

Backend health:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok","service":"automated-deep-research-agent","environment":"local"}
```

SSE research stream:

```bash
curl -N "http://localhost:8000/research/stream?topic=AI%20agents&max_tasks=3"
```

Expected event sequence includes:

```text
event: status
event: task
event: search_results
event: summary
event: report
event: done
```

Frontend verification:

1. Open the Vite frontend URL.
2. Enter `AI agents`.
3. Keep `3 tasks`.
4. Click `Start`.
5. Confirm that TODO Planner, Runtime log, Sources, Task Summarizer, and Report Writer update as the run progresses.
6. Confirm that `data/notes.jsonl` contains saved task notes after completed summaries.

## Tool Calling

The planner still creates the initial TODO list. Each TODO is then handled by a bounded executor where the model can choose only whitelisted actions:

- `search_web`: calls Tavily through the existing search tool.
- `summarize_task`: summarizes accumulated search results for the current task.
- `save_note`: writes the completed task summary to JSONL notes.
- `write_report`: generates the final Markdown report from completed summaries.
- `load_skill`: reads an external cognitive skill manual from `skills/<skill_name>/SKILL.md`.
- `judge_evidence`: judges whether completed summaries and sources are enough for final reporting.
- `rewrite_queries`: turns evidence gaps into at most two supplemental search queries.

The JSON fallback protocol accepts only strict JSON with this shape:

```json
{"action":"search_web","arguments":{"query":"AI agents"},"reason":"Need sources."}
```

Task completion uses:

```json
{"action":"final","arguments":{},"reason":"Summary and note are complete."}
```

Invalid JSON, unknown actions, and schema-invalid arguments are rejected and fed back to the model as recoverable errors. After two consecutive JSON parse failures, the current task fails and the orchestrator continues with the remaining tasks.

Each task is limited to 6 tool calls by default. Each research run also has a global budget of `max_tasks * 8 + 4` tool calls, including final report generation. Supplemental search is separately bounded to one round and at most two queries. The executor does not expose arbitrary shell execution, arbitrary file writes, or arbitrary URL fetch tools.

## External Skills

External skills are optional read-only cognitive manuals that agents can load
through the `load_skill` business tool. Business tools do work inside the
runtime, such as searching, writing notes, judging evidence, or writing reports.
Skills provide instructions only; they are not executed.

Put each skill under the project root:

```text
skills/
└── <skill_name>/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── assets/
```

Current limits:

- `SKILL.md` is required.
- `skill_name` may contain only letters, numbers, underscores, and hyphens.
- Only `skills/<skill_name>/SKILL.md` is read.
- `scripts/` are never executed.
- Files outside the skill directory are never read.

The project includes `skills/research-task-summarizer/SKILL.md`, a cognitive
manual used by `TaskSummarizer` when skill-enabled summarization is configured.
If that skill cannot be loaded, the summarizer falls back to the normal summary
prompt and records the failed tool call.

## Evidence Gaps

After all planned tasks finish, `EvidenceJudge` evaluates the combined summaries,
sources, and failed tasks. If evidence is sufficient, the workflow writes the
report immediately.

If evidence is insufficient and no supplemental round has been used yet:

- `QueryRewriter` may produce at most 2 new queries.
- Each query triggers one `search_web` call.
- Supplemental results are merged into workflow sources with URL de-duplication.
- The workflow runs `judge_evidence` again.

The supplemental loop cannot run more than once. If rewriting or supplemental
search fails, the workflow still generates a report. If evidence remains
insufficient, `ReportWriter` must mark the report as `证据不足`, `资料不足`,
`待验证`, or `仅供参考`.

## Tests

Run backend tests:

```bash
uv run --with ".[test]" pytest
```

Run frontend tests:

```bash
cd frontend
npm test
```

Run frontend type-check and build:

```bash
cd frontend
npm run build
```

## Citation Quality

The report writer validates the final Markdown before returning it:

- Completed tasks must keep at least one source.
- The report must include a `References` or `参考文献` section.
- Body claims must include citation markers like `[1]`.
- Citation markers used in the body must exist in the references section.
- The references section must include every source URL.
- Duplicate source URLs are rejected.
- If any task has fewer than 3 sources, the report must mark the evidence as `资料不足`, `待验证`, `insufficient`, or `needs verification`.
- If `EvidenceJudge` marks the overall evidence insufficient, the report must explicitly mark the limitation as `证据不足`, `资料不足`, `待验证`, or `仅供参考`.

If validation fails, the report writer asks the LLM to revise once. If it still fails, the orchestrator emits a structured fallback report that preserves sources, citation markers, and evidence status.

## Development Notes

See [docs/开发指南.md](docs/开发指南.md) for the current rules on adding LangGraph nodes, business tools, external skills, and evidence-aware report behavior.

## Common Errors

`DASHSCOPE_API_KEY is required`

Add `DASHSCOPE_API_KEY` to `.env` and restart the backend.

`TAVILY_API_KEY is required`

Add `TAVILY_API_KEY` to `.env` and restart the backend.

Provider does not support native tools

No manual action is required in normal use. The backend emits a status update and falls back to strict JSON tool actions. If fallback actions repeatedly fail to parse, inspect the model output and make sure the selected model can follow JSON-only instructions.

Frontend shows connection error

Check that the backend is running on `http://localhost:8000`. During local
development the frontend uses Vite's `/api` proxy, so opening `http://localhost:5173/`
is expected and does not mean the backend is on port 5173. If you configured
`VITE_API_BASE_URL`, verify that it points to the backend and that the frontend
origin is included in `CORS_ORIGINS`.

Report fails citation validation

The LLM returned a report missing citation markers, references, or source URLs. The system retries once. If it still fails, rerun the topic or lower ambiguity in the topic.

No notes are written

Check `NOTES_PATH`. The directory is created on demand only after at least one task summary is saved.

## MVP Limitations

- No login or user accounts.
- No multi-user history.
- No production deployment setup.
- No PDF or DOCX export.
- No SerpAPI implementation yet.
- No persistent job queue.
- No background resume after process restart.
- Citation quality is rule-based and cannot fully judge source trustworthiness.
- Real API behavior depends on external LLM and search provider availability.
