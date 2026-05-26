import asyncio
import json
from collections.abc import Sequence

import pytest

from app.agents import AgentOutputError, ReportWriter, TaskSummarizer, TodoPlanner
from app.llm import LLMMessage
from app.models import ResearchTask, SearchResult, TaskStatus, TaskSummary


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


def test_todo_planner_parses_three_to_five_tasks() -> None:
    llm = MockLLM(
        [
            json.dumps(
                [
                    {
                        "title": "Market landscape",
                        "intent": "Map current vendors",
                        "query": "AI research agent market landscape",
                    },
                    {
                        "title": "Technical architecture",
                        "intent": "Identify common architecture patterns",
                        "query": "deep research agent architecture",
                    },
                    {
                        "title": "Evaluation",
                        "intent": "Find evaluation criteria",
                        "query": "AI agent research evaluation criteria",
                    },
                ]
            )
        ]
    )

    tasks = asyncio.run(TodoPlanner(llm).plan("AI research agents", max_tasks=3))

    assert len(tasks) == 3
    assert all(task.status == TaskStatus.PENDING for task in tasks)
    assert tasks[0] == ResearchTask(
        title="Market landscape",
        intent="Map current vendors",
        query="AI research agent market landscape",
        status=TaskStatus.PENDING,
    )


def test_todo_planner_retries_invalid_json_then_accepts_valid_tasks() -> None:
    llm = MockLLM(
        [
            "```json\n[]\n```",
            json.dumps(
                [
                    {"title": "A", "intent": "A intent", "query": "A query"},
                    {"title": "B", "intent": "B intent", "query": "B query"},
                    {"title": "C", "intent": "C intent", "query": "C query"},
                ]
            ),
        ]
    )

    tasks = asyncio.run(TodoPlanner(llm, retries=1).plan("Topic", max_tasks=3))

    assert len(tasks) == 3
    assert len(llm.calls) == 2


def test_todo_planner_raises_clear_error_for_bad_output() -> None:
    llm = MockLLM(["{\"tasks\": []}", "{\"tasks\": []}"])

    with pytest.raises(AgentOutputError, match="JSON array"):
        asyncio.run(TodoPlanner(llm, retries=1).plan("Topic", max_tasks=3))


def test_task_summarizer_returns_markdown_and_preserves_sources() -> None:
    task = ResearchTask(
        title="Market landscape",
        intent="Map current vendors",
        query="AI research agent market landscape",
    )
    sources = [
        SearchResult(
            title="Example report",
            url="https://example.com/report",
            snippet="Market detail.",
            source="example.com",
        )
    ]
    llm = MockLLM(["### Findings\nThe market is active [1]."])

    summary = asyncio.run(TaskSummarizer(llm).summarize(task, sources))

    assert summary.task_title == "Market landscape"
    assert summary.content == "### Findings\nThe market is active [1]."
    assert summary.sources == sources


def test_report_writer_combines_summaries_and_deduplicates_sources() -> None:
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )
    summaries = [
        TaskSummary(task_title="A", content="Summary A [1].", sources=[source]),
        TaskSummary(task_title="B", content="Summary B [1].", sources=[source]),
    ]
    llm = MockLLM(
        [
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [1]. 资料不足: only one unique source.",
                    "## Sectioned Analysis",
                    "Analysis text [1].",
                    "## Conclusion",
                    "Conclusion text [1].",
                    "## References",
                    "[1] https://example.com/report",
                ]
            )
        ]
    )

    report = asyncio.run(ReportWriter(llm).write("AI Research Agents", summaries))

    assert report.title == "AI Research Agents"
    assert "## Overview" in report.markdown
    assert "## References" in report.markdown
    assert report.sources == [source]
    assert "https://example.com/report" in llm.calls[0][1].content


def test_report_writer_rejects_missing_required_sections() -> None:
    llm = MockLLM(["# Report\n\nOnly body."])

    with pytest.raises(AgentOutputError, match="overview"):
        asyncio.run(ReportWriter(llm, retries=0).write("Topic", []))


def test_report_writer_retries_when_citation_quality_fails() -> None:
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )
    summaries = [
        TaskSummary(task_title="A", content="Summary A [1].", sources=[source]),
    ]
    llm = MockLLM(
        [
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [2]. 资料不足.",
                    "## Sectioned Analysis",
                    "Analysis text [2].",
                    "## Conclusion",
                    "Conclusion text [2].",
                    "## References",
                    "[1] https://example.com/report",
                ]
            ),
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [1]. 资料不足.",
                    "## Sectioned Analysis",
                    "Analysis text [1].",
                    "## Conclusion",
                    "Conclusion text [1].",
                    "## References",
                    "[1] https://example.com/report",
                ]
            ),
        ]
    )

    report = asyncio.run(ReportWriter(llm, retries=1).write("AI Research Agents", summaries))

    assert len(llm.calls) == 2
    assert "https://example.com/report" in report.markdown
    assert "missing citation marker" in llm.calls[1][-1].content


def test_report_writer_deterministically_appends_missing_reference_urls() -> None:
    source_a = SearchResult(
        title="Example report A",
        url="https://example.com/report-a",
        snippet="Market detail A.",
        source="example.com",
    )
    source_b = SearchResult(
        title="Example report B",
        url="https://example.com/report-b",
        snippet="Market detail B.",
        source="example.com",
    )
    source_c = SearchResult(
        title="Example report C",
        url="https://example.com/report-c",
        snippet="Market detail C.",
        source="example.com",
    )
    summaries = [
        TaskSummary(
            task_title="A",
            content="Summary A [1][2][3].",
            sources=[source_a, source_b, source_c],
        ),
    ]
    llm = MockLLM(
        [
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [1][2][3].",
                    "## Sectioned Analysis",
                    "Analysis text [1][2][3].",
                    "## Conclusion",
                    "Conclusion text [1][2][3].",
                    "## References",
                    "[1] https://example.com/report-a",
                    "[2] https://example.com/report-b",
                ]
            )
        ]
    )

    report = asyncio.run(ReportWriter(llm, retries=0).write("AI Research Agents", summaries))

    assert "[3] https://example.com/report-c" in report.markdown
