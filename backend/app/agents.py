import json
import re
from collections.abc import Sequence
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, ValidationError

from app.citations import CitationQualityError, CitationQualityValidator
from app.llm import LLMMessage, LLMProvider
from app.models import FinalReport, ResearchTask, SearchResult, TaskStatus, TaskSummary


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
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    async def summarize(
        self,
        task: ResearchTask,
        search_results: Sequence[SearchResult],
    ) -> TaskSummary:
        source_lines = [
            f"[{index}] {result.title} - {result.url} - {result.snippet}"
            for index, result in enumerate(search_results, start=1)
        ]
        messages = [
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
        content = (await self.llm.complete(messages, temperature=0.2)).strip()
        if not content:
            raise AgentOutputError("Task summarizer returned empty Markdown.")
        return TaskSummary(task_title=task.title, content=content, sources=list(search_results))


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

    async def write(self, topic: str, summaries: Sequence[TaskSummary]) -> FinalReport:
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You write Markdown research reports. Include a title, overview, "
                    "sectioned analysis, conclusion, and references. Every key "
                    "claim must cite sources with markers like [1]. The references "
                    "section must include every source URL. If a task has fewer "
                    "than 3 sources, explicitly mark that evidence as 资料不足 or 待验证."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"Topic: {topic}\n\n"
                    f"Task summaries and sources:\n{self._build_summary_blocks(summaries)}"
                ),
            ),
        ]

        last_error: AgentOutputError | None = None
        for attempt in range(self.retries + 1):
            markdown = (await self.llm.complete(messages, temperature=0.2)).strip()
            try:
                return self._build_validated_report(topic, markdown, summaries)
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

    def _collect_sources(self, summaries: Sequence[TaskSummary]) -> list[SearchResult]:
        sources_by_url: dict[str, SearchResult] = {}
        for summary in summaries:
            for source in summary.sources:
                sources_by_url[str(source.url)] = source
        return list(sources_by_url.values())

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
    ) -> FinalReport:
        if not markdown:
            raise AgentOutputError("Report writer returned empty Markdown.")
        self._validate_report(markdown)
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
