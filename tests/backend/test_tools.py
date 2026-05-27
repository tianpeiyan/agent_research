import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.models import ResearchTask, SearchResult, TaskSummary
from app.tools import NoteTool, TavilySearchTool, ToolError


def _task() -> ResearchTask:
    return ResearchTask(
        title="Market landscape",
        intent="Map current vendors",
        query="AI research agent market landscape",
    )


def test_tavily_search_returns_structured_results_and_posts_query() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Example report",
                        "url": "https://example.com/report",
                        "content": "Market detail.",
                    }
                ]
            },
        )

    tool = TavilySearchTool(
        api_key="tvly-test",
        transport=httpx.MockTransport(handler),
    )

    results = asyncio.run(tool.search(_task()))

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["auth"] == "Bearer tvly-test"
    assert captured["payload"] == {
        "query": "AI research agent market landscape",
        "max_results": 5,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    assert results == [
        SearchResult(
            title="Example report",
            url="https://example.com/report",
            snippet="Market detail.",
            source="example.com",
        )
    ]


def test_tavily_search_deduplicates_by_url() -> None:
    tool = TavilySearchTool(
        api_key="tvly-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "First",
                            "url": "https://example.com/report/",
                            "content": "First snippet.",
                        },
                        {
                            "title": "Duplicate",
                            "url": "https://EXAMPLE.com/report",
                            "content": "Duplicate snippet.",
                        },
                    ]
                },
            )
        ),
    )

    results = asyncio.run(tool.search(_task()))

    assert len(results) == 1
    assert results[0].title == "First"


def test_tavily_search_failed_response_has_testable_error_code() -> None:
    tool = TavilySearchTool(
        api_key="tvly-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool.search(_task()))

    assert exc_info.value.code == "search_failed"


def test_tavily_search_no_results_has_testable_error_code() -> None:
    tool = TavilySearchTool(
        api_key="tvly-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []})
        ),
    )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool.search(_task()))

    assert exc_info.value.code == "no_search_results"


def test_tavily_search_timeout_has_testable_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    tool = TavilySearchTool(
        api_key="tvly-test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool.search(_task()))

    assert exc_info.value.code == "search_timeout"


def test_note_tool_saves_task_summary_sources_tags_and_time(tmp_path: Path) -> None:
    notes_path = tmp_path / "notes.jsonl"
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )
    summary = TaskSummary(
        task_title="Market landscape",
        content="The market is active [1].",
        sources=[source],
    )

    record = NoteTool(notes_path).save(summary, tags=["market", "phase-4"])

    persisted = json.loads(notes_path.read_text(encoding="utf-8").strip())
    assert record.task_title == "Market landscape"
    assert persisted["task_title"] == "Market landscape"
    assert persisted["summary_content"] == "The market is active [1]."
    assert persisted["sources"][0]["url"] == "https://example.com/report"
    assert persisted["tags"] == ["market", "phase-4"]
    assert persisted["created_at"].endswith("Z") or "+00:00" in persisted["created_at"]


def test_note_tool_remains_append_only_jsonl_task_artifact(tmp_path: Path) -> None:
    notes_path = tmp_path / "notes.jsonl"
    tool = NoteTool(notes_path)
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )

    first = tool.save(
        TaskSummary(
            task_title="Task A",
            content="First task finding [1].",
            sources=[source],
        ),
        tags=["research"],
    )
    second = tool.save(
        TaskSummary(
            task_title="Task B",
            content="Second task finding [1].",
            sources=[source],
        ),
        tags=["research"],
    )

    persisted = [
        json.loads(line)
        for line in notes_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [record["task_title"] for record in persisted] == ["Task A", "Task B"]
    assert [record["summary_content"] for record in persisted] == [
        "First task finding [1].",
        "Second task finding [1].",
    ]
    assert first.task_title == "Task A"
    assert second.task_title == "Task B"
