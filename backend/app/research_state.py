from typing import TypedDict

from app.models import (
    EvidenceJudgement,
    FinalReport,
    ResearchTask,
    SearchResult,
    TaskExecutionRecord,
    TaskSummary,
)


class ResearchWorkflowState(TypedDict, total=False):
    topic: str
    max_tasks: int
    planned_tasks: list[ResearchTask]
    current_index: int
    execution_records: list[TaskExecutionRecord]
    completed_summaries: list[TaskSummary]
    failed_tasks: list[TaskExecutionRecord]
    sources: list[SearchResult]
    evidence_judgement: EvidenceJudgement | None
    supplemental_queries: list[str]
    supplemental_rounds_used: int
    report: FinalReport


def build_initial_research_state(topic: str, max_tasks: int) -> ResearchWorkflowState:
    return {
        "topic": topic,
        "max_tasks": max_tasks,
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


def merge_sources_by_url(
    existing: list[SearchResult],
    new_sources: list[SearchResult],
) -> list[SearchResult]:
    sources_by_url = {str(source.url): source for source in existing}
    for source in new_sources:
        sources_by_url.setdefault(str(source.url), source)
    return list(sources_by_url.values())


def collect_sources_from_summaries(summaries: list[TaskSummary]) -> list[SearchResult]:
    sources: list[SearchResult] = []
    for summary in summaries:
        sources = merge_sources_by_url(sources, list(summary.sources))
    return sources
