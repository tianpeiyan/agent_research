import pytest

from app.citations import CitationQualityError, CitationQualityValidator
from app.models import FinalReport, SearchResult, TaskSummary


def _source(index: int, url: str | None = None) -> SearchResult:
    return SearchResult(
        title=f"Source {index}",
        url=url or f"https://example.com/source-{index}",
        snippet=f"Snippet {index}.",
        source="example.com",
    )


def _valid_report(sources: list[SearchResult]) -> FinalReport:
    return FinalReport(
        title="AI Agents",
        markdown="\n".join(
            [
                "# AI Agents",
                "## Overview",
                "Agents can automate multi-step work [1][2][3].",
                "## Sectioned Analysis",
                "Evidence is cross-checked against multiple sources [1][2][3].",
                "## Conclusion",
                "The finding is sufficiently sourced [1][2][3].",
                "## References",
                "[1] https://example.com/source-1",
                "[2] https://example.com/source-2",
                "[3] https://example.com/source-3",
            ]
        ),
        sources=sources,
    )


def test_citation_validator_accepts_complete_references_and_markers() -> None:
    sources = [_source(1), _source(2), _source(3)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]

    CitationQualityValidator().validate(_valid_report(sources), summaries)


def test_citation_validator_requires_completed_task_sources() -> None:
    report = _valid_report([_source(1), _source(2), _source(3)])
    summaries = [TaskSummary(task_title="Task A", content="Summary.", sources=[])]

    with pytest.raises(CitationQualityError, match="at least one source"):
        CitationQualityValidator().validate(report, summaries)


def test_citation_validator_requires_reference_urls_for_all_sources() -> None:
    sources = [_source(1), _source(2), _source(3, "https://example.com/missing")]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]

    with pytest.raises(CitationQualityError, match="source URL"):
        CitationQualityValidator().validate(_valid_report(sources), summaries)


def test_citation_validator_requires_source_urls_inside_references_section() -> None:
    sources = [_source(1), _source(2), _source(3)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]
    report = FinalReport(
        title="AI Agents",
        markdown="\n".join(
            [
                "# AI Agents",
                "## Overview",
                "Body mentions https://example.com/source-3 [3].",
                "## Sectioned Analysis",
                "Agents can automate work [1][2][3].",
                "## Conclusion",
                "The finding is sourced [1][2][3].",
                "## References",
                "[1] https://example.com/source-1",
                "[2] https://example.com/source-2",
            ]
        ),
        sources=sources,
    )

    with pytest.raises(CitationQualityError, match="source URL"):
        CitationQualityValidator().validate(report, summaries)


def test_citation_validator_requires_body_citation_markers() -> None:
    sources = [_source(1), _source(2), _source(3)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]
    report = FinalReport(
        title="AI Agents",
        markdown="\n".join(
            [
                "# AI Agents",
                "## Overview",
                "Agents can automate work.",
                "## Conclusion",
                "The finding is sourced.",
                "## References",
                "[1] https://example.com/source-1",
                "[2] https://example.com/source-2",
                "[3] https://example.com/source-3",
            ]
        ),
        sources=sources,
    )

    with pytest.raises(CitationQualityError, match="citation markers"):
        CitationQualityValidator().validate(report, summaries)


def test_citation_validator_requires_reference_marker_integrity() -> None:
    sources = [_source(1), _source(2), _source(3)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]
    report = FinalReport(
        title="AI Agents",
        markdown="\n".join(
            [
                "# AI Agents",
                "## Overview",
                "Agents can automate work [4].",
                "## Conclusion",
                "The finding is sourced [1].",
                "## References",
                "[1] https://example.com/source-1",
                "[2] https://example.com/source-2",
                "[3] https://example.com/source-3",
            ]
        ),
        sources=sources,
    )

    with pytest.raises(CitationQualityError, match="missing citation marker"):
        CitationQualityValidator().validate(report, summaries)


def test_citation_validator_requires_insufficient_source_marking() -> None:
    sources = [_source(1)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]
    report = FinalReport(
        title="AI Agents",
        markdown="\n".join(
            [
                "# AI Agents",
                "## Overview",
                "Agents can automate work [1].",
                "## Conclusion",
                "The finding is sourced [1].",
                "## References",
                "[1] https://example.com/source-1",
            ]
        ),
        sources=sources,
    )

    with pytest.raises(CitationQualityError, match="资料不足"):
        CitationQualityValidator().validate(report, summaries)


def test_citation_validator_rejects_duplicate_source_urls() -> None:
    sources = [_source(1), _source(2, "https://EXAMPLE.com/source-1/"), _source(3)]
    summaries = [TaskSummary(task_title="Task A", content="Summary [1].", sources=sources)]

    with pytest.raises(CitationQualityError, match="deduplicated"):
        CitationQualityValidator().validate(_valid_report(sources), summaries)
