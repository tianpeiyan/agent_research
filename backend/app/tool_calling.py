import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.agents import EvidenceJudge, QueryRewriter, ReportWriter, TaskSummarizer
from app.llm import LLMMessage, LLMProvider, LLMToolCallUnsupported
from app.models import (
    FinalReport,
    EvidenceJudgement,
    NoteRecord,
    ResearchTask,
    SearchResult,
    TaskSummary,
    ToolCallRequest,
    ToolCallResult,
    ToolCallStatus,
    ToolDefinition,
    ToolExecutionError,
)
from app.progress import ResearchProgressTracker
from app.skills import SkillRegistry
from app.tools import NoteTool, ToolError


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SearchTool(Protocol):
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        pass


class SearchWebArgs(BaseModel):
    query: NonEmptyString

    model_config = ConfigDict(extra="forbid")


class SummarizeTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveNoteArgs(BaseModel):
    tags: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class WriteReportArgs(BaseModel):
    topic: NonEmptyString | None = None

    model_config = ConfigDict(extra="forbid")


class LoadSkillArgs(BaseModel):
    skill_name: NonEmptyString

    model_config = ConfigDict(extra="forbid")


class JudgeEvidenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RewriteQueriesArgs(BaseModel):
    max_queries: int = Field(default=2, ge=1, le=2)

    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolExecutionContext:
    topic: str
    task: ResearchTask | None = None
    planned_tasks: list[ResearchTask] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)
    summary: TaskSummary | None = None
    note: NoteRecord | None = None
    completed_summaries: list[TaskSummary] = field(default_factory=list)
    failed_tasks: list[Any] = field(default_factory=list)
    sources: list[SearchResult] = field(default_factory=list)
    evidence_judgement: EvidenceJudgement | None = None
    supplemental_queries: list[str] = field(default_factory=list)
    report: FinalReport | None = None


ToolHandler = Callable[[BaseModel, ToolExecutionContext], Awaitable[dict[str, Any]]]
StopCondition = Callable[[ToolCallRequest, ToolExecutionContext], bool]
OutputParser = Callable[[ToolCallRequest, ToolExecutionContext], Any]
ToolResultCallback = Callable[[ToolCallRequest, ToolCallResult, ToolExecutionContext], None]
T = TypeVar("T")


@dataclass(frozen=True)
class _RegisteredTool:
    definition: ToolDefinition
    args_model: type[BaseModel]
    handler: ToolHandler
    stage: str


class JSONFallbackActionParser:
    def parse(
        self,
        raw_output: str,
        allowed_actions: set[str],
    ) -> ToolCallRequest:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(
                "invalid_json",
                "JSON fallback output must be a single strict JSON object.",
            ) from exc

        if not isinstance(parsed, dict):
            raise ToolExecutionError("invalid_json_shape", "Tool action must be a JSON object.")

        missing = [
            field_name
            for field_name in ("action", "arguments", "reason")
            if field_name not in parsed
        ]
        if missing:
            raise ToolExecutionError(
                "missing_action_field",
                f"Tool action is missing required field(s): {', '.join(missing)}.",
            )

        if not isinstance(parsed["action"], str) or not parsed["action"].strip():
            raise ToolExecutionError("invalid_action", "Tool action must be a non-empty string.")
        if not isinstance(parsed["arguments"], dict):
            raise ToolExecutionError("invalid_arguments", "Tool arguments must be an object.")
        if not isinstance(parsed["reason"], str) or not parsed["reason"].strip():
            raise ToolExecutionError("invalid_reason", "Tool reason must be a non-empty string.")

        action = parsed["action"].strip()
        if action != "final" and action not in allowed_actions:
            raise ToolExecutionError("unregistered_tool", f"Tool action is not registered: {action}.")

        return ToolCallRequest(
            action=action,
            arguments=parsed["arguments"],
            reason=parsed["reason"],
        )


