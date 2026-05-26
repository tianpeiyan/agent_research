import json
import asyncio

from fastapi.testclient import TestClient

from app.main import _research_event_stream, app, get_orchestrator_factory
from app.models import ResearchRequest, SSEEventType
from app.progress import ResearchProgressTracker


class StreamingOrchestrator:
    def __init__(self, progress: ResearchProgressTracker, mode: str) -> None:
        self.progress = progress
        self.mode = mode

    async def run(self, topic: str, max_tasks: int = 5) -> None:
        self.progress.status("正在规划", topic=topic)
        if self.mode == "llm_failure":
            raise RuntimeError("LLM failed with DASHSCOPE_API_KEY=secret and Bearer abc123.")

        self.progress.emit(
            SSEEventType.TASK,
            {
                "tasks": [
                    {
                        "title": "Task A",
                        "intent": "Intent A",
                        "query": "query a",
                        "status": "pending",
                    }
                ]
            },
        )
        self.progress.status("正在搜索", task_title="Task A")

        if self.mode == "search_failure":
            self.progress.error("Search failed for Task A.")
            self.progress.emit(
                SSEEventType.REPORT,
                {
                    "report": {
                        "title": topic,
                        "markdown": "# Report\n\nNo completed tasks.",
                        "sources": [],
                    }
                },
            )
            self.progress.emit(SSEEventType.DONE, {"topic": topic})
            return

        self.progress.emit(
            SSEEventType.SEARCH_RESULTS,
            {
                "task_title": "Task A",
                "results": [
                    {
                        "title": "Source 1",
                        "url": "https://example.com/source-1",
                        "snippet": "Snippet 1.",
                        "source": "example.com",
                    }
                ],
            },
        )
        self.progress.status("正在总结", task_title="Task A")
        self.progress.emit(
            SSEEventType.SUMMARY,
            {
                "summary": {
                    "task_title": "Task A",
                    "content": "Summary [1].",
                    "sources": [
                        {
                            "title": "Source 1",
                            "url": "https://example.com/source-1",
                            "snippet": "Snippet 1.",
                            "source": "example.com",
                        }
                    ],
                }
            },
        )
        self.progress.status("报告生成完成", topic=topic)
        self.progress.emit(
            SSEEventType.REPORT,
            {
                "report": {
                    "title": topic,
                    "markdown": "# Report\n\n## Overview\nDone.",
                    "sources": [
                        {
                            "title": "Source 1",
                            "url": "https://example.com/source-1",
                            "snippet": "Snippet 1.",
                            "source": "example.com",
                        }
                    ],
                }
            },
        )
        self.progress.emit(SSEEventType.DONE, {"topic": topic})


class SlowOrchestrator:
    def __init__(self, progress: ResearchProgressTracker) -> None:
        self.progress = progress
        self.cancelled = False

    async def run(self, topic: str, max_tasks: int = 5) -> None:
        self.progress.status("正在规划", topic=topic)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _override_factory(mode: str):
    def dependency():
        return lambda progress: StreamingOrchestrator(progress, mode)

    return dependency


def _read_sse_events(response_text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in response_text.strip().split("\n\n"):
        event_name = ""
        payload = {}
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            if line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
        events.append((event_name, payload))
    return events


def _stream_text(client: TestClient, url: str) -> str:
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def test_research_stream_sends_complete_event_sequence() -> None:
    app.dependency_overrides[get_orchestrator_factory] = _override_factory("normal")
    try:
        client = TestClient(app)

        events = _read_sse_events(
            _stream_text(client, "/research/stream?topic=AI%20agents&max_tasks=3")
        )

        assert [event_name for event_name, _payload in events] == [
            "status",
            "task",
            "status",
            "search_results",
            "status",
            "summary",
            "status",
            "report",
            "done",
        ]
        assert events[0][1]["message"] == "正在规划"
        assert events[-1] == ("done", {"topic": "AI agents"})
    finally:
        app.dependency_overrides.clear()


def test_research_stream_cancels_running_task_when_client_disconnects() -> None:
    async def scenario() -> bool:
        progress = ResearchProgressTracker()
        progress.enable_streaming()
        orchestrator = SlowOrchestrator(progress)
        stream = _research_event_stream(
            ResearchRequest(topic="AI agents", max_tasks=3),
            orchestrator,
            progress,
        )

        first_chunk = await anext(stream)
        assert first_chunk.startswith("event: status\n")
        await stream.aclose()
        return orchestrator.cancelled

    assert asyncio.run(scenario()) is True


def test_research_stream_returns_error_event_for_search_failure() -> None:
    app.dependency_overrides[get_orchestrator_factory] = _override_factory("search_failure")
    try:
        client = TestClient(app)

        events = _read_sse_events(
            _stream_text(client, "/research/stream?topic=AI%20agents&max_tasks=3")
        )

        assert ("error", {"message": "Search failed for Task A."}) in events
        assert events[-1] == ("done", {"topic": "AI agents"})
    finally:
        app.dependency_overrides.clear()


def test_research_stream_returns_sanitized_error_event_for_llm_failure() -> None:
    app.dependency_overrides[get_orchestrator_factory] = _override_factory("llm_failure")
    try:
        client = TestClient(app)

        events = _read_sse_events(
            _stream_text(client, "/research/stream?topic=AI%20agents&max_tasks=3")
        )

        error_payloads = [payload for event_name, payload in events if event_name == "error"]
        assert len(error_payloads) == 1
        assert "secret" not in error_payloads[0]["message"]
        assert "abc123" not in error_payloads[0]["message"]
        assert "<redacted>" in error_payloads[0]["message"]
        assert events[-1] == ("done", {"topic": "AI agents"})
    finally:
        app.dependency_overrides.clear()
