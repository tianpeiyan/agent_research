import json
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, ValidationError

from app.citations import CitationQualityError, CitationQualityValidator
from app.llm import LLMMessage, LLMProvider
from app.models import (
    EvidenceConfidence,
    EvidenceJudgement,
    FinalReport,
    ResearchTask,
    SearchResult,
    TaskExecutionRecord,
    TaskStatus,
    TaskSummary,
)


class AgentOutputError(RuntimeError):
    pass


class _PlannerTaskOutput(BaseModel):
    title: str
    intent: str
    query: str

    model_config = ConfigDict(extra="forbid")


class TodoPlanner:
    def __init__(self, llm: LLMProvider, retries: int = 1) -> None:
        self.llm = llm
        self.retries = retries

    async def plan(self, topic: str, max_tasks: int = 5) -> list[ResearchTask]:
        if max_tasks < 3 or max_tasks > 5:
            raise ValueError("TodoPlanner max_tasks must be between 3 and 5.")

        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You are a research planning agent. Return only a JSON array. "
                    "Each item must contain title, intent, and query. No markdown."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"Break this research topic into {max_tasks} focused TODO tasks: "
                    f"{topic}"
                ),
            ),
        ]

        last_error: AgentOutputError | None = None
        for _ in range(self.retries + 1):
            raw_output = await self.llm.complete(messages, temperature=0.1)
            try:
                return self._parse_tasks(raw_output, max_tasks=max_tasks)
            except AgentOutputError as exc:
                last_error = exc

        raise last_error or AgentOutputError("Planner failed to produce valid tasks.")

    def _parse_tasks(self, raw_output: str, max_tasks: int) -> list[ResearchTask]:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AgentOutputError("Planner output must be a JSON array of tasks.") from exc

        if not isinstance(parsed, list):
            raise AgentOutputError("Planner output must be a JSON array of tasks.")
        if len(parsed) < 3 or len(parsed) > max_tasks:
            raise AgentOutputError("Planner output must contain 3 to 5 tasks.")

        tasks: list[ResearchTask] = []
        try:
            for item in parsed:
                task = _PlannerTaskOutput.model_validate(item)
                tasks.append(
                    ResearchTask(
                        title=task.title,
                        intent=task.intent,
                        query=task.query,
                        status=TaskStatus.PENDING,
                    )
                )
        except ValidationError as exc:
            raise AgentOutputError(
                "Planner task items must contain only title, intent, and query."
            ) from exc

        return tasks


