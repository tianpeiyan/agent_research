import re
from collections.abc import Sequence
from urllib.parse import urlparse, urlunparse

from app.models import FinalReport, SearchResult, TaskSummary


class CitationQualityError(RuntimeError):
    pass


class CitationQualityValidator:
    INSUFFICIENT_MARKERS = ("资料不足", "待验证", "insufficient", "needs verification")

    def validate(self, report: FinalReport, summaries: Sequence[TaskSummary]) -> None:
        if not summaries:
            return

        self._validate_completed_tasks_have_sources(summaries)
        self._validate_source_deduplication(report.sources)
        self._validate_reference_section(report.markdown)
        self._validate_reference_urls(report.markdown, report.sources)
        self._validate_body_citation_markers(report.markdown)
        self._validate_marker_integrity(report.markdown)
        self._validate_insufficient_source_marking(report.markdown, summaries)

    def _validate_completed_tasks_have_sources(
        self,
        summaries: Sequence[TaskSummary],
    ) -> None:
        for summary in summaries:
            if not summary.sources:
                raise CitationQualityError(
                    f"Completed task '{summary.task_title}' must keep at least one source."
                )

    def _validate_source_deduplication(self, sources: Sequence[SearchResult]) -> None:
        normalized_urls = [self._normalize_url(str(source.url)) for source in sources]
        if len(normalized_urls) != len(set(normalized_urls)):
            raise CitationQualityError("Report sources must be deduplicated by URL.")

    def _validate_reference_section(self, markdown: str) -> None:
        if self._reference_section(markdown) is None:
            raise CitationQualityError("Report must include a references section.")

    def _validate_reference_urls(
        self,
        markdown: str,
        sources: Sequence[SearchResult],
    ) -> None:
        reference_section = self._reference_section(markdown)
        if reference_section is None:
            raise CitationQualityError("Report must include a references section.")

        normalized_references = self._normalize_text_urls(reference_section)
        for source in sources:
            normalized_url = self._normalize_url(str(source.url))
            if normalized_url not in normalized_references:
                raise CitationQualityError(
                    f"References must include source URL: {source.url}"
                )

    def _validate_body_citation_markers(self, markdown: str) -> None:
        body = self._body_before_references(markdown)
        if not re.search(r"\[\d+\]", body):
            raise CitationQualityError("Report body must include citation markers like [1].")

    def _validate_marker_integrity(self, markdown: str) -> None:
        body_markers = set(re.findall(r"\[(\d+)\]", self._body_before_references(markdown)))
        reference_section = self._reference_section(markdown) or ""
        reference_markers = set(re.findall(r"\[(\d+)\]", reference_section))
        missing_markers = body_markers - reference_markers
        if missing_markers:
            missing = ", ".join(sorted(missing_markers, key=int))
            raise CitationQualityError(
                f"Reference section is missing citation marker(s): {missing}."
            )

    def _validate_insufficient_source_marking(
        self,
        markdown: str,
        summaries: Sequence[TaskSummary],
    ) -> None:
        has_insufficient_sources = any(len(summary.sources) < 3 for summary in summaries)
        if not has_insufficient_sources:
            return

        normalized = markdown.casefold()
        if not any(marker in normalized for marker in self.INSUFFICIENT_MARKERS):
            raise CitationQualityError(
                "Reports with fewer than 3 sources for any task must mark "
                "the limitation as 资料不足 or 待验证."
            )

    def _reference_section(self, markdown: str) -> str | None:
        match = re.search(
            r"(?ims)^#{2,3}\s*(references|参考文献)\s*$",
            markdown,
        )
        if not match:
            return None
        return markdown[match.end() :]

    def _body_before_references(self, markdown: str) -> str:
        match = re.search(
            r"(?ims)^#{2,3}\s*(references|参考文献)\s*$",
            markdown,
        )
        if not match:
            return markdown
        return markdown[: match.start()]

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