class ToolRegistry:
    def __init__(
        self,
        search_tool: SearchTool,
        summarizer: TaskSummarizer,
        note_tool: NoteTool,
        report_writer: ReportWriter,
        progress: ResearchProgressTracker,
        skill_registry: SkillRegistry | None = None,
        evidence_judge: EvidenceJudge | None = None,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        self.progress = progress
        self._tools: dict[str, _RegisteredTool] = {}
        self._register(
            name="search_web",
            description="Search the web for sources relevant to the current research task.",
            args_model=SearchWebArgs,
            handler=self._search_web,
            stage="search",
        )
        self._register(
            name="summarize_task",
            description="Create a cited summary from the current task's accumulated search results.",
            args_model=SummarizeTaskArgs,
            handler=self._summarize_task,
            stage="summary",
        )
        self._register(
            name="save_note",
            description="Persist the current task summary as a JSONL note with optional tags.",
            args_model=SaveNoteArgs,
            handler=self._save_note,
            stage="note",
        )
        self._register(
            name="write_report",
            description="Write the final Markdown research report from all completed task summaries.",
            args_model=WriteReportArgs,
            handler=self._write_report,
            stage="report",
        )
        self._register(
            name="load_skill",
            description="Load an external cognitive skill manual from skills/<skill_name>/SKILL.md.",
            args_model=LoadSkillArgs,
            handler=self._load_skill,
            stage="skill",
        )
        self._register(
            name="judge_evidence",
            description="Judge whether completed summaries and sources provide sufficient evidence before report writing.",
            args_model=JudgeEvidenceArgs,
            handler=self._judge_evidence,
            stage="evidence",
        )
        self._register(
            name="rewrite_queries",
            description="Rewrite evidence gaps into at most two supplemental web search queries.",
            args_model=RewriteQueriesArgs,
            handler=self._rewrite_queries,
            stage="query_rewrite",
        )
        self.search_tool = search_tool
        self.summarizer = summarizer
        self.note_tool = note_tool
        self.report_writer = report_writer
        self.skill_registry = skill_registry or SkillRegistry()
        self.evidence_judge = evidence_judge
        self.query_rewriter = query_rewriter

    @property
    def actions(self) -> set[str]:
        return set(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [registered.definition for registered in self._tools.values()]

    def definitions_for(self, allowed_actions: set[str]) -> list[ToolDefinition]:
        return [
            registered.definition
            for action, registered in self._tools.items()
            if action in allowed_actions
        ]

    def stage_for(self, action: str) -> str:
        registered = self._tools.get(action)
        return registered.stage if registered else "tool"

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext,
    ) -> ToolCallResult:
        registered = self._tools.get(request.action)
        if registered is None:
            result = ToolCallResult(
                action=request.action,
                success=False,
                error_code="unregistered_tool",
                error_message=f"Tool action is not registered: {request.action}.",
                call_id=request.call_id,
            )
            self._log_result(request, registered=None, result=result)
            return result

        try:
            arguments = registered.args_model.model_validate(request.arguments)
        except ValidationError as exc:
            result = ToolCallResult(
                action=request.action,
                success=False,
                error_code="invalid_tool_arguments",
                error_message=self.progress.sanitize(str(exc)),
                call_id=request.call_id,
            )
            self._log_result(request, registered=registered, result=result)
            return result

        try:
            payload = await registered.handler(arguments, context)
        except ToolExecutionError as exc:
            result = ToolCallResult(
                action=request.action,
                success=False,
                error_code=exc.code,
                error_message=self.progress.sanitize(exc.message),
                call_id=request.call_id,
            )
        except ToolError as exc:
            result = ToolCallResult(
                action=request.action,
                success=False,
                error_code=exc.code,
                error_message=self.progress.sanitize(exc.message),
                call_id=request.call_id,
            )
        except Exception as exc:
            result = ToolCallResult(
                action=request.action,
                success=False,
                error_code="tool_execution_failed",
                error_message=self.progress.sanitize(str(exc)),
                call_id=request.call_id,
            )
        else:
            result = ToolCallResult(
                action=request.action,
                success=True,
                result=payload,
                call_id=request.call_id,
            )

        self._log_result(request, registered=registered, result=result)
        return result

    def _register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        handler: ToolHandler,
        stage: str,
    ) -> None:
        self._tools[name] = _RegisteredTool(
            definition=ToolDefinition(
                name=name,
                description=description,
                parameters=args_model.model_json_schema(),
            ),
            args_model=args_model,
            handler=handler,
            stage=stage,
        )

    async def _search_web(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = arguments if isinstance(arguments, SearchWebArgs) else SearchWebArgs.model_validate(arguments)
        task = self._require_task(context)
        search_task = task.model_copy(update={"query": args.query})
        results = await self.search_tool.search(search_task)
        context.search_results = self._merge_results(context.search_results, results)
        return {"results": [result.model_dump(mode="json") for result in context.search_results]}

    async def _summarize_task(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        self._require_task(context)
        if not context.search_results:
            raise ToolExecutionError(
                "missing_search_results",
                "summarize_task requires at least one search result.",
            )
        summary = await self.summarizer.summarize(context.task, context.search_results)
        context.summary = summary
        return {"summary": summary.model_dump(mode="json")}

    async def _save_note(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = arguments if isinstance(arguments, SaveNoteArgs) else SaveNoteArgs.model_validate(arguments)
        if context.summary is None:
            raise ToolExecutionError("missing_summary", "save_note requires a task summary.")
        tags = args.tags or ["research", context.topic]
        note = self.note_tool.save(context.summary, tags=tags)
        context.note = note
        return {"note": note.model_dump(mode="json")}

    async def _write_report(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = arguments if isinstance(arguments, WriteReportArgs) else WriteReportArgs.model_validate(arguments)
        topic = args.topic or context.topic
        report = await self.report_writer.write(
            topic,
            context.completed_summaries,
            evidence_judgement=context.evidence_judgement,
        )
        context.report = report
        return {"report": report.model_dump(mode="json")}

    async def _load_skill(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = arguments if isinstance(arguments, LoadSkillArgs) else LoadSkillArgs.model_validate(arguments)
        manual = self.skill_registry.load(args.skill_name)
        return {"skill": manual.model_dump(mode="json")}

    async def _judge_evidence(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if self.evidence_judge is None:
            raise ToolExecutionError("evidence_judge_unavailable", "Evidence judge is not configured.")
        judgement = await self.evidence_judge.judge(
            topic=context.topic,
            completed_summaries=context.completed_summaries,
            sources=context.sources,
            failed_tasks=context.failed_tasks,
        )
        context.evidence_judgement = judgement
        return {"evidence_judgement": judgement.model_dump(mode="json")}

    async def _rewrite_queries(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = arguments if isinstance(arguments, RewriteQueriesArgs) else RewriteQueriesArgs.model_validate(arguments)
        if self.query_rewriter is None:
            raise ToolExecutionError("query_rewriter_unavailable", "Query rewriter is not configured.")
        gaps = context.evidence_judgement.gaps if context.evidence_judgement is not None else []
        queries = await self.query_rewriter.rewrite(
            topic=context.topic,
            evidence_gaps=gaps,
            existing_task_queries=[task.query for task in context.planned_tasks],
            existing_source_summaries=[
                f"{source.title}: {source.snippet}" for source in context.sources
            ],
            max_queries=args.max_queries,
        )
        context.supplemental_queries = queries[: args.max_queries]
        return {"queries": context.supplemental_queries}

    def _require_task(self, context: ToolExecutionContext) -> ResearchTask:
        if context.task is None:
            raise ToolExecutionError("missing_task", "This tool requires a current task.")
        return context.task

    def _merge_results(
        self,
        existing: Sequence[SearchResult],
        new_results: Sequence[SearchResult],
    ) -> list[SearchResult]:
        results_by_url = {str(result.url): result for result in existing}
        for result in new_results:
            results_by_url.setdefault(str(result.url), result)
        return list(results_by_url.values())

    def _log_result(
        self,
        request: ToolCallRequest,
        registered: _RegisteredTool | None,
        result: ToolCallResult,
    ) -> None:
        stage = registered.stage if registered else "tool"
        output_summary = self._output_summary(result)
        self.progress.log_tool_call(
            stage=stage,
            tool_name=request.action,
            input_summary=self._input_summary(request),
            output_summary=output_summary,
            status=ToolCallStatus.SUCCESS if result.success else ToolCallStatus.FAILED,
            error=result.error_message,
        )

    def _input_summary(self, request: ToolCallRequest) -> str:
        if request.action == "search_web":
            query = request.arguments.get("query", "")
            return f"query={query}"
        if request.action == "load_skill":
            return f"skill={request.arguments.get('skill_name', '')}"
        if request.action == "judge_evidence":
            return "judge=evidence"
        if request.action == "rewrite_queries":
            return f"max_queries={request.arguments.get('max_queries', 2)}"
        return f"action={request.action}"

    def _output_summary(self, result: ToolCallResult) -> str:
        if not result.success:
            return result.error_code or "failed"
        payload = result.result or {}
        if result.action == "search_web":
            return f"{len(payload.get('results', []))} results"
        if result.action == "summarize_task":
            summary = payload.get("summary")
            content = summary.get("content", "") if isinstance(summary, dict) else ""
            return f"{len(content)} chars"
        if result.action == "save_note":
            note = payload.get("note")
            title = note.get("task_title", "") if isinstance(note, dict) else ""
            return f"saved note for {title}"
        if result.action == "write_report":
            report = payload.get("report")
            markdown = report.get("markdown", "") if isinstance(report, dict) else ""
            return f"{len(markdown)} chars"
        if result.action == "load_skill":
            skill = payload.get("skill")
            metadata = skill.get("metadata", {}) if isinstance(skill, dict) else {}
            return f"{metadata.get('content_length', 0)} chars"
        if result.action == "judge_evidence":
            judgement = payload.get("evidence_judgement")
            if isinstance(judgement, dict):
                return f"sufficient={judgement.get('is_sufficient')}, confidence={judgement.get('confidence')}"
            return "judged"
        if result.action == "rewrite_queries":
            return f"{len(payload.get('queries', []))} queries"
        return "ok"


class ToolCallingAgentRunner(Generic[T]):
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        progress: ResearchProgressTracker,
        max_global_tool_calls: int | None = None,
        fallback_parse_retries: int = 2,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.progress = progress
        self.max_global_tool_calls = max_global_tool_calls
        self.global_tool_calls = 0
        self.fallback_parse_retries = fallback_parse_retries
        self.parser = JSONFallbackActionParser()

    async def run(
        self,
        *,
        agent_name: str,
        goal: str,
        system_prompt: str,
        context: ToolExecutionContext,
        stop_condition: StopCondition,
        output_parser: OutputParser,
        max_tool_calls: int,
        initial_messages: Sequence[LLMMessage] | None = None,
        on_tool_result: ToolResultCallback | None = None,
        status_data: dict[str, Any] | None = None,
        allowed_actions: set[str] | None = None,
    ) -> T:
        messages = list(initial_messages or self._initial_messages(system_prompt, goal))
        status_payload = {"agent_name": agent_name, **(status_data or {})}
        available_actions = self.registry.actions if allowed_actions is None else allowed_actions
        use_fallback = not getattr(self.llm, "supports_native_tools", False)
        if use_fallback:
            self.progress.status("已降级为 JSON 工具调用", **status_payload)

        parse_failures = 0
        calls_used = 0

        while calls_used < max_tool_calls:
            self.progress.status("模型正在选择工具", **status_payload)
            request: ToolCallRequest
            if use_fallback:
                raw_output = await self.llm.complete(messages, temperature=0.1)
                try:
                    request = self.parser.parse(raw_output, available_actions)
                    parse_failures = 0
                except ToolExecutionError as exc:
                    parse_failures += 1
                    if parse_failures > self.fallback_parse_retries:
                        raise ToolExecutionError(exc.code, exc.message) from exc
                    messages.append(LLMMessage(role="assistant", content=raw_output or "EMPTY_OUTPUT"))
                    messages.append(LLMMessage(role="user", content=self._recoverable_error_feedback(exc)))
                    continue
            else:
                try:
                    turn = await self.llm.complete_with_tools(
                        messages,
                        tools=(
                            self.registry.definitions()
                            if allowed_actions is None
                            else self.registry.definitions_for(allowed_actions)
                        ),
                        tool_choice="auto",
                        temperature=0.1,
                    )
                except LLMToolCallUnsupported:
                    use_fallback = True
                    self.progress.status("已降级为 JSON 工具调用", **status_payload)
                    messages = list(initial_messages or self._initial_messages(system_prompt, goal))
                    continue
                if not turn.tool_calls:
                    content = turn.content or ""
                    request = self.parser.parse(
                        json.dumps(
                            {
                                "action": "final",
                                "arguments": {"summary": content},
                                "reason": "native assistant final response",
                            },
                            ensure_ascii=False,
                        ),
                        available_actions,
                    )
                else:
                    request = turn.tool_calls[0]

            if stop_condition(request, context):
                return output_parser(request, context)
            if allowed_actions is not None and request.action not in allowed_actions:
                raise ToolExecutionError(
                    "tool_not_allowed",
                    f"Tool action is not allowed for this agent: {request.action}.",
                )

            self.reserve_global_tool_call()
            calls_used += 1
            self.progress.status(
                "正在执行工具",
                tool_name=request.action,
                **status_payload,
            )
            result = await self.registry.execute(request, context)
            if on_tool_result is not None:
                on_tool_result(request, result, context)

            self._append_tool_feedback(messages, request, result, context, use_fallback)
            if not result.success:
                raise ToolExecutionError(
                    result.error_code or "tool_execution_failed",
                    result.error_message or "Tool execution failed.",
                )

        raise ToolExecutionError(
            "tool_call_limit_exceeded",
            f"Agent {agent_name} exceeded maximum tool calls: {max_tool_calls}.",
        )

    def reserve_global_tool_call(self) -> None:
        if self.max_global_tool_calls is None:
            return
        if self.global_tool_calls >= self.max_global_tool_calls:
            raise ToolExecutionError(
                "global_tool_call_limit_exceeded",
                f"Research exceeded maximum tool calls: {self.max_global_tool_calls}.",
            )
        self.global_tool_calls += 1

    def _initial_messages(self, system_prompt: str, goal: str) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=goal),
        ]

    def _recoverable_error_feedback(self, exc: ToolExecutionError) -> str:
        return (
            "Your previous tool action was rejected as a recoverable error. "
            f"Error code: {exc.code}. Error: {exc.message}. "
            "Return only a corrected strict JSON action."
        )

    def _tool_result_feedback(
        self,
        result: ToolCallResult,
        context: ToolExecutionContext,
    ) -> str:
        return (
            "Tool result JSON:\n"
            f"{json.dumps(result.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"Current search result count: {len(context.search_results)}. "
            f"Has summary: {context.summary is not None}."
        )

    def _append_tool_feedback(
        self,
        messages: list[LLMMessage],
        request: ToolCallRequest,
        result: ToolCallResult,
        context: ToolExecutionContext,
        use_fallback: bool,
    ) -> None:
        if use_fallback:
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
                )
            )
            messages.append(
                LLMMessage(
                    role="user",
                    content=self._tool_result_feedback(result, context),
                )
            )
            return

        call_id = request.call_id or f"call_{request.action}"
        messages.append(
            LLMMessage(
                role="assistant",
                content=request.reason,
                tool_calls=[
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": request.action,
                            "arguments": json.dumps(
                                request.arguments,
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            )
        )
        messages.append(
            LLMMessage(
                role="tool",
                content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                tool_call_id=call_id,
            )
        )


class ToolCallingResearchExecutor:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        progress: ResearchProgressTracker,
        max_task_tool_calls: int = 6,
        max_global_tool_calls: int | None = None,
        fallback_parse_retries: int = 2,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.progress = progress
        self.max_task_tool_calls = max_task_tool_calls
        self.runner: ToolCallingAgentRunner[tuple[list[SearchResult], TaskSummary, NoteRecord]] = (
            ToolCallingAgentRunner(
                llm=llm,
                registry=registry,
                progress=progress,
                max_global_tool_calls=max_global_tool_calls,
                fallback_parse_retries=fallback_parse_retries,
            )
        )

    async def run_task(
        self,
        topic: str,
        task: ResearchTask,
    ) -> tuple[list[SearchResult], TaskSummary, NoteRecord]:
        context = ToolExecutionContext(topic=topic, task=task)
        return await self.runner.run(
            agent_name="ToolCallingResearchExecutor",
            goal=self._task_goal(topic, task),
            system_prompt=self._task_system_prompt(),
            context=context,
            stop_condition=self._task_stop_condition,
            output_parser=self._parse_task_output,
            max_tool_calls=self.max_task_tool_calls,
            initial_messages=self._initial_task_messages(topic, task),
            on_tool_result=lambda request, result, _context: self._emit_compatible_event(
                request.action,
                result,
                task,
            ),
            status_data={"task_title": task.title},
        )

    async def write_report(
        self,
        topic: str,
        completed_summaries: list[TaskSummary],
        evidence_judgement: EvidenceJudgement | None = None,
    ) -> FinalReport:
        context = ToolExecutionContext(
            topic=topic,
            completed_summaries=completed_summaries,
            evidence_judgement=evidence_judgement,
        )
        self.runner.reserve_global_tool_call()
        result = await self.registry.execute(
            ToolCallRequest(
                action="write_report",
                arguments={"topic": topic},
                reason="Generate final report after task execution.",
            ),
            context,
        )
        if not result.success or context.report is None:
            raise ToolExecutionError(
                result.error_code or "report_failed",
                result.error_message or "Report generation failed.",
            )
        return context.report

    def _task_system_prompt(self) -> str:
        return (
            "You are a controlled research tool-calling agent. "
            "When native tools are unavailable, return only strict JSON, "
            "with no Markdown and no explanatory text. The JSON shape is "
            '{"action":"...","arguments":{...},"reason":"..."}. '
            "Use only these actions: search_web, summarize_task, save_note, final. "
            "A task may finish only after summarize_task and save_note succeed."
        )

    def _task_goal(self, topic: str, task: ResearchTask) -> str:
        return (
            f"Topic: {topic}\n"
            f"Task title: {task.title}\n"
            f"Intent: {task.intent}\n"
            f"Default search query: {task.query}\n"
            "Choose the next tool action."
        )

    def _initial_task_messages(self, topic: str, task: ResearchTask) -> list[LLMMessage]:
        return [
            LLMMessage(
                role="system",
                content=self._task_system_prompt(),
            ),
            LLMMessage(
                role="user",
                content=self._task_goal(topic, task),
            ),
        ]

    def _task_stop_condition(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext,
    ) -> bool:
        return request.action == "final"

    def _parse_task_output(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext,
    ) -> tuple[list[SearchResult], TaskSummary, NoteRecord]:
        if context.summary is None:
            raise ToolExecutionError(
                "missing_summary",
                "Task cannot finish before summarize_task produces a summary.",
            )
        if context.note is None:
            raise ToolExecutionError(
                "missing_note",
                "Task cannot finish before save_note persists the summary.",
            )
        return context.search_results, context.summary, context.note

    def _emit_compatible_event(
        self,
        action: str,
        result: ToolCallResult,
        task: ResearchTask,
    ) -> None:
        from app.models import SSEEventType

        if not result.success or not result.result:
            return
        if action == "search_web":
            self.progress.emit(
                SSEEventType.SEARCH_RESULTS,
                {"task_title": task.title, "results": result.result["results"]},
            )
        elif action == "summarize_task":
            self.progress.emit(SSEEventType.SUMMARY, {"summary": result.result["summary"]})
