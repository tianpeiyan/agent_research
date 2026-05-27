import asyncio
import json
from collections.abc import Sequence

import pytest

from app.agents import (
    AgentOutputError,
    EvidenceJudge,
    QueryRewriter,
    ReportWriter,
    TaskSummarizer,
    TodoPlanner,
)
from app.llm import LLMMessage
from app.models import (
    EvidenceConfidence,
    EvidenceJudgement,
    ResearchTask,
    SearchResult,
    TaskStatus,
    TaskSummary,
)
from app.progress import ResearchProgressTracker
from app.skills import SkillRegistry
from app.tool_calling import ToolRegistry
from app.tools import NoteTool


class MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[LLMMessage]] = []
        self.supports_native_tools = False

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class MockReportWriter:
    async def write(self, topic: str, summaries, evidence_judgement=None):
        from app.models import FinalReport

        return FinalReport(title=topic, markdown="# Report", sources=[])


class MockSearchTool:
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        return []


def _action(action: str, arguments: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "arguments": arguments or {},
            "reason": f"Run {action}.",
        }
    )


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


def test_task_summarizer_can_load_skill_before_summarizing(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "research-task-summarizer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Summarize\n\nUse citations.", encoding="utf-8")
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
    llm = MockLLM(
        [
            _action("load_skill", {"skill_name": "research-task-summarizer"}),
            _action("final", {"content": "### Findings\nThe market is active [1]. 资料不足."}),
        ]
    )
    progress = ResearchProgressTracker()
    summarizer = TaskSummarizer(llm, skill_name="research-task-summarizer")
    registry = ToolRegistry(
        search_tool=MockSearchTool(),
        summarizer=summarizer,
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=MockReportWriter(),
        progress=progress,
        skill_registry=SkillRegistry(tmp_path / "skills"),
    )
    summarizer.configure_tool_runtime(registry, progress)

    summary = asyncio.run(summarizer.summarize(task, sources))

    assert summary.content == "### Findings\nThe market is active [1]. 资料不足."
    assert [log.tool_name for log in progress.tool_logs] == ["load_skill"]
    assert progress.tool_logs[0].status == "success"


def test_task_summarizer_falls_back_when_skill_load_fails(tmp_path) -> None:
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
    llm = MockLLM(
        [
            _action("load_skill", {"skill_name": "research-task-summarizer"}),
            "### Findings\nFallback summary [1]. 资料不足.",
        ]
    )
    progress = ResearchProgressTracker()
    summarizer = TaskSummarizer(llm, skill_name="research-task-summarizer")
    registry = ToolRegistry(
        search_tool=MockSearchTool(),
        summarizer=summarizer,
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=MockReportWriter(),
        progress=progress,
        skill_registry=SkillRegistry(tmp_path / "skills"),
    )
    summarizer.configure_tool_runtime(registry, progress)

    summary = asyncio.run(summarizer.summarize(task, sources))

    assert summary.content == "### Findings\nFallback summary [1]. 资料不足."
    assert [log.tool_name for log in progress.tool_logs] == ["load_skill"]
    assert progress.tool_logs[0].status == "failed"
    assert progress.tool_logs[0].error == "Skill not found: research-task-summarizer."


def test_evidence_judge_parses_structured_judgement() -> None:
    llm = MockLLM(
        [
            json.dumps(
                {
                    "is_sufficient": True,
                    "confidence": "high",
                    "gaps": [],
                    "rationale": "Three independent sources support the summaries.",
                }
            )
        ]
    )

    judgement = asyncio.run(EvidenceJudge(llm).judge("Topic", [], [], []))

    assert judgement.is_sufficient is True
    assert judgement.confidence == "high"
    assert judgement.gaps == []
    assert judgement.rationale == "Three independent sources support the summaries."


def test_evidence_judge_retries_invalid_json_then_accepts_judgement() -> None:
    llm = MockLLM(
        [
            "not json",
            json.dumps(
                {
                    "is_sufficient": False,
                    "confidence": "low",
                    "gaps": ["Need current primary sources."],
                    "rationale": "The evidence is too sparse.",
                }
            ),
        ]
    )

    judgement = asyncio.run(EvidenceJudge(llm, retries=1).judge("Topic", [], [], []))

    assert len(llm.calls) == 2
    assert judgement.is_sufficient is False
    assert judgement.gaps == ["Need current primary sources."]


def test_query_rewriter_parses_limits_and_deduplicates_queries() -> None:
    llm = MockLLM(
        [
            json.dumps(
                {
                    "queries": [
                        "existing query",
                        "new primary source query",
                        "new primary source query",
                        "second gap query",
                    ]
                }
            )
        ]
    )

    queries = asyncio.run(
        QueryRewriter(llm).rewrite(
            topic="Topic",
            evidence_gaps=["Need primary sources."],
            existing_task_queries=["existing query"],
            existing_source_summaries=["Existing source: summary."],
            max_queries=2,
        )
    )

    assert queries == ["new primary source query", "second gap query"]
    assert "Need primary sources." in llm.calls[0][1].content


def test_query_rewriter_retries_invalid_json_then_accepts_queries() -> None:
    llm = MockLLM(
        [
            "not json",
            json.dumps({"queries": ["authoritative update query"]}),
        ]
    )

    queries = asyncio.run(
        QueryRewriter(llm, retries=1).rewrite(
            topic="Topic",
            evidence_gaps=["Need current data."],
            existing_task_queries=[],
            existing_source_summaries=[],
            max_queries=2,
        )
    )

    assert len(llm.calls) == 2
    assert queries == ["authoritative update query"]


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


def test_report_writer_includes_evidence_status_in_prompt() -> None:
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )
    summaries = [
        TaskSummary(task_title="A", content="Summary A [1].", sources=[source]),
    ]
    judgement = EvidenceJudgement(
        is_sufficient=False,
        confidence=EvidenceConfidence.LOW,
        gaps=["Need primary source confirmation."],
        rationale="Only one secondary source supports the conclusion.",
    )
    llm = MockLLM(
        [
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [1]. 证据不足，待验证。",
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

    report = asyncio.run(
        ReportWriter(llm, retries=0).write(
            "AI Research Agents",
            summaries,
            evidence_judgement=judgement,
        )
    )

    assert "证据不足" in report.markdown
    assert "Need primary source confirmation." in llm.calls[0][1].content
    assert "marked the evidence as insufficient" in llm.calls[0][0].content


def test_report_writer_rejects_insufficient_evidence_without_risk_marking() -> None:
    source = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="Market detail.",
        source="example.com",
    )
    summaries = [
        TaskSummary(task_title="A", content="Summary A [1].", sources=[source]),
    ]
    judgement = EvidenceJudgement(
        is_sufficient=False,
        confidence=EvidenceConfidence.LOW,
        gaps=["Need independent corroboration."],
        rationale="Evidence is sparse.",
    )
    llm = MockLLM(
        [
            "\n".join(
                [
                    "# AI Research Agents",
                    "## Overview",
                    "Overview text [1].",
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

    with pytest.raises(AgentOutputError, match="insufficient evidence"):
        asyncio.run(
            ReportWriter(llm, retries=0).write(
                "AI Research Agents",
                summaries,
                evidence_judgement=judgement,
            )
        )


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