class TaskSummarizer:
    def __init__(self, llm: LLMProvider, skill_name: str | None = None) -> None:
        self.llm = llm
        self.skill_name = skill_name
        self._tool_registry: Any | None = None
        self._progress: Any | None = None

    def configure_tool_runtime(self, registry: Any, progress: Any) -> None:
        self._tool_registry = registry
        self._progress = progress

    async def summarize(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> TaskSummary:
        if self.skill_name and self._tool_registry is not None and self._progress is not None:
            try:
                return await self._summarize_with_tools(task, search_results)
            except Exception as exc:
                if not self._is_skill_load_error(exc):
                    raise

        return await self._summarize_direct(task, search_results)

    async def _summarize_direct(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> TaskSummary:
        content = (await self.llm.complete(self._summary_messages(task, search_results), temperature=0.2)).strip()
        return self._build_summary(task, search_results, content)

    async def _summarize_with_tools(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> TaskSummary:
        from app.tool_calling import ToolCallingAgentRunner, ToolExecutionContext

        context = ToolExecutionContext(topic=task.title, task=task, search_results=list(search_results))
        runner = ToolCallingAgentRunner[str](
            llm=self.llm,
            registry=self._tool_registry,
            progress=self._progress,
            fallback_parse_retries=1,
        )
        content = await runner.run(
            agent_name="TaskSummarizer",
            goal=self._tool_summary_goal(task, search_results),
            system_prompt=self._tool_summary_system_prompt(),
            context=context,
            stop_condition=lambda request, _context: request.action == "final",
            output_parser=self._parse_tool_summary_output,
            max_tool_calls=2,
            allowed_actions={"load_skill"},
            status_data={"task_title": task.title},
        )
        return self._build_summary(task, search_results, content)

    def _summary_messages(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> list[LLMMessage]:
        source_lines = [
            f"[{index}] {result.title} - {result.url} - {result.snippet}"
            for index, result in enumerate(search_results, start=1)
        ]
        return [
            LLMMessage(
                role="system",
                content=(
                    "You summarize research evidence into concise Markdown. "
                    "Use citation markers like [1] when citing sources. If fewer "
                    "than 3 sources are available, explicitly mark the evidence "
                    "as 资料不足 or 待验证."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"Task: {task.title}\nIntent: {task.intent}\n\nSources:\n"
                    + "\n".join(source_lines)
                ),
            ),
        ]

    def _tool_summary_system_prompt(self) -> str:
        return (
            "You summarize research evidence into concise Markdown. "
            "You may call load_skill to read the configured summarization skill manual. "
            "When finished, return strict JSON with action final and arguments containing "
            'a non-empty "content" Markdown string. Use citation markers like [1]. '
            "If fewer than 3 sources are available, explicitly mark the evidence as 资料不足 or 待验证."
        )

    def _tool_summary_goal(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> str:
        source_lines = [
            f"[{index}] {result.title} - {result.url} - {result.snippet}"
            for index, result in enumerate(search_results, start=1)
        ]
        return (
            f"Task: {task.title}\n"
            f"Intent: {task.intent}\n"
            f"Skill to load when useful: {self.skill_name}\n\n"
            "Sources:\n"
            + "\n".join(source_lines)
        )

    def _parse_tool_summary_output(self, request: Any, context: Any) -> str:
        content = request.arguments.get("content") or request.arguments.get("summary")
        if not isinstance(content, str) or not content.strip():
            raise AgentOutputError("Task summarizer final action must include non-empty content.")
        return content.strip()

    def _build_summary(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
        content: str,
    ) -> TaskSummary:
        if not content:
            raise AgentOutputError("Task summarizer returned empty Markdown.")
        return TaskSummary(task_title=task.title, content=content, sources=list(search_results))

    def _is_skill_load_error(self, exc: Exception) -> bool:
        code = getattr(exc, "code", "")
        return code in {
            "skill_not_found",
            "skill_manual_missing",
            "invalid_skill_name",
            "skill_load_failed",
        }


class _EvidenceJudgementOutput(BaseModel):
    is_sufficient: bool
    confidence: EvidenceConfidence
    gaps: list[str] = []
    rationale: str

    model_config = ConfigDict(extra="forbid")


class EvidenceJudge:
    def __init__(self, llm: LLMProvider, retries: int = 1) -> None:
        self.llm = llm
        self.retries = retries

    async def judge(
        self,
        topic: str,
        completed_summaries: Sequence[TaskSummary],
        sources: Sequence[SearchResult],
        failed_tasks: Sequence[TaskExecutionRecord],
    ) -> EvidenceJudgement:
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You judge whether research evidence is sufficient before report writing. "
                    "Return only strict JSON with is_sufficient, confidence, gaps, and rationale. "
                    "confidence must be one of high, medium, low. Do not include markdown."
                ),
            ),
            LLMMessage(
                role="user",
                content=self._build_prompt(topic, completed_summaries, sources, failed_tasks),
            ),
        ]

        last_error: AgentOutputError | None = None
        for _ in range(self.retries + 1):
            raw_output = await self.llm.complete(messages, temperature=0.1)
            try:
                return self._parse_judgement(raw_output)
            except AgentOutputError as exc:
                last_error = exc

        raise last_error or AgentOutputError("Evidence judge failed to produce valid JSON.")

    def _build_prompt(
        self,
        topic: str,
        completed_summaries: Sequence[TaskSummary],
        sources: Sequence[SearchResult],
        failed_tasks: Sequence[TaskExecutionRecord],
    ) -> str:
        summary_blocks = []
        for summary in completed_summaries:
            summary_blocks.append(
                f"Task: {summary.task_title}\n"
                f"Source count: {len(summary.sources)}\n"
                f"Summary: {summary.content}"
            )
        source_lines = [
            f"- {source.title} | {source.url} | {source.snippet}" for source in sources
        ]
        failed_lines = [
            f"- {record.task.title}: {record.error or 'failed'}" for record in failed_tasks
        ]
        return (
            f"Topic: {topic}\n\n"
            "Completed summaries:\n"
            + ("\n\n".join(summary_blocks) or "None")
            + "\n\nSources:\n"
            + ("\n".join(source_lines) or "None")
            + "\n\nFailed tasks:\n"
            + ("\n".join(failed_lines) or "None")
        )

    def _parse_judgement(self, raw_output: str) -> EvidenceJudgement:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AgentOutputError("Evidence judge output must be strict JSON.") from exc
        try:
            output = _EvidenceJudgementOutput.model_validate(parsed)
        except ValidationError as exc:
            raise AgentOutputError("Evidence judge output has invalid structure.") from exc
        return EvidenceJudgement(
            is_sufficient=output.is_sufficient,
            confidence=output.confidence,
            gaps=output.gaps,
            rationale=output.rationale,
        )


class _QueryRewriteOutput(BaseModel):
    queries: list[str]

    model_config = ConfigDict(extra="forbid")


class QueryRewriter:
    def __init__(self, llm: LLMProvider, retries: int = 1) -> None:
        self.llm = llm
        self.retries = retries

    async def rewrite(
        self,
        *,
        topic: str,
        evidence_gaps: Sequence[str],
        existing_task_queries: Sequence[str],
        existing_source_summaries: Sequence[str],
        max_queries: int = 2,
    ) -> list[str]:
        if max_queries < 1 or max_queries > 2:
            raise ValueError("QueryRewriter max_queries must be between 1 and 2.")

        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You rewrite research gaps into supplemental web search queries. "
                    "Return only strict JSON shaped as {\"queries\":[...]}. "
                    "Return at most the requested number of queries. Do not include markdown."
                ),
            ),
            LLMMessage(
                role="user",
                content=self._build_prompt(
                    topic=topic,
                    evidence_gaps=evidence_gaps,
                    existing_task_queries=existing_task_queries,
                    existing_source_summaries=existing_source_summaries,
                    max_queries=max_queries,
                ),
            ),
        ]

        last_error: AgentOutputError | None = None
        for _ in range(self.retries + 1):
            raw_output = await self.llm.complete(messages, temperature=0.1)
            try:
                return self._parse_queries(
                    raw_output,
                    existing_task_queries=existing_task_queries,
                    max_queries=max_queries,
                )
            except AgentOutputError as exc:
                last_error = exc

        raise last_error or AgentOutputError("Query rewriter failed to produce valid JSON.")

    def _build_prompt(
        self,
        *,
        topic: str,
        evidence_gaps: Sequence[str],
        existing_task_queries: Sequence[str],
        existing_source_summaries: Sequence[str],
        max_queries: int,
    ) -> str:
        return (
            f"Topic: {topic}\n"
            f"Maximum supplemental queries: {max_queries}\n\n"
            "Evidence gaps:\n"
            + ("\n".join(f"- {gap}" for gap in evidence_gaps) or "None")
            + "\n\nExisting task queries:\n"
            + ("\n".join(f"- {query}" for query in existing_task_queries) or "None")
            + "\n\nExisting source summaries:\n"
            + ("\n".join(f"- {summary}" for summary in existing_source_summaries) or "None")
        )

    def _parse_queries(
        self,
        raw_output: str,
        *,
        existing_task_queries: Sequence[str],
        max_queries: int,
    ) -> list[str]:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AgentOutputError("Query rewriter output must be strict JSON.") from exc
        try:
            output = _QueryRewriteOutput.model_validate(parsed)
        except ValidationError as exc:
            raise AgentOutputError("Query rewriter output has invalid structure.") from exc

        existing = {query.casefold().strip() for query in existing_task_queries}
        deduped: list[str] = []
        seen: set[str] = set()
        for query in output.queries:
            normalized = query.strip()
            key = normalized.casefold()
            if not normalized or key in existing or key in seen:
                continue
            deduped.append(normalized)
            seen.add(key)
            if len(deduped) >= max_queries:
                break

        return deduped


