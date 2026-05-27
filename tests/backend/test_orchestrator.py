import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from app.agents import ReportWriter, TaskSummarizer, TodoPlanner
from app.llm import LLMMessage
from app.models import ResearchTask, SSEEventType, SearchResult, TaskStatus, ToolCallStatus
from app.orchestrator import ResearchOrchestrator
from app.progress import ResearchProgressTracker
from app.tool_calling import ToolCallingResearchExecutor, ToolRegistry
from app.tools import NoteTool, ToolError


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


def _action(action: str, arguments: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "arguments": arguments or {},
            "reason": f"Run {action}.",
        }
    )


def _task_actions(query: str) -> list[str]:
    return [
        _action("search_web", {"query": query}),
        _action("summarize_task"),
        _action("save_note"),
        _action("final"),
    ]


def _task_script(query: str, summary: str) -> list[str]:
    return [
        _action("search_web", {"query": query}),
        _action("summarize_task"),
        summary,
        _action("save_note"),
        _action("final"),
    ]


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
    def __init__(self, events: list[str], llm: MockLLM) -> None:
        self.events = events
        self.llm = llm

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
        self.evidence_judgements = []

    async def write(self, topic: str, summaries, evidence_judgement=None):
        self.events.append("report")
        self.evidence_judgements.append(evidence_judgement)
        from app.models import FinalReport

        sources = [source for summary in summaries for source in summary.sources]
        evidence_note = ""
        if evidence_judgement is not None and not evidence_judgement.is_sufficient:
            evidence_note = "证据不足：部分结论待验证，仅供参考。"
        return FinalReport(
            title=topic,
            markdown=(
                f"# {topic}\n\n"
                "## Overview\nDone.\n\n"
                f"{evidence_note}\n\n"
                "## Sectioned Analysis\nDone.\n\n"
                "## Conclusion\nDone.\n\n"
                "## References\n"
                + "\n".join(str(source.url) for source in sources)
            ),
            sources=sources,
        )


class OrderedEvidenceJudge:
    def __init__(self, events: list[str], is_sufficient: bool = True) -> None:
        self.events = events
        self.is_sufficient = is_sufficient

    async def judge(self, topic, completed_summaries, sources, failed_tasks):
        self.events.append("evidence")
        from app.models import EvidenceConfidence, EvidenceJudgement

        return EvidenceJudgement(
            is_sufficient=self.is_sufficient,
            confidence=EvidenceConfidence.HIGH if self.is_sufficient else EvidenceConfidence.LOW,
            gaps=[] if self.is_sufficient else ["Need more sources."],
            rationale="Enough evidence." if self.is_sufficient else "Evidence is incomplete.",
        )


class RecordingEvidenceJudge:
    def __init__(self, events: list[str], sufficiency_by_call: list[bool]) -> None:
        self.events = events
        self.sufficiency_by_call = sufficiency_by_call
        self.sources_by_call: list[list[SearchResult]] = []

    async def judge(self, topic, completed_summaries, sources, failed_tasks):
        self.events.append("evidence")
        self.sources_by_call.append(list(sources))
        from app.models import EvidenceConfidence, EvidenceJudgement

        is_sufficient = self.sufficiency_by_call.pop(0)
        return EvidenceJudgement(
            is_sufficient=is_sufficient,
            confidence=EvidenceConfidence.HIGH if is_sufficient else EvidenceConfidence.LOW,
            gaps=[] if is_sufficient else ["Need stronger source coverage."],
            rationale="Enough evidence." if is_sufficient else "Evidence is incomplete.",
        )


class OrderedQueryRewriter:
    def __init__(self, events: list[str], queries: list[str]) -> None:
        self.events = events
        self.queries = queries
        self.calls: list[dict[str, object]] = []

    async def rewrite(
        self,
        *,
        topic,
        evidence_gaps,
        existing_task_queries,
        existing_source_summaries,
        max_queries=2,
    ):
        self.events.append("rewrite")
        self.calls.append(
            {
                "topic": topic,
                "evidence_gaps": list(evidence_gaps),
                "existing_task_queries": list(existing_task_queries),
                "existing_source_summaries": list(existing_source_summaries),
                "max_queries": max_queries,
            }
        )
        return self.queries[:max_queries]


