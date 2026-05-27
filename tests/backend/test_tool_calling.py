import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.agents import TaskSummarizer
from app.llm import LLMMessage, LLMToolCallUnsupported
from app.models import (
    ResearchTask,
    SearchResult,
    ToolCallRequest,
    ToolCallingTurn,
    ToolExecutionError,
)
from app.progress import ResearchProgressTracker
from app.skills import SkillRegistry
from app.tool_calling import (
    JSONFallbackActionParser,
    ToolCallingAgentRunner,
    ToolCallingResearchExecutor,
    ToolExecutionContext,
    ToolRegistry,
)
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


class NativeMockLLM:
    supports_native_tools = True

    def __init__(
        self,
        turns: list[ToolCallingTurn | LLMToolCallUnsupported],
        responses: list[str],
    ) -> None:
        self.turns = turns
        self.responses = responses
        self.tool_calls: list[list[LLMMessage]] = []
        self.complete_calls: list[list[LLMMessage]] = []
        self.tool_definitions: list[list[str]] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        self.complete_calls.append(list(messages))
        return self.responses.pop(0)

    async def complete_with_tools(
        self,
        messages: Sequence[LLMMessage],
        tools,
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> ToolCallingTurn:
        self.tool_calls.append(list(messages))
        self.tool_definitions.append([tool.name for tool in tools])
        turn = self.turns.pop(0)
        if isinstance(turn, LLMToolCallUnsupported):
            raise turn
        return turn


class MockSearchTool:
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        if task.query == "secret failure":
            raise ToolError(
                "search_failed",
                "Failed with TAVILY_API_KEY=tvly-secret and Bearer abc123.",
            )
        return [
            SearchResult(
                title="Source",
                url="https://example.com/source",
                snippet="Snippet.",
                source="example.com",
            )
        ]


class DuplicateSearchTool:
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Source {task.query}",
                url="https://example.com/source",
                snippet=f"Snippet {task.query}.",
                source="example.com",
            )
        ]


class FailingNoteTool:
    def save(self, summary, tags=None):
        raise RuntimeError("disk failed with token=note-secret")


class MockReportWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def write(self, topic: str, summaries, evidence_judgement=None):
        from app.models import FinalReport

        self.calls.append(
            {
                "topic": topic,
                "summaries": list(summaries),
                "evidence_judgement": evidence_judgement,
            }
        )
        return FinalReport(
            title=topic,
            markdown=(
                f"# {topic}\n\n"
                "## Overview\nDone [1].\n\n"
                "## Conclusion\nDone [1].\n\n"
                "## References\n[1] https://example.com/source"
            ),
            sources=[source for summary in summaries for source in summary.sources],
        )


class MockEvidenceJudge:
    async def judge(self, topic, completed_summaries, sources, failed_tasks):
        from app.models import EvidenceConfidence, EvidenceJudgement

        return EvidenceJudgement(
            is_sufficient=True,
            confidence=EvidenceConfidence.HIGH,
            gaps=[],
            rationale="Enough evidence.",
        )


class MockQueryRewriter:
    def __init__(self, queries: list[str] | None = None) -> None:
        self.queries = queries or ["supplemental query"]
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


def _task() -> ResearchTask:
    return ResearchTask(
        title="Task A",
        intent="Intent A",
        query="query a",
    )


def _registry(
    tmp_path: Path,
    llm: MockLLM | None = None,
    skill_registry: SkillRegistry | None = None,
) -> ToolRegistry:
    return ToolRegistry(
        search_tool=MockSearchTool(),
        summarizer=TaskSummarizer(llm or MockLLM(["Summary [1]."])),
        note_tool=NoteTool(tmp_path / "notes.jsonl"),
        report_writer=MockReportWriter(),
        progress=ResearchProgressTracker(),
        skill_registry=skill_registry,
    )


def _registry_with(
    tmp_path: Path,
    search_tool=None,
    llm: MockLLM | NativeMockLLM | None = None,
    note_tool=None,
    progress: ResearchProgressTracker | None = None,
    skill_registry: SkillRegistry | None = None,
    evidence_judge=None,
    query_rewriter=None,
    report_writer=None,
) -> ToolRegistry:
    return ToolRegistry(
        search_tool=search_tool or MockSearchTool(),
        summarizer=TaskSummarizer(llm or MockLLM(["Summary [1]."])),
        note_tool=note_tool or NoteTool(tmp_path / "notes.jsonl"),
        report_writer=report_writer or MockReportWriter(),
        progress=progress or ResearchProgressTracker(),
        skill_registry=skill_registry,
        evidence_judge=evidence_judge,
        query_rewriter=query_rewriter,
    )


