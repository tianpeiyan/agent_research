from app.models import SearchResult, TaskSummary
from app.research_state import (
    build_initial_research_state,
    collect_sources_from_summaries,
    merge_sources_by_url,
)


def _source(index: int, url: str | None = None) -> SearchResult:
    return SearchResult(
        title=f"Source {index}",
        url=url or f"https://example.com/source-{index}",
        snippet=f"Snippet {index}.",
        source="example.com",
    )


def test_build_initial_research_state_defines_expected_fields() -> None:
    state = build_initial_research_state("Topic", 3)

    assert state == {
        "topic": "Topic",
        "max_tasks": 3,
        "planned_tasks": [],
        "current_index": 0,
        "execution_records": [],
        "completed_summaries": [],
        "failed_tasks": [],
        "sources": [],
        "evidence_judgement": None,
        "supplemental_queries": [],
        "supplemental_rounds_used": 0,
    }


def test_merge_sources_by_url_preserves_first_source_for_duplicate_url() -> None:
    first = _source(1, url="https://example.com/shared")
    duplicate = _source(2, url="https://example.com/shared")
    second = _source(3)

    merged = merge_sources_by_url([first], [duplicate, second])

    assert merged == [first, second]


def test_collect_sources_from_summaries_deduplicates_urls() -> None:
    first = _source(1, url="https://example.com/shared")
    duplicate = _source(2, url="https://example.com/shared")
    second = _source(3)

    summaries = [
        TaskSummary(task_title="Task A", content="A [1].", sources=[first]),
        TaskSummary(task_title="Task B", content="B [1][2].", sources=[duplicate, second]),
    ]

    assert collect_sources_from_summaries(summaries) == [first, second]