class SupplementalSearchTool:
    def __init__(self, events: list[str], results_by_query: dict[str, list[SearchResult]]) -> None:
        self.events = events
        self.results_by_query = results_by_query
        self.calls: list[str] = []

    async def search(self, task: ResearchTask) -> list[SearchResult]:
        self.events.append(f"search:{task.query}")
        self.calls.append(task.query)
        if task.query == "failed supplemental":
            raise ToolError("search_failed", "Supplemental search failed.")
        return self.results_by_query[task.query]


class FailingEvidenceJudge:
    async def judge(self, topic, completed_summaries, sources, failed_tasks):
        raise RuntimeError("judge failed with token=evidence-secret")


class FailingReportWriter:
    async def write(self, topic: str, summaries, evidence_judgement=None):
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
            *_task_script("query a", "### Summary A\nFinding A [1]."),
            *_task_script("query b", "### Summary B\nFinding B [1]."),
            *_task_script("query c", "### Summary C\nFinding C [1]."),
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
    assert status_messages[0] == "正在规划"
    assert status_messages.count("已降级为 JSON 工具调用") == 3
    assert status_messages.count("模型正在选择工具") == 12
    assert status_messages.count("正在执行工具") == 9
    assert status_messages.count("任务完成") == 3
    assert status_messages[-1] == "报告生成完成"
    event_types = [event.type for event in progress.events]
    assert event_types.count(SSEEventType.SEARCH_RESULTS) == 3
    assert event_types.count(SSEEventType.SUMMARY) == 3
    assert event_types[-3:] == [
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
            *_task_script("query a", "### Summary A\nFinding A [1]."),
            _action("search_web", {"query": "failed query"}),
            *_task_script("query c", "### Summary C\nFinding C [1]."),
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
            ),
            _action("search_web", {"query": "failed query"}),
            _action("search_web", {"query": "failed query"}),
            _action("search_web", {"query": "failed query"}),
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
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
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


def test_orchestrator_builds_langgraph_state_graph(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM([])
    progress = ResearchProgressTracker()
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        progress=progress,
    )
    registry = ToolRegistry(
        search_tool=orchestrator.search_tool,
        summarizer=orchestrator.summarizer,
        note_tool=orchestrator.note_tool,
        report_writer=orchestrator.report_writer,
        progress=progress,
    )
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=progress,
    )

    graph = orchestrator._build_graph(executor)

    assert isinstance(graph, CompiledStateGraph)
    assert {"plan", "execute_task", "write_report"} <= set(graph.get_graph().nodes)


def test_orchestrator_graph_contains_evidence_and_supplemental_search_edges(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    llm = MockLLM([])
    progress = ResearchProgressTracker()
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=OrderedEvidenceJudge(events),
        query_rewriter=OrderedQueryRewriter(events, ["supplemental query"]),
        progress=progress,
    )
    registry = ToolRegistry(
        search_tool=orchestrator.search_tool,
        summarizer=orchestrator.summarizer,
        note_tool=orchestrator.note_tool,
        report_writer=orchestrator.report_writer,
        progress=progress,
        evidence_judge=orchestrator.evidence_judge,
        query_rewriter=orchestrator.query_rewriter,
    )
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=progress,
    )

    graph = orchestrator._build_graph(executor).get_graph()
    nodes = set(graph.nodes)
    edges = {(edge.source, edge.target, edge.conditional) for edge in graph.edges}

    assert {
        "plan",
        "execute_task",
        "judge_evidence",
        "rewrite_queries",
        "supplemental_search",
        "write_report",
    } <= nodes
    assert {
        ("execute_task", "judge_evidence", True),
        ("judge_evidence", "rewrite_queries", True),
        ("judge_evidence", "write_report", True),
        ("rewrite_queries", "supplemental_search", False),
        ("supplemental_search", "judge_evidence", False),
    } <= edges