def test_json_fallback_parser_accepts_valid_action_and_final() -> None:
    parser = JSONFallbackActionParser()

    request = parser.parse(
        json.dumps(
            {
                "action": "search_web",
                "arguments": {"query": "AI agents"},
                "reason": "Need sources.",
            }
        ),
        {"search_web"},
    )
    final = parser.parse(
        json.dumps({"action": "final", "arguments": {}, "reason": "Done."}),
        {"search_web"},
    )

    assert request.action == "search_web"
    assert request.arguments == {"query": "AI agents"}
    assert final.action == "final"


def test_json_fallback_parser_returns_testable_errors() -> None:
    parser = JSONFallbackActionParser()

    with pytest.raises(ToolExecutionError) as invalid_json:
        parser.parse("```json\n{}\n```", {"search_web"})
    with pytest.raises(ToolExecutionError) as missing_field:
        parser.parse(json.dumps({"action": "search_web"}), {"search_web"})
    with pytest.raises(ToolExecutionError) as unknown_tool:
        parser.parse(
            json.dumps({"action": "shell", "arguments": {}, "reason": "Run it."}),
            {"search_web"},
        )

    assert invalid_json.value.code == "invalid_json"
    assert missing_field.value.code == "missing_action_field"
    assert unknown_tool.value.code == "unregistered_tool"


def test_tool_registry_validates_arguments_and_redacts_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    context = ToolExecutionContext(topic="Topic", task=_task())

    invalid = asyncio.run(
        registry.execute(
            parser_request("search_web", {}),
            context,
        )
    )
    failed = asyncio.run(
        registry.execute(
            parser_request("search_web", {"query": "secret failure"}),
            context,
        )
    )

    assert invalid.success is False
    assert invalid.error_code == "invalid_tool_arguments"
    assert failed.success is False
    assert "tvly-secret" not in failed.error_message
    assert "abc123" not in failed.error_message
    assert "<redacted>" in failed.error_message


