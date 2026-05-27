from typing import Protocol

from langgraph.graph import END, START, StateGraph

from app.agents import EvidenceJudge, QueryRewriter, ReportWriter, TaskSummarizer, TodoPlanner
from app.models import (
    EvidenceConfidence,
    EvidenceJudgement,
    FinalReport,
    ResearchResult,
    ResearchTask,
    SearchResult,
    SSEEventType,
    TaskExecutionRecord,
    TaskStatus,
    TaskSummary,
    ToolCallStatus,
)
from app.progress import ResearchProgressTracker
from app.research_state import (
    ResearchWorkflowState,
    build_initial_research_state,
    collect_sources_from_summaries,
    merge_sources_by_url,
)
from app.tool_calling import ToolCallingResearchExecutor, ToolRegistry
from app.tools import NoteTool


class SearchTool(Protocol):
    async def search(self, task: ResearchTask) -> list[SearchResult]:
        pass


class ResearchOrchestrator:
    def __init__(
        self,
        planner: TodoPlanner,
        search_tool: SearchTool,
        summarizer: TaskSummarizer,
        note_tool: NoteTool,
        report_writer: ReportWriter,
        evidence_judge: EvidenceJudge | None = None,
        query_rewriter: QueryRewriter | None = None,
        progress: ResearchProgressTracker | None = None,
        max_task_tool_calls: int = 6,
    ) -> None:
        self.planner = planner
        self.search_tool = search_tool
        self.summarizer = summarizer
        self.note_tool = note_tool
        self.report_writer = report_writer
        self.evidence_judge = evidence_judge
        self.query_rewriter = query_rewriter
        self.progress = progress or ResearchProgressTracker()
        self.max_task_tool_calls = max_task_tool_calls

    async def run(self, topic: str, max_tasks: int = 5) -> ResearchResult:
        self.progress.reset()
        registry = ToolRegistry(
            search_tool=self.search_tool,
            summarizer=self.summarizer,
            note_tool=self.note_tool,
            report_writer=self.report_writer,
            progress=self.progress,
            evidence_judge=self.evidence_judge,
            query_rewriter=self.query_rewriter,
        )
        if hasattr(self.summarizer, "configure_tool_runtime"):
            self.summarizer.configure_tool_runtime(registry, self.progress)
        executor = ToolCallingResearchExecutor(
            llm=self.summarizer.llm,
            registry=registry,
            progress=self.progress,
            max_task_tool_calls=self.max_task_tool_calls,
            max_global_tool_calls=max_tasks * 8 + 4,
        )
        graph = self._build_graph(executor)
        state = await graph.ainvoke(build_initial_research_state(topic, max_tasks))
        report = state["report"]
        return ResearchResult(
            topic=topic,
            tasks=state["execution_records"],
            report=report,
            tool_logs=list(self.progress.tool_logs),
        )

    def _build_graph(self, executor: ToolCallingResearchExecutor):
        graph = StateGraph(ResearchWorkflowState)

        async def plan_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            topic = state["topic"]
            max_tasks = state["max_tasks"]
            self.progress.status("正在规划", topic=topic)
            planned_tasks = await self.planner.plan(topic=topic, max_tasks=max_tasks)
            self.progress.emit(
                SSEEventType.TASK,
                {"tasks": [task.model_dump(mode="json") for task in planned_tasks]},
            )
            return {
                "planned_tasks": planned_tasks,
                "current_index": 0,
            }

        async def execute_task_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            topic = state["topic"]
            planned_tasks = state["planned_tasks"]
            current_index = state["current_index"]
            planned_task = planned_tasks[current_index]
            running_task = planned_task.model_copy(update={"status": TaskStatus.RUNNING})
            self.progress.emit(
                SSEEventType.TASK,
                {"task": running_task.model_dump(mode="json")},
            )

            execution_records = list(state["execution_records"])
            completed_summaries = list(state["completed_summaries"])
            failed_tasks = list(state["failed_tasks"])
            sources = list(state["sources"])

            try:
                search_results, summary, note = await executor.run_task(topic, running_task)
                completed_task = running_task.model_copy(update={"status": TaskStatus.COMPLETED})
                execution_records.append(
                    TaskExecutionRecord(
                        task=completed_task,
                        search_results=search_results,
                        summary=summary,
                        note=note,
                        status=TaskStatus.COMPLETED,
                    )
                )
                completed_summaries.append(summary)
                sources = merge_sources_by_url(sources, search_results)
                self.progress.status("任务完成", task_title=completed_task.title)
                self.progress.emit(
                    SSEEventType.TASK,
                    {"task": completed_task.model_dump(mode="json")},
                )
            except Exception as exc:
                error = self.progress.sanitize(str(exc))
                self.progress.error(error, task_title=running_task.title)
                failed_task = running_task.model_copy(update={"status": TaskStatus.FAILED})
                execution_records.append(
                    TaskExecutionRecord(
                        task=failed_task,
                        status=TaskStatus.FAILED,
                        error=error,
                    )
                )
                failed_tasks.append(execution_records[-1])

            return {
                "current_index": current_index + 1,
                "execution_records": execution_records,
                "completed_summaries": completed_summaries,
                "failed_tasks": failed_tasks,
                "sources": sources,
            }

        async def write_report_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            topic = state["topic"]
            completed_summaries = state["completed_summaries"]
            report = await self._write_report(
                topic,
                completed_summaries,
                state.get("evidence_judgement"),
                executor,
            )
            self.progress.emit(SSEEventType.REPORT, {"report": report.model_dump(mode="json")})
            self.progress.status("报告生成完成", topic=topic)
            self.progress.emit(SSEEventType.DONE, {"topic": topic})
            return {"report": report}

        def route_after_plan(state: ResearchWorkflowState) -> str:
            return "execute_task" if state["planned_tasks"] else "write_report"

        def route_after_task(state: ResearchWorkflowState) -> str:
            return (
                "execute_task"
                if state["current_index"] < len(state["planned_tasks"])
                else ("judge_evidence" if self.evidence_judge is not None else "write_report")
            )

        async def judge_evidence_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            context = self._evidence_context(state)
            try:
                result = await executor.registry.execute(
                    self._judge_evidence_request(),
                    context,
                )
                if not result.success or context.evidence_judgement is None:
                    raise RuntimeError(result.error_message or "Evidence judgement failed.")
                judgement = context.evidence_judgement
            except Exception as exc:
                error = self.progress.sanitize(str(exc))
                judgement = EvidenceJudgement(
                    is_sufficient=False,
                    confidence=EvidenceConfidence.LOW,
                    gaps=["Evidence judgement failed."],
                    rationale=f"Evidence judgement failed; report should treat evidence as insufficient. {error}",
                )
            return {"evidence_judgement": judgement}

        def route_after_judge_evidence(state: ResearchWorkflowState) -> str:
            judgement = state.get("evidence_judgement")
            if judgement is None or judgement.is_sufficient:
                return "write_report"
            if self.query_rewriter is None:
                return "write_report"
            if state.get("supplemental_rounds_used", 0) >= 1:
                return "write_report"
            return "rewrite_queries"

        async def rewrite_queries_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            context = self._query_rewrite_context(state)
            try:
                result = await executor.registry.execute(
                    self._rewrite_queries_request(max_queries=2),
                    context,
                )
                if not result.success:
                    return {"supplemental_queries": []}
                return {"supplemental_queries": context.supplemental_queries[:2]}
            except Exception as exc:
                self.progress.error(self.progress.sanitize(str(exc)))
                return {"supplemental_queries": []}

        async def supplemental_search_node(state: ResearchWorkflowState) -> ResearchWorkflowState:
            queries = list(state.get("supplemental_queries", []))[:2]
            sources = list(state["sources"])
            rounds_used = state.get("supplemental_rounds_used", 0) + 1
            self.progress.status(
                "正在补充搜索",
                query_count=len(queries),
                supplemental_rounds_used=rounds_used,
            )

            for index, query in enumerate(queries, start=1):
                context = self._supplemental_search_context(state["topic"], query, index)
                result = await executor.registry.execute(
                    self._search_web_request(query),
                    context,
                )
                if not result.success:
                    continue
                sources = merge_sources_by_url(sources, context.search_results)
                if result.result:
                    self.progress.emit(
                        SSEEventType.SEARCH_RESULTS,
                        {
                            "task_title": context.task.title if context.task else "Supplemental search",
                            "results": result.result["results"],
                        },
                    )

            return {
                "sources": sources,
                "supplemental_rounds_used": rounds_used,
            }

        graph.add_node("plan", plan_node)
        graph.add_node("execute_task", execute_task_node)
        graph.add_node("judge_evidence", judge_evidence_node)
        graph.add_node("rewrite_queries", rewrite_queries_node)
        graph.add_node("supplemental_search", supplemental_search_node)
        graph.add_node("write_report", write_report_node)
        graph.add_edge(START, "plan")
        graph.add_conditional_edges(
            "plan",
            route_after_plan,
            {"execute_task": "execute_task", "write_report": "write_report"},
        )
        graph.add_conditional_edges(
            "execute_task",
            route_after_task,
            {
                "execute_task": "execute_task",
                "judge_evidence": "judge_evidence",
                "write_report": "write_report",
            },
        )
        graph.add_conditional_edges(
            "judge_evidence",
            route_after_judge_evidence,
            {
                "rewrite_queries": "rewrite_queries",
                "write_report": "write_report",
            },
        )
        graph.add_edge("rewrite_queries", "supplemental_search")
        graph.add_edge("supplemental_search", "judge_evidence")
        graph.add_edge("write_report", END)
        return graph.compile()

    def _evidence_context(self, state: ResearchWorkflowState):
        from app.tool_calling import ToolExecutionContext

        return ToolExecutionContext(
            topic=state["topic"],
            completed_summaries=list(state["completed_summaries"]),
            failed_tasks=list(state["failed_tasks"]),
            sources=list(state["sources"]),
        )

    def _query_rewrite_context(self, state: ResearchWorkflowState):
        from app.tool_calling import ToolExecutionContext

        return ToolExecutionContext(
            topic=state["topic"],
            planned_tasks=list(state["planned_tasks"]),
            sources=list(state["sources"]),
            evidence_judgement=state.get("evidence_judgement"),
        )

    def _supplemental_search_context(
        self,
        topic: str,
        query: str,
        index: int,
    ):
        from app.tool_calling import ToolExecutionContext

        return ToolExecutionContext(
            topic=topic,
            task=ResearchTask(
                title=f"Supplemental search {index}",
                intent="Address evidence gaps before final report generation.",
                query=query,
                status=TaskStatus.RUNNING,
            ),
        )

    def _judge_evidence_request(self):
        from app.tool_calling import ToolCallRequest

        return ToolCallRequest(
            action="judge_evidence",
            arguments={},
            reason="Judge evidence before final report generation.",
        )

    def _rewrite_queries_request(self, max_queries: int):
        from app.tool_calling import ToolCallRequest

        return ToolCallRequest(
            action="rewrite_queries",
            arguments={"max_queries": max_queries},
            reason="Rewrite evidence gaps into supplemental search queries.",
        )

    def _search_web_request(self, query: str):
        from app.tool_calling import ToolCallRequest

        return ToolCallRequest(
            action="search_web",
            arguments={"query": query},
            reason="Run one supplemental search for an evidence gap.",
        )

    async def _write_report(
        self,
        topic: str,
        completed_summaries: list[TaskSummary],
        evidence_judgement: EvidenceJudgement | None,
        executor: ToolCallingResearchExecutor,
    ) -> FinalReport:
        if not completed_summaries:
            evidence_note = self._evidence_status_note(evidence_judgement)
            report = FinalReport(
                title=topic,
                markdown=(
                    f"# {topic}\n\n"
                    "## Overview\n"
                    "No subtasks completed successfully."
                    f"{evidence_note}\n\n"
                    "## Conclusion\n"
                    "Research could not be completed with the available inputs.\n\n"
                    "## References\n"
                    "No sources available."
                ),
                sources=[],
            )
            self.progress.log_tool_call(
                stage="report",
                tool_name=type(self.report_writer).__name__,
                input_summary="summaries=0",
                output_summary="empty report",
                status=ToolCallStatus.SUCCESS,
            )
            return report
        try:
            report = await executor.write_report(
                topic,
                completed_summaries,
                evidence_judgement=evidence_judgement,
            )
        except Exception as exc:
            error = self.progress.sanitize(str(exc))
            self.progress.status("报告生成器输出不合格，已使用结构化兜底报告")
            fallback_report = self._build_fallback_report(
                topic,
                completed_summaries,
                evidence_judgement,
            )
            self.progress.log_tool_call(
                stage="report",
                tool_name="StructuredFallbackReportWriter",
                input_summary=f"summaries={len(completed_summaries)}",
                output_summary=f"{len(fallback_report.markdown)} chars",
                status=ToolCallStatus.SUCCESS,
            )
            return fallback_report

        return report

    def _build_fallback_report(
        self,
        topic: str,
        completed_summaries: list[TaskSummary],
        evidence_judgement: EvidenceJudgement | None = None,
    ) -> FinalReport:
        sources = self._collect_sources(completed_summaries)
        source_numbers = {str(source.url): index for index, source in enumerate(sources, start=1)}
        insufficient_note = self._evidence_status_note(evidence_judgement)
        if any(len(summary.sources) < 3 for summary in completed_summaries):
            insufficient_note += "\n\n资料不足：至少一个已完成任务的来源少于 3 条，相关结论需要继续验证。"

        analysis_sections: list[str] = []
        for summary in completed_summaries:
            markers = "".join(
                f"[{source_numbers[str(source.url)]}]" for source in summary.sources
            )
            analysis_sections.append(
                f"### {summary.task_title}\n"
                f"{summary.content}\n\n"
                f"来源：{markers or '待验证'}"
            )

        reference_lines = [
            f"[{source_numbers[str(source.url)]}] {source.url}" for source in sources
        ]
        if not reference_lines:
            reference_lines = ["无可用来源。"]

        markdown = (
            f"# {topic}\n\n"
            "## 概述\n"
            f"本报告基于 {len(completed_summaries)} 个已完成子任务生成，"
            f"共保留 {len(sources)} 条去重来源。{insufficient_note}\n\n"
            "## 分节分析\n"
            + "\n\n".join(analysis_sections)
            + "\n\n"
            "## 总结\n"
            "以上结论来自已完成任务的搜索结果和总结，失败任务未被纳入最终结论。"
            f"{''.join(f'[{index}]' for index in range(1, len(sources) + 1))}\n\n"
            "## 参考文献\n"
            + "\n".join(reference_lines)
        )
        return FinalReport(title=topic, markdown=markdown, sources=sources)

    def _evidence_status_note(
        self,
        judgement: EvidenceJudgement | None,
    ) -> str:
        if judgement is None or judgement.is_sufficient:
            return ""
        gaps = "；".join(judgement.gaps) if judgement.gaps else "未给出具体缺口"
        return (
            "\n\n证据不足：证据判断结果显示当前资料仍存在缺口，"
            f"相关结论待验证，仅供参考。缺口：{gaps}。"
        )

    def _collect_sources(self, summaries: list[TaskSummary]) -> list[SearchResult]:
        return collect_sources_from_summaries(summaries)