def test_orchestrator_runs_evidence_judge_before_report(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=OrderedEvidenceJudge(events, is_sufficient=False),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert events[-2:] == ["evidence", "report"]
    assert [log.stage for log in result.tool_logs][-2:] == ["evidence", "report"]
    assert result.tool_logs[-2].tool_name == "judge_evidence"
    assert result.tool_logs[-2].output_summary == "sufficient=False, confidence=low"


def test_orchestrator_continues_when_evidence_judge_fails(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=FailingEvidenceJudge(),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert events[-1] == "report"
    assert result.report.markdown.startswith("# AI Research Agents")
    evidence_log = result.tool_logs[-2]
    assert evidence_log.stage == "evidence"
    assert evidence_log.status == ToolCallStatus.FAILED
    assert "evidence-secret" not in (evidence_log.error or "")
    assert "<redacted>" in (evidence_log.error or "")


def test_orchestrator_runs_one_supplemental_search_round_when_evidence_is_insufficient(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    evidence_judge = RecordingEvidenceJudge(events, [False, True])
    query_rewriter = OrderedQueryRewriter(
        events,
        ["supplemental duplicate", "supplemental new", "unused query"],
    )
    search_tool = SupplementalSearchTool(
        events,
        {
            "query a": [_source(1)],
            "query b": [_source(2)],
            "query c": [_source(3)],
            "supplemental duplicate": [_source(2)],
            "supplemental new": [_source(4)],
        },
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=search_tool,
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=evidence_judge,
        query_rewriter=query_rewriter,
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert search_tool.calls == [
        "query a",
        "query b",
        "query c",
        "supplemental duplicate",
        "supplemental new",
    ]
    assert [len(sources) for sources in evidence_judge.sources_by_call] == [3, 4]
    assert query_rewriter.calls[0]["max_queries"] == 2
    assert events[-6:] == [
        "evidence",
        "rewrite",
        "search:supplemental duplicate",
        "search:supplemental new",
        "evidence",
        "report",
    ]
    assert [log.stage for log in result.tool_logs][-6:] == [
        "evidence",
        "query_rewrite",
        "search",
        "search",
        "evidence",
        "report",
    ]
    assert result.tool_logs[-5].output_summary == "2 queries"


def test_orchestrator_limits_supplemental_search_to_one_round(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    evidence_judge = RecordingEvidenceJudge(events, [False, False])
    query_rewriter = OrderedQueryRewriter(events, ["supplemental one", "supplemental two"])
    search_tool = SupplementalSearchTool(
        events,
        {
            "query a": [_source(1)],
            "query b": [_source(2)],
            "query c": [_source(3)],
            "supplemental one": [_source(4)],
            "supplemental two": [_source(5)],
        },
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=search_tool,
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=evidence_judge,
        query_rewriter=query_rewriter,
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert search_tool.calls.count("supplemental one") == 1
    assert search_tool.calls.count("supplemental two") == 1
    assert events.count("rewrite") == 1
    assert events.count("evidence") == 2
    assert events[-1] == "report"
    assert result.report.markdown.startswith("# AI Research Agents")
    assert "证据不足" in result.report.markdown
    assert "仅供参考" in result.report.markdown


def test_orchestrator_continues_when_supplemental_search_fails(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=SupplementalSearchTool(
            events,
            {
                "query a": [_source(1)],
                "query b": [_source(2)],
                "query c": [_source(3)],
            },
        ),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=OrderedReportWriter(events),
        evidence_judge=RecordingEvidenceJudge(events, [False, False]),
        query_rewriter=OrderedQueryRewriter(events, ["failed supplemental"]),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert result.report.markdown.startswith("# AI Research Agents")
    assert events[-1] == "report"
    failed_search_logs = [
        log for log in result.tool_logs if log.stage == "search" and log.status == ToolCallStatus.FAILED
    ]
    assert len(failed_search_logs) == 1
    assert failed_search_logs[0].error == "Supplemental search failed."


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
            ),
            _action("search_web", {"query": "query a"}),
            _action("search_web", {"query": "query b"}),
            _action("search_web", {"query": "query c"}),
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
            *_task_script("query a", "### Summary A\nFinding A [1]."),
            *_task_script("query b", "### Summary B\nFinding B [1]."),
            _action("search_web", {"query": "failed query"}),
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


def test_orchestrator_fallback_report_includes_evidence_status(tmp_path: Path) -> None:
    events: list[str] = []
    llm = MockLLM(
        [
            *_task_actions("query a"),
            *_task_actions("query b"),
            *_task_actions("query c"),
        ]
    )
    orchestrator = ResearchOrchestrator(
        planner=OrderedPlanner(events),
        search_tool=OrderedSearchTool(events),
        summarizer=OrderedSummarizer(events, llm),
        note_tool=OrderedNoteTool(events, NoteTool(tmp_path / "notes.jsonl")),
        report_writer=FailingReportWriter(),
        evidence_judge=OrderedEvidenceJudge(events, is_sufficient=False),
    )

    result = asyncio.run(orchestrator.run("AI Research Agents", max_tasks=3))

    assert "证据不足" in result.report.markdown
    assert "Need more sources." in result.report.markdown
    assert result.tool_logs[-1].tool_name == "StructuredFallbackReportWriter"
