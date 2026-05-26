from typing import Protocol

from app.agents import ReportWriter, TaskSummarizer, TodoPlanner
from app.models import (
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
        progress: ResearchProgressTracker | None = None,
    ) -> None:
        self.planner = planner
        self.search_tool = search_tool
        self.summarizer = summarizer
        self.note_tool = note_tool
        self.report_writer = report_writer
        self.progress = progress or ResearchProgressTracker()

    async def run(self, topic: str, max_tasks: int = 5) -> ResearchResult:
        self.progress.reset()
        self.progress.status("正在规划", topic=topic)
        planned_tasks = await self.planner.plan(topic=topic, max_tasks=max_tasks)
        self.progress.emit(
            SSEEventType.TASK,
            {"tasks": [task.model_dump(mode="json") for task in planned_tasks]},
        )
        execution_records: list[TaskExecutionRecord] = []
        completed_summaries: list[TaskSummary] = []

        for planned_task in planned_tasks:
            running_task = planned_task.model_copy(update={"status": TaskStatus.RUNNING})
            self.progress.emit(
                SSEEventType.TASK,
                {"task": running_task.model_dump(mode="json")},
            )
            current_stage = "search"
            current_tool_name = type(self.search_tool).__name__
            try:
                self.progress.status("正在搜索", task_title=running_task.title)
                search_results = await self.search_tool.search(running_task)
                self.progress.log_tool_call(
                    stage="search",
                    tool_name=type(self.search_tool).__name__,
                    input_summary=f"query={running_task.query}",
                    output_summary=f"{len(search_results)} results",
                    status=ToolCallStatus.SUCCESS,
                )
                self.progress.emit(
                    SSEEventType.SEARCH_RESULTS,
                    {
                        "task_title": running_task.title,
                        "results": [
                            result.model_dump(mode="json") for result in search_results
                        ],
                    },
                )

                current_stage = "summary"
                current_tool_name = type(self.summarizer).__name__
                self.progress.status("正在总结", task_title=running_task.title)
                summary = await self.summarizer.summarize(running_task, search_results)
                self.progress.log_tool_call(
                    stage="summary",
                    tool_name=type(self.summarizer).__name__,
                    input_summary=f"task={running_task.title}, sources={len(search_results)}",
                    output_summary=f"{len(summary.content)} chars",
                    status=ToolCallStatus.SUCCESS,
                )
                self.progress.emit(
                    SSEEventType.SUMMARY,
                    {"summary": summary.model_dump(mode="json")},
                )

                current_stage = "note"
                current_tool_name = type(self.note_tool).__name__
                note = self.note_tool.save(summary, tags=["research", topic])
                self.progress.log_tool_call(
                    stage="note",
                    tool_name=type(self.note_tool).__name__,
                    input_summary=f"task={summary.task_title}",
                    output_summary=f"saved note for {note.task_title}",
                    status=ToolCallStatus.SUCCESS,
                )
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
                self.progress.status("任务完成", task_title=completed_task.title)
                self.progress.emit(
                    SSEEventType.TASK,
                    {"task": completed_task.model_dump(mode="json")},
                )
            except Exception as exc:
                error = self.progress.sanitize(str(exc))
                self.progress.log_tool_call(
                    stage=current_stage,
                    tool_name=current_tool_name,
                    input_summary=f"task={running_task.title}",
                    output_summary="task failed",
                    status=ToolCallStatus.FAILED,
                    error=error,
                )
                self.progress.error(error, task_title=running_task.title)
                failed_task = running_task.model_copy(update={"status": TaskStatus.FAILED})
                execution_records.append(
                    TaskExecutionRecord(
                        task=failed_task,
                        status=TaskStatus.FAILED,
                        error=error,
                    )
                )

        report = await self._write_report(topic, completed_summaries)
        self.progress.emit(SSEEventType.REPORT, {"report": report.model_dump(mode="json")})
        self.progress.status("报告生成完成", topic=topic)
        self.progress.emit(SSEEventType.DONE, {"topic": topic})
        return ResearchResult(
            topic=topic,
            tasks=execution_records,
            report=report,
            tool_logs=list(self.progress.tool_logs),
        )

    async def _write_report(
        self,
        topic: str,
        completed_summaries: list[TaskSummary],
    ) -> FinalReport:
        if not completed_summaries:
            report = FinalReport(
                title=topic,
                markdown=(
                    f"# {topic}\n\n"
                    "## Overview\n"
                    "No subtasks completed successfully.\n\n"
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
            report = await self.report_writer.write(topic, completed_summaries)
        except Exception as exc:
            error = self.progress.sanitize(str(exc))
            self.progress.log_tool_call(
                stage="report",
                tool_name=type(self.report_writer).__name__,
                input_summary=f"summaries={len(completed_summaries)}",
                output_summary="report failed",
                status=ToolCallStatus.FAILED,
                error=error,
            )
            self.progress.status("报告生成器输出不合格，已使用结构化兜底报告")
            fallback_report = self._build_fallback_report(topic, completed_summaries)
            self.progress.log_tool_call(
                stage="report",
                tool_name="StructuredFallbackReportWriter",
                input_summary=f"summaries={len(completed_summaries)}",
                output_summary=f"{len(fallback_report.markdown)} chars",
                status=ToolCallStatus.SUCCESS,
            )
            return fallback_report

        self.progress.log_tool_call(
            stage="report",
            tool_name=type(self.report_writer).__name__,
            input_summary=f"summaries={len(completed_summaries)}",
            output_summary=f"{len(report.markdown)} chars",
            status=ToolCallStatus.SUCCESS,
        )
        return report

    def _build_fallback_report(
        self,
        topic: str,
        completed_summaries: list[TaskSummary],
    ) -> FinalReport:
        sources = self._collect_sources(completed_summaries)
        source_numbers = {str(source.url): index for index, source in enumerate(sources, start=1)}
        insufficient_note = ""
        if any(len(summary.sources) < 3 for summary in completed_summaries):
            insufficient_note = "\n\n资料不足：至少一个已完成任务的来源少于 3 条，相关结论需要继续验证。"

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

    def _collect_sources(self, summaries: list[TaskSummary]) -> list[SearchResult]:
        sources_by_url: dict[str, SearchResult] = {}
        for summary in summaries:
            for source in summary.sources:
                sources_by_url[str(source.url)] = source
        return list(sources_by_url.values())
