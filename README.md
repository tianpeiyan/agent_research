# Automated Deep Research Agent

MVP for an automated deep research agent. A user enters a research topic, the backend runs a sequential agent workflow, and the frontend streams progress, task summaries, sources, and a final Markdown report.

## Current MVP

- Backend: FastAPI with `GET /health` and `GET /research/stream`.
- Frontend: Vue 3 + TypeScript research workspace.
- Agent flow: TODO Planner -> SearchTool -> Task Summarizer -> NoteTool -> Report Writer.
- LLM provider: Alibaba Bailian / DashScope OpenAI-compatible chat completions.
- Search provider: Tavily.
- Output: Server-Sent Events plus a Markdown report with citation quality checks.
- Notes: JSONL records written to `NOTES_PATH`.

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

If Vite chooses another port, add that origin to `CORS_ORIGINS` and restart the backend.

If the backend is not running on `http://localhost:8000`, set:

```bash
VITE_API_BASE_URL=http://your-backend-host:8000
```

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

If validation fails, the report writer asks the LLM to revise once. If it still fails, `/research/stream` emits an `error` event and then `done`.

## Common Errors

`DASHSCOPE_API_KEY is required`

Add `DASHSCOPE_API_KEY` to `.env` and restart the backend.

`TAVILY_API_KEY is required`

Add `TAVILY_API_KEY` to `.env` and restart the backend.

Frontend shows connection error

Check that the backend is running, `VITE_API_BASE_URL` points to the backend, and the frontend origin is included in `CORS_ORIGINS`.

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