class ReportWriter:
    def __init__(
        self,
        llm: LLMProvider,
        citation_validator: CitationQualityValidator | None = None,
        retries: int = 1,
    ) -> None:
        self.llm = llm
        self.citation_validator = citation_validator or CitationQualityValidator()
        self.retries = retries

    async def write(
        self,
        topic: str,
        summaries: Sequence[TaskSummary],
        evidence_judgement: EvidenceJudgement | None = None,
    ) -> FinalReport:
        messages = [
            LLMMessage(
                role="system",
                content=self._system_prompt(evidence_judgement),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"Topic: {topic}\n\n"
                    f"Evidence status:\n{self._build_evidence_block(evidence_judgement)}\n\n"
                    f"Task summaries and sources:\n{self._build_summary_blocks(summaries)}"
                ),
            ),
        ]

        last_error: AgentOutputError | None = None
        for attempt in range(self.retries + 1):
            markdown = (await self.llm.complete(messages, temperature=0.2)).strip()
            try:
                return self._build_validated_report(
                    topic,
                    markdown,
                    summaries,
                    evidence_judgement,
                )
            except AgentOutputError as exc:
                last_error = exc
                if attempt < self.retries:
                    messages.extend(
                        [
                            LLMMessage(role="assistant", content=markdown or "EMPTY_REPORT"),
                            LLMMessage(
                                role="user",
                                content=(
                                    "Revise the report to fix this validation error: "
                                    f"{exc}. Return only the full corrected Markdown report. "
                                    "Keep all source URLs in the References section."
                                ),
                            ),
                        ]
                    )

        raise last_error or AgentOutputError("Report writer failed to produce a valid report.")

    def _system_prompt(self, evidence_judgement: EvidenceJudgement | None) -> str:
        prompt = (
            "You write Markdown research reports. Include a title, overview, "
            "sectioned analysis, conclusion, and references. Every key "
            "claim must cite sources with markers like [1]. The references "
            "section must include every source URL. If a task has fewer "
            "than 3 sources, explicitly mark that evidence as 资料不足 or 待验证."
        )
        if evidence_judgement is not None and not evidence_judgement.is_sufficient:
            prompt += (
                " The evidence judge marked the evidence as insufficient. "
                "The report must explicitly state 证据不足 or 待验证, distinguish "
                "high-confidence conclusions from claims needing verification, "
                "and avoid overstating the available evidence."
            )
        return prompt

    def _validate_report(self, markdown: str) -> None:
        normalized = markdown.casefold()
        has_overview = "概述" in markdown or "overview" in normalized
        has_conclusion = "总结" in markdown or "conclusion" in normalized
        has_references = "参考文献" in markdown or "references" in normalized
        if not markdown.lstrip().startswith("#") or not (
            has_overview and has_conclusion and has_references
        ):
            raise AgentOutputError(
                "Report must include a title, overview, conclusion, and references."
            )

    def _validate_evidence_status(
        self,
        markdown: str,
        evidence_judgement: EvidenceJudgement | None,
    ) -> None:
        if evidence_judgement is None or evidence_judgement.is_sufficient:
            return

        normalized = markdown.casefold()
        if any(marker in normalized for marker in ("证据不足", "资料不足", "待验证", "仅供参考")):
            return
        raise AgentOutputError(
            "Reports with insufficient evidence judgement must mark the limitation "
            "as 证据不足, 资料不足, 待验证, or 仅供参考."
        )

    def _collect_sources(self, summaries: Sequence[TaskSummary]) -> list[SearchResult]:
        sources_by_url: dict[str, SearchResult] = {}
        for summary in summaries:
            for source in summary.sources:
                sources_by_url[str(source.url)] = source
        return list(sources_by_url.values())

    def _build_evidence_block(self, evidence_judgement: EvidenceJudgement | None) -> str:
        if evidence_judgement is None:
            return "No evidence judgement was provided."
        gaps = "\n".join(f"- {gap}" for gap in evidence_judgement.gaps) or "None"
        return (
            f"is_sufficient: {evidence_judgement.is_sufficient}\n"
            f"confidence: {evidence_judgement.confidence}\n"
            f"gaps:\n{gaps}\n"
            f"rationale: {evidence_judgement.rationale}"
        )

    def _build_summary_blocks(self, summaries: Sequence[TaskSummary]) -> str:
        blocks: list[str] = []
        for summary in summaries:
            source_lines = [
                f"[{index}] {source.title} - {source.url} - {source.snippet}"
                for index, source in enumerate(summary.sources, start=1)
            ]
            blocks.append(
                f"## {summary.task_title}\n"
                f"{summary.content}\n\n"
                "Sources:\n"
                + "\n".join(source_lines)
            )
        return "\n\n".join(blocks)

    def _build_validated_report(
        self,
        topic: str,
        markdown: str,
        summaries: Sequence[TaskSummary],
        evidence_judgement: EvidenceJudgement | None,
    ) -> FinalReport:
        if not markdown:
            raise AgentOutputError("Report writer returned empty Markdown.")
        self._validate_report(markdown)
        self._validate_evidence_status(markdown, evidence_judgement)
        sources = self._collect_sources(summaries)
        markdown = self._append_missing_reference_urls(markdown, sources)
        report = FinalReport(
            title=topic,
            markdown=markdown,
            sources=sources,
        )
        try:
            self.citation_validator.validate(report, summaries)
        except CitationQualityError as exc:
            raise AgentOutputError(str(exc)) from exc
        return report

    def _append_missing_reference_urls(
        self,
        markdown: str,
        sources: Sequence[SearchResult],
    ) -> str:
        reference_match = re.search(
            r"(?ims)^#{2,3}\s*(references|参考文献)\s*$",
            markdown,
        )
        if not reference_match:
            return markdown

        reference_section = markdown[reference_match.end() :]
        normalized_references = self._normalize_text_urls(reference_section)
        missing_urls = [
            str(source.url)
            for source in sources
            if self._normalize_url(str(source.url)) not in normalized_references
        ]
        if not missing_urls:
            return markdown

        existing_numbers = [
            int(number) for number in re.findall(r"\[(\d+)\]", reference_section)
        ]
        next_number = max(existing_numbers, default=0) + 1
        appended_lines = []
        for offset, url in enumerate(missing_urls):
            appended_lines.append(f"[{next_number + offset}] {url}")

        separator = "\n" if markdown.endswith("\n") else "\n\n"
        return markdown + separator + "\n".join(appended_lines)

    def _normalize_text_urls(self, text: str) -> str:
        urls = re.findall(r"https?://[^\s)\]]+", text)
        normalized = text
        for url in urls:
            normalized = normalized.replace(url, self._normalize_url(url))
        return normalized

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                "",
                parsed.query,
                "",
            )
        )
