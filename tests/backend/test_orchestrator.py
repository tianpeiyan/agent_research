import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.agents import ReportWriter, TaskSummarizer, TodoPlanner
from app.llm import LLMMessage
from app.models import ResearchTask, SSEEventType, SearchResult, TaskStatus, ToolCallStatus
from app.orchestrator import ResearchOrchestrator
from app.progress import ResearchProgressTracker
from app.tools import NoteTool, ToolError


class MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[LLMMessage]] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class MockSearchTool:
    def __init__(self, results_by_query: dict[str, list[SearchResult]]) -> None:
        self.results_by_query = results_by_query
        self.calls: list[str] = []

    async def search(self, task: ResearchTask) -> list[SearchResult]:
        self.calls.append(task.query)
        if task.query == "failed query":
            raise ToolError("search_failed", "Search failed for this task.")
        return self.results_by_query[task.query]


class SensitiveFailingSearchTool:
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        raise ToolError(
            "search_failed",
            "Tavily failed with TAVILY_API_KEY=tvly-secret and Bearer abc123.",
        )


class OrderedPlanner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def plan(self, topic: str, max_tasks: int = 5) -> list[ResearchTask]:
        self.events.append("plan")
        return [
            ResearchTask(title="Task A", intent="Intent A", query="query a"),
            ResearchTask(title="Task B", intent="Intent B", query="query b"),
            ResearchTask(title="Task C", intent="Intent C", query="query c"),
        ]


class OrderedSearchTool:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def search(self, task: ResearchTask) -> list[SearchResult]:
        self.events.append(f"search:{task.title}")
        index = {"Task A": 1, "Task B": 2, "Task C": 3}[task.title]
        return [_source(index)]


class OrderedSummarizer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def summarize(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ):
        self.events.append(f"summarize:{task.title}")
        from app.models import TaskSummary

        return TaskSummary(
            task_title=task.title,
            content=f"Summary for {task.title} [1].",
            sources=list(search_results),
        )


class OrderedNoteTool:
    def __init__(self, events: list[str], delegate: NoteTool) -> None:
        self.events = events
        self.delegate = delegate

    def save(self, summary, tags=None):
        self.events.append(f"note:{summary.task_title}")
        return self.delegate.save(summary, tags=tags)


class OrderedReportWriter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def write(self, topic: str, summaries):
        self.events.append("report")
        from app.models import FinalReport

        sources = [source for summary in summaries for source in summary.sources]
        return FinalReport(
            title=topic,
            markdown=(
                f"# {topic}\n\n"
                "## Overview\nDone.\n\n"
                "## Sectioned Analysis\nDone.\n\n"
                "## Conclusion\nDone.\n\n"
                "## References\n"
                + "\n".join(str(source.url) for source in sources)
            ),
            sources=sources,
        )


class FailingReportWriter:
    async def write(self, topic: str, summaries):
        raise RuntimeError("Report must include a title, overview, conclusion, and references.")


def _source(index: int) -> SearchResult:
    return SearchResult(
        title=f"Source {index}",
        url=f"https://example.com/source-{index}",
        snippet=f"Snippet {index}.",
        source="example.com",
    )


