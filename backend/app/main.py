import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.agents import ReportWriter, TaskSummarizer, TodoPlanner
from app.config import get_settings
from app.llm import BailianLLMProvider
from app.models import ErrorResponse, ResearchRequest, SSEEventType
from app.orchestrator import ResearchOrchestrator
from app.progress import ResearchProgressTracker
from app.tools import NoteTool, TavilySearchTool


settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def create_research_orchestrator(
    progress: ResearchProgressTracker,
) -> ResearchOrchestrator:
    llm = BailianLLMProvider(
        api_key=settings.dashscope_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    return ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=TavilySearchTool(
            api_key=settings.tavily_api_key,
            base_url=settings.tavily_base_url,
            max_results=min(settings.max_search_results, 5),
        ),
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(Path(settings.notes_path)),
        report_writer=ReportWriter(llm),
        progress=progress,
    )


def get_orchestrator_factory() -> Callable[[ResearchProgressTracker], ResearchOrchestrator]:
    return create_research_orchestrator


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
    }


@app.get("/research/stream")
async def research_stream(
    topic: str = Query(...),
    max_tasks: int = Query(5),
    orchestrator_factory: Callable[[ResearchProgressTracker], ResearchOrchestrator] = Depends(
        get_orchestrator_factory
    ),
) -> StreamingResponse:
    try:
        request = ResearchRequest(topic=topic, max_tasks=max_tasks)
    except ValidationError as exc:
        error = ErrorResponse(
            code="invalid_research_request",
            message=(
                "Research topic must be non-empty and at most 200 characters; "
                "max_tasks must be between 3 and 5."
            ),
            details={"errors": exc.errors()},
        )
        raise HTTPException(status_code=422, detail=error.model_dump(mode="json")) from exc

    progress = ResearchProgressTracker()
    progress.enable_streaming()
    orchestrator = orchestrator_factory(progress)
    return StreamingResponse(
        _research_event_stream(request, orchestrator, progress),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _research_event_stream(
    request: ResearchRequest,
    orchestrator: ResearchOrchestrator,
    progress: ResearchProgressTracker,
) -> AsyncIterator[str]:
    run_task = asyncio.create_task(
        orchestrator.run(topic=request.topic, max_tasks=request.max_tasks)
    )
    done_seen = False

    try:
        while True:
            next_event_task = asyncio.create_task(progress.next_event())
            completed, pending = await asyncio.wait(
                {next_event_task, run_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if next_event_task in completed:
                event = next_event_task.result()
                yield event.to_sse()
                if event.type == SSEEventType.DONE:
                    done_seen = True
                    await run_task
                    break
            else:
                next_event_task.cancel()

            if run_task in completed:
                for event in progress.drain_events():
                    yield event.to_sse()
                    if event.type == SSEEventType.DONE:
                        done_seen = True

                try:
                    await run_task
                except Exception as exc:
                    message = progress.sanitize(str(exc))
                    progress.error(message)
                    for event in progress.drain_events():
                        yield event.to_sse()

                if not done_seen:
                    progress.emit(SSEEventType.DONE, {"topic": request.topic})
                    for event in progress.drain_events():
                        yield event.to_sse()
                break

            for pending_task in pending:
                if pending_task is not run_task:
                    pending_task.cancel()
    finally:
        if not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