def test_tool_registry_rejects_unregistered_tools_and_bad_argument_types(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    context = ToolExecutionContext(topic="Topic", task=_task())

    unregistered = asyncio.run(
        registry.execute(
            ToolCallRequest(action="shell", arguments={}, reason="Run shell."),
            context,
        )
    )
    invalid_type = asyncio.run(
        registry.execute(
            ToolCallRequest(
                action="search_web",
                arguments={"query": 123},
                reason="Bad type.",
            ),
            context,
        )
    )

    assert unregistered.success is False
    assert unregistered.error_code == "unregistered_tool"
    assert invalid_type.success is False
    assert invalid_type.error_code == "invalid_tool_arguments"


def test_tool_registry_runs_existing_tools_through_one_path(tmp_path: Path) -> None:
    llm = MockLLM(["Summary [1]."])
    registry = _registry(tmp_path, llm=llm)
    context = ToolExecutionContext(topic="Topic", task=_task())

    search = asyncio.run(registry.execute(parser_request("search_web", {"query": "query a"}), context))
    summary = asyncio.run(registry.execute(parser_request("summarize_task", {}), context))
    note = asyncio.run(registry.execute(parser_request("save_note", {"tags": ["research"]}), context))

    assert search.success is True
    assert len(context.search_results) == 1
    assert summary.success is True
    assert context.summary is not None
    assert note.success is True
    assert (tmp_path / "notes.jsonl").exists()


def test_tool_registry_deduplicates_repeated_search_results(tmp_path: Path) -> None:
    registry = _registry_with(tmp_path, search_tool=DuplicateSearchTool())
    context = ToolExecutionContext(topic="Topic", task=_task())

    first = asyncio.run(registry.execute(parser_request("search_web", {"query": "first"}), context))
    second = asyncio.run(registry.execute(parser_request("search_web", {"query": "second"}), context))

    assert first.success is True
    assert second.success is True
    assert len(context.search_results) == 1
    assert context.search_results[0].title == "Source first"


def test_tool_registry_loads_skill_manual(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo\n\nManual.", encoding="utf-8")
    registry = _registry(
        tmp_path,
        skill_registry=SkillRegistry(tmp_path / "skills"),
    )

    result = asyncio.run(
        registry.execute(
            parser_request("load_skill", {"skill_name": "foo"}),
            ToolExecutionContext(topic="Topic"),
        )
    )

    assert result.success is True
    assert result.result["skill"]["name"] == "foo"
    assert result.result["skill"]["content"] == "# Foo\n\nManual."


def test_tool_registry_returns_testable_skill_errors(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        skill_registry=SkillRegistry(tmp_path / "skills"),
    )

    missing = asyncio.run(
        registry.execute(
            parser_request("load_skill", {"skill_name": "missing"}),
            ToolExecutionContext(topic="Topic"),
        )
    )
    traversal = asyncio.run(
        registry.execute(
            ToolCallRequest(
                action="load_skill",
                arguments={"skill_name": "../outside"},
                reason="Try traversal.",
            ),
            ToolExecutionContext(topic="Topic"),
        )
    )

    assert missing.success is False
    assert missing.error_code == "skill_not_found"
    assert traversal.success is False
    assert traversal.error_code == "invalid_skill_name"


def test_tool_registry_judges_evidence_and_updates_context(tmp_path: Path) -> None:
    progress = ResearchProgressTracker()
    registry = _registry_with(
        tmp_path,
        progress=progress,
        evidence_judge=MockEvidenceJudge(),
    )
    context = ToolExecutionContext(topic="Topic")

    result = asyncio.run(registry.execute(parser_request("judge_evidence", {}), context))

    assert result.success is True
    assert context.evidence_judgement is not None
    assert context.evidence_judgement.is_sufficient is True
    assert result.result["evidence_judgement"]["confidence"] == "high"
    assert progress.tool_logs[-1].stage == "evidence"
    assert progress.tool_logs[-1].tool_name == "judge_evidence"


def test_tool_registry_rewrites_queries_from_evidence_context(tmp_path: Path) -> None:
    from app.models import EvidenceConfidence, EvidenceJudgement

    rewriter = MockQueryRewriter(["query one", "query two", "query three"])
    progress = ResearchProgressTracker()
    registry = _registry_with(
        tmp_path,
        progress=progress,
        query_rewriter=rewriter,
    )
    context = ToolExecutionContext(
        topic="Topic",
        planned_tasks=[ResearchTask(title="Task", intent="Intent", query="existing query")],
        sources=[
            SearchResult(
                title="Source",
                url="https://example.com/source",
                snippet="Snippet.",
                source="example.com",
            )
        ],
        evidence_judgement=EvidenceJudgement(
            is_sufficient=False,
            confidence=EvidenceConfidence.LOW,
            gaps=["Need primary data."],
            rationale="Sparse evidence.",
        ),
    )

    result = asyncio.run(
        registry.execute(parser_request("rewrite_queries", {"max_queries": 2}), context)
    )

    assert result.success is True
    assert result.result == {"queries": ["query one", "query two"]}
    assert context.supplemental_queries == ["query one", "query two"]
    assert rewriter.calls[0]["evidence_gaps"] == ["Need primary data."]
    assert rewriter.calls[0]["existing_task_queries"] == ["existing query"]
    assert progress.tool_logs[-1].stage == "query_rewrite"
    assert progress.tool_logs[-1].output_summary == "2 queries"


def test_tool_registry_passes_evidence_judgement_to_report_writer(tmp_path: Path) -> None:
    from app.models import EvidenceConfidence, EvidenceJudgement, TaskSummary

    source = SearchResult(
        title="Source",
        url="https://example.com/source",
        snippet="Snippet.",
        source="example.com",
    )
    judgement = EvidenceJudgement(
        is_sufficient=False,
        confidence=EvidenceConfidence.LOW,
        gaps=["Need more corroboration."],
        rationale="Sparse evidence.",
    )
    report_writer = MockReportWriter()
    registry = _registry_with(tmp_path, report_writer=report_writer)
    context = ToolExecutionContext(
        topic="Topic",
        completed_summaries=[
            TaskSummary(task_title="Task", content="Summary [1].", sources=[source])
        ],
        evidence_judgement=judgement,
    )

    result = asyncio.run(registry.execute(parser_request("write_report", {}), context))

    assert result.success is True
    assert context.report is not None
    assert report_writer.calls[0]["evidence_judgement"] == judgement


def test_tool_calling_agent_runner_runs_json_fallback_for_generic_agent(tmp_path: Path) -> None:
    llm = MockLLM(
        [
            action_json("search_web", {"query": "query a"}),
            action_json("final", {}),
        ]
    )
    registry = _registry(tmp_path, llm=llm)
    runner = ToolCallingAgentRunner[int](
        llm=llm,
        registry=registry,
        progress=ResearchProgressTracker(),
    )
    context = ToolExecutionContext(topic="Topic", task=_task())

    result = asyncio.run(
        runner.run(
            agent_name="GenericAgent",
            goal="Find one source, then finish.",
            system_prompt="Return strict JSON tool actions.",
            context=context,
            stop_condition=lambda request, _context: request.action == "final",
            output_parser=lambda _request, run_context: len(run_context.search_results),
            max_tool_calls=2,
        )
    )

    assert result == 1
    assert llm.calls[0][0].content == "Return strict JSON tool actions."
    assert llm.calls[0][1].content == "Find one source, then finish."


def test_tool_calling_agent_runner_shares_complete_tool_definitions_across_agents(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    first_llm = NativeMockLLM([ToolCallingTurn(content="Done.", tool_calls=[])], [])
    second_llm = NativeMockLLM([ToolCallingTurn(content="Done.", tool_calls=[])], [])
    expected_tools = [
        "search_web",
        "summarize_task",
        "save_note",
        "write_report",
        "load_skill",
        "judge_evidence",
        "rewrite_queries",
    ]

    for agent_name, llm in [("FirstAgent", first_llm), ("SecondAgent", second_llm)]:
        runner = ToolCallingAgentRunner[str](
            llm=llm,
            registry=registry,
            progress=ResearchProgressTracker(),
        )
        result = asyncio.run(
            runner.run(
                agent_name=agent_name,
                goal="Finish immediately.",
                system_prompt="Use tools only if needed.",
                context=ToolExecutionContext(topic="Topic", task=_task()),
                stop_condition=lambda request, _context: request.action == "final",
                output_parser=lambda _request, _context: agent_name,
                max_tool_calls=1,
            )
        )

        assert result == agent_name
        assert llm.tool_definitions == [expected_tools]


def test_tool_calling_agent_runner_rejects_unregistered_native_tool(
    tmp_path: Path,
) -> None:
    llm = NativeMockLLM(
        turns=[native_turn("shell", {})],
        responses=[],
    )
    runner = ToolCallingAgentRunner[None](
        llm=llm,
        registry=_registry(tmp_path),
        progress=ResearchProgressTracker(),
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(
            runner.run(
                agent_name="GenericAgent",
                goal="Try an unregistered tool.",
                system_prompt="Use tools.",
                context=ToolExecutionContext(topic="Topic", task=_task()),
                stop_condition=lambda request, _context: request.action == "final",
                output_parser=lambda _request, _context: None,
                max_tool_calls=1,
            )
        )

    assert exc_info.value.code == "unregistered_tool"


def test_tool_calling_agent_runner_can_call_load_skill_and_continue(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo\n\nManual.", encoding="utf-8")
    llm = MockLLM(
        [
            action_json("load_skill", {"skill_name": "foo"}),
            action_json("final", {}),
        ]
    )
    progress = ResearchProgressTracker()
    registry = _registry_with(
        tmp_path,
        llm=llm,
        progress=progress,
        skill_registry=SkillRegistry(tmp_path / "skills"),
    )
    runner = ToolCallingAgentRunner[str](
        llm=llm,
        registry=registry,
        progress=progress,
    )

    result = asyncio.run(
        runner.run(
            agent_name="GenericAgent",
            goal="Load a skill, then finish.",
            system_prompt="Return strict JSON tool actions.",
            context=ToolExecutionContext(topic="Topic"),
            stop_condition=lambda request, _context: request.action == "final",
            output_parser=lambda _request, _context: "done",
            max_tool_calls=2,
        )
    )

    assert result == "done"
    assert [log.tool_name for log in progress.tool_logs] == ["load_skill"]
    assert progress.tool_logs[0].stage == "skill"


def test_tool_calling_executor_runs_native_tool_call_flow(tmp_path: Path) -> None:
    llm = NativeMockLLM(
        turns=[
            native_turn("search_web", {"query": "query a"}),
            native_turn("summarize_task", {}),
            native_turn("save_note", {}),
            ToolCallingTurn(content="Done.", tool_calls=[]),
        ],
        responses=["Summary [1]."],
    )
    progress = ResearchProgressTracker()
    registry = _registry_with(tmp_path, llm=llm, progress=progress)
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=progress,
    )

    search_results, summary, note = asyncio.run(executor.run_task("Topic", _task()))

    assert len(search_results) == 1
    assert summary.content == "Summary [1]."
    assert note.task_title == "Task A"
    assert len(llm.tool_calls) == 4
    assert llm.tool_calls[1][-2].role == "assistant"
    assert llm.tool_calls[1][-2].tool_calls[0]["function"]["name"] == "search_web"
    assert llm.tool_calls[1][-1].role == "tool"
    assert llm.tool_calls[1][-1].tool_call_id == "call_search_web"
    assert [log.stage for log in progress.tool_logs] == ["search", "summary", "note"]


def test_tool_calling_executor_downgrades_native_unsupported_to_json_fallback(
    tmp_path: Path,
) -> None:
    llm = NativeMockLLM(
        turns=[LLMToolCallUnsupported("tools not supported")],
        responses=[
            action_json("search_web", {"query": "query a"}),
            action_json("summarize_task", {}),
            "Summary [1].",
            action_json("save_note", {}),
            action_json("final", {}),
        ],
    )
    progress = ResearchProgressTracker()
    registry = _registry_with(tmp_path, llm=llm, progress=progress)
    executor = ToolCallingResearchExecutor(llm=llm, registry=registry, progress=progress)

    _search_results, summary, _note = asyncio.run(executor.run_task("Topic", _task()))

    assert summary.content == "Summary [1]."
    assert len(llm.tool_calls) == 1
    assert any(event.data["message"] == "已降级为 JSON 工具调用" for event in progress.events)


def test_tool_calling_executor_fails_after_json_parse_retries(tmp_path: Path) -> None:
    llm = MockLLM(["not json", "still not json", "also not json"])
    registry = _registry(tmp_path, llm=llm)
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=ResearchProgressTracker(),
        fallback_parse_retries=2,
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "invalid_json"
    assert len(llm.calls) == 3


def test_tool_calling_executor_enforces_task_tool_call_limit(tmp_path: Path) -> None:
    llm = MockLLM(
        [
            action_json("search_web", {"query": "query a"}),
            action_json("search_web", {"query": "query a"}),
        ]
    )
    registry = _registry(tmp_path, llm=llm)
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=ResearchProgressTracker(),
        max_task_tool_calls=1,
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "tool_call_limit_exceeded"


def test_tool_calling_executor_rejects_final_without_summary(tmp_path: Path) -> None:
    llm = MockLLM([action_json("final", {})])
    registry = _registry(tmp_path, llm=llm)
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=ResearchProgressTracker(),
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "missing_summary"


def test_tool_calling_executor_surfaces_summary_failure(tmp_path: Path) -> None:
    llm = MockLLM(
        [
            action_json("search_web", {"query": "query a"}),
            action_json("summarize_task", {}),
            "",
        ]
    )
    registry = _registry(tmp_path, llm=llm)
    executor = ToolCallingResearchExecutor(
        llm=llm,
        registry=registry,
        progress=ResearchProgressTracker(),
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "tool_execution_failed"


def test_tool_calling_executor_surfaces_save_note_failure(tmp_path: Path) -> None:
    llm = MockLLM(
        [
            action_json("search_web", {"query": "query a"}),
            action_json("summarize_task", {}),
            "Summary [1].",
            action_json("save_note", {}),
        ]
    )
    progress = ResearchProgressTracker()
    registry = _registry_with(
        tmp_path,
        llm=llm,
        note_tool=FailingNoteTool(),
        progress=progress,
    )
    executor = ToolCallingResearchExecutor(llm=llm, registry=registry, progress=progress)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "tool_execution_failed"
    assert "note-secret" not in str(exc_info.value)
    assert "note-secret" not in (progress.tool_logs[-1].error or "")
    assert "<redacted>" in (progress.tool_logs[-1].error or "")


def test_tool_calling_executor_enforces_global_tool_call_limit(tmp_path: Path) -> None:
    progress = ResearchProgressTracker()
    registry = _registry(tmp_path, llm=MockLLM(["Summary [1]."]))
    executor = ToolCallingResearchExecutor(
        llm=MockLLM(
            [
                action_json("search_web", {"query": "query a"}),
                action_json("summarize_task", {}),
            ]
        ),
        registry=registry,
        progress=progress,
        max_global_tool_calls=1,
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.run_task("Topic", _task()))

    assert exc_info.value.code == "global_tool_call_limit_exceeded"


def parser_request(action: str, arguments: dict[str, object]):
    return JSONFallbackActionParser().parse(
        json.dumps(
            {
                "action": action,
                "arguments": arguments,
                "reason": "Test action.",
            }
        ),
        {
            "search_web",
            "summarize_task",
            "save_note",
            "write_report",
            "load_skill",
            "judge_evidence",
            "rewrite_queries",
        },
    )


def action_json(action: str, arguments: dict[str, object]) -> str:
    return json.dumps(
        {
            "action": action,
            "arguments": arguments,
            "reason": "Test action.",
        }
    )


def native_turn(action: str, arguments: dict[str, object]) -> ToolCallingTurn:
    return ToolCallingTurn(
        tool_calls=[
            ToolCallRequest(
                action=action,
                arguments=arguments,
                reason="Native tool call.",
                call_id=f"call_{action}",
            )
        ]
    )