def test_orchestrator_completes_full_mock_research_flow(tmp_path: Path) -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {"title": "Task A", "intent": "Intent A", "query": "query a"},
                    {"title": "Task B", "intent": "Intent B", "query": "query b"},
                    {"title": "Task C", "intent": "Intent C", "query": "query c"},
                ]
            ),
            "### Summary A\nFinding A [1].",
            "### Summary B\nFinding B [1].",
            "### Summary C\nFinding C [1].",
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Finding A, B, and C [1][2][3]. 资料不足: each task has one source.",
                    "## Sectioned Analysis",
                    "Task A content [1].\nTask B content [2].\nTask C content [3].",
                    "## Conclusion",
                    "All tasks completed [1][2][3].",
                    "## References",
                    "[1] https://example.com/source-1",
                    "[2] https://example.com/source-2",
                    "[3] https://example.com/source-3",
                ]
            ),
        ]
    )
    search_tool = MockSearchTool(
        {
            "query a": [_source(1)],
            "query b": [_source(2)],
            "query c": [_source(3)],
        }
    )
    notes_path = tmp_path / "notes.jsonl"
    progress = ResearchProgressTracker()
    orchestrator = ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=search_tool,
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(notes_path),
        report_writer=ReportWriter(llm),
        progress=progress,
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert search_tool.calls == ["query a", "query b", "query c"]
    assert [record.status for record in result.tasks] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
    ]
    assert all(record.summary is not None for record in result.tasks)
    assert "Task A content" in result.report.markdown
    assert "Task B content" in result.report.markdown
    assert "Task C content" in result.report.markdown
    assert "https://example.com/source-1" in result.report.markdown
    assert "https://example.com/source-2" in result.report.markdown
    assert "https://example.com/source-3" in result.report.markdown
    assert len(result.report.sources) == 3
    note_lines = notes_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(note_lines) == 3
    assert json.loads(note_lines[0])["task_title"] == "Task A"
    assert [log.stage for log in result.tool_logs] == [
        "search",
        "summary",
        "note",
        "search",
        "summary",
        "note",
        "search",
        "summary",
        "note",
        "report",
    ]
    assert all(log.created_at is not None for log in result.tool_logs)
    assert all(log.status == ToolCallStatus.SUCCESS for log in result.tool_logs)

    status_messages = [
        event.data["message"]
        for event in progress.events
        if event.type == SSEEventType.STATUS
    ]
    assert status_messages == [
        "正在规划",
        "正在搜索",
        "正在总结",
        "任务完成",
        "正在搜索",
        "正在总结",
        "任务完成",
        "正在搜索",
        "正在总结",
        "任务完成",
        "报告生成完成",
    ]
    assert [event.type for event in progress.events] == [
        SSEEventType.STATUS,
        SSEEventType.TASK,
        SSEEventType.TASK,
        SSEEventType.STATUS,
        SSEEventType.SEARCH_RESULTS,
        SSEEventType.STATUS,
        SSEEventType.SUMMARY,
        SSEEventType.STATUS,
        SSEEventType.TASK,
        SSEEventType.TASK,
        SSEEventType.STATUS,
        SSEEventType.SEARCH_RESULTS,
        SSEEventType.STATUS,
        SSEEventType.SUMMARY,
        SSEEventType.STATUS,
        SSEEventType.TASK,
        SSEEventType.TASK,
        SSEEventType.STATUS,
        SSEEventType.SEARCH_RESULTS,
        SSEEventType.STATUS,
        SSEEventType.SUMMARY,
        SSEEventType.STATUS,
        SSEEventType.TASK,
        SSEEventType.REPORT,
        SSEEventType.STATUS,
        SSEEventType.DONE,
    ]
    assert progress.events[-1].to_sse().startswith("event: done\n")


def test_orchestrator_records_failed_subtask_and_reports_completed_work(
    tmp_path: Path,
) -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {"title": "Task A", "intent": "Intent A", "query": "query a"},
                    {
                        "title": "Task B",
                        "intent": "Intent B",
                        "query": "failed query",
                    },
                    {"title": "Task C", "intent": "Intent C", "query": "query c"},
                ]
            ),
            "### Summary A\nFinding A [1].",
            "### Summary C\nFinding C [1].",
            "\n".join(
                [
                    "# Partial Report",
                    "## Overview",
                    "Completed tasks A and C [1][2]. 资料不足: one task failed.",
                    "## Sectioned Analysis",
                    "Task A content [1].\nTask C content [2].",
                    "## Conclusion",
                    "One task failed but completed findings remain available [1][2].",
                    "## References",
                    "[1] https://example.com/source-1",
                    "[2] https://example.com/source-3",
                ]
            ),
        ]
    )
    search_tool = MockSearchTool(
        {
            "query a": [_source(1)],
            "query c": [_source(3)],
        }
    )
    orchestrator = ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=search_tool,
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=ReportWriter(llm),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert [record.status for record in result.tasks] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.COMPLETED,
    ]
    failed_record = result.tasks[1]
    assert failed_record.task.status == TaskStatus.FAILED
    assert failed_record.error == "Search failed for this task."
    assert failed_record.summary is None
    assert "Task A content" in result.report.markdown
    assert "Task C content" in result.report.markdown
    assert len(result.report.sources) == 2


def test_orchestrator_returns_clear_empty_report_when_all_subtasks_fail(
    tmp_path: Path,
) -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {
                        "title": "Task A",
                        "intent": "Intent A",
                        "query": "failed query",
                    },
                    {
                        "title": "Task B",
                        "intent": "Intent B",
                        "query": "failed query",
                    },
                    {
                        "title": "Task C",
                        "intent": "Intent C",
                        "query": "failed query",
                    },
                ]
            )
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=MockSearchTool({}),
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=ReportWriter(llm),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert all(record.status == TaskStatus.FAILED for record in result.tasks)
    assert result.report.sources == []
    assert "No subtasks completed successfully" in result.report.markdown


def test_orchestrator_runs_agents_and_tools_in_fixed_sequential_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
    )

    asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert events == [
        "plan",
        "search:Task A",
        "summarize:Task A",
        "note:Task A",
        "search:Task B",
        "summarize:Task B",
        "note:Task B",
        "search:Task C",
        "summarize:Task C",
        "note:Task C",
        "report",
    ]


def test_orchestrator_error_events_and_logs_do_not_expose_api_keys(
    tmp_path: Path,
) -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {
                        "title": "Task A",
                        "intent": "Intent A",
                        "query": "query a",
                    },
                    {
                        "title": "Task B",
                        "intent": "Intent B",
                        "query": "query b",
                    },
                    {
                        "title": "Task C",
                        "intent": "Intent C",
                        "query": "query c",
                    },
                ]
            )
        ]
    )
    progress = ResearchProgressTracker()
    orchestrator = ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=SensitiveFailingSearchTool(),
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=ReportWriter(llm),
        progress=progress,
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    error_messages = [
        event.data["message"]
        for event in progress.events
        if event.type == SSEEventType.ERROR
    ]
    assert len(error_messages) == 3
    assert all("tvly-secret" not in message for message in error_messages)
    assert all("abc123" not in message for message in error_messages)
    assert all("<redacted>" in message for message in error_messages)
    assert all("tvly-secret" not in (record.error or "") for record in result.tasks)
    assert all("abc123" not in (log.error or "") for log in result.tool_logs)
    assert [log.status for log in result.tool_logs[:3]] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.FAILED,
        ToolCallStatus.FAILED,
    ]


def test_orchestrator_builds_fallback_report_when_report_writer_output_is_invalid(
    tmp_path: Path,
) -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {"title": "Task A", "intent": "Intent A", "query": "query a"},
                    {"title": "Task B", "intent": "Intent B", "query": "query b"},
                    {
                        "title": "Task C",
                        "intent": "Intent C",
                        "query": "failed query",
                    },
                ]
            ),
            "### Summary A\nFinding A [1].",
            "### Summary B\nFinding B [1].",
        ]
    )
    progress = ResearchProgressTracker()
    orchestrator = ResearchOrchestrator(
        planner=TodoPlanner(llm),
        search_tool=MockSearchTool(
            {
                "query a": [_source(1)],
                "query b": [_source(2)],
            }
        ),
        summarizer=TaskSummarizer(llm),
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=FailingReportWriter(),
        progress=progress,
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert result.report.markdown.startswith("# AI Research Agents")
    assert "## 概述" in result.report.markdown
    assert "## 分节分析" in result.report.markdown
    assert "## 总结" in result.report.markdown
    assert "## 参考文献" in result.report.markdown
    assert "https://example.com/source-1" in result.report.markdown
    assert "https://example.com/source-2" in result.report.markdown
    assert [log.stage for log in result.tool_logs][-2:] == ["report", "report"]
    assert result.tool_logs[-2].status == ToolCallStatus.FAILED
    assert result.tool_logs[-1].tool_name == "StructuredFallbackReportWriter"
    assert any(
        event.type == SSEEventType.REPORT and "兜底" not in str(event.data)
        for event in progress.events
    )
