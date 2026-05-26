import json

import pytest
from pydantic import ValidationError

from app.models import (
    FinalReport,
    ResearchRequest,
    ResearchTask,
    SSEEvent,
    SSEEventType,
    SearchResult,
    TaskStatus,
    TaskSummary,
)


def test_research_task_contains_required_todo_fields() -> None:
    task = ResearchTask(
        title="Market landscape",
        intent="Understand current market dynamics",
        query="AI research agent market landscape",
        status=TaskStatus.PENDING,
    )

    assert task.model_dump(mode="json") == {
        "title": "Market landscape",
        "intent": "Understand current market dynamics",
        "query": "AI research agent market landscape",
        "status": "pending",
    }


def test_research_task_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        ResearchTask(
            title="Market landscape",
            intent="Understand current market dynamics",
            query="AI research agent market landscape",
            status="unknown",
        )


def test_search_result_contains_required_source_fields() -> None:
    result = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="A concise source excerpt.",
        source="example.com",
    )

    assert result.model_dump(mode="json") == {
        "title": "Example report",
        "url": "https://example.com/report",
        "snippet": "A concise source excerpt.",
        "source": "example.com",
    }


def test_search_result_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            title="Invalid",
            url="not-a-url",
            snippet="A concise source excerpt.",
            source="example.com",
        )


def test_summary_and_report_preserve_sources() -> None:
    result = SearchResult(
        title="Example report",
        url="https://example.com/report",
        snippet="A concise source excerpt.",
        source="example.com",
    )

    summary = TaskSummary(
        task_title="Market landscape",
        content="Summary with source.",
        sources=[result],
    )
    report = FinalReport(
        title="Research report",
        markdown="# Research report\n\nSummary.",
        sources=summary.sources,
    )

    assert summary.sources[0].url == result.url
    assert report.sources[0].source == "example.com"


def test_sse_event_types_cover_phase_two_protocol() -> None:
    assert {event.value for event in SSEEventType} == {
        "status",
        "task",
        "search_results",
        "summary",
        "report",
        "error",
        "done",
    }


def test_sse_event_serializes_to_event_stream_frame() -> None:
    event = SSEEvent(
        type=SSEEventType.STATUS,
        data={"message": "Planning research"},
    )

    frame = event.to_sse()
    event_line, data_line = frame.splitlines()[:2]

    assert event_line == "event: status"
    assert json.loads(data_line.removeprefix("data: ")) == {
        "message": "Planning research"
    }
    assert frame.endswith("\n\n")


def test_research_request_rejects_empty_or_too_long_topic() -> None:
    with pytest.raises(ValidationError):
        ResearchRequest(topic="   ")

    with pytest.raises(ValidationError):
        ResearchRequest(topic="x" * 201)


def test_research_request_strips_topic_whitespace() -> None:
    request = ResearchRequest(topic="  AI safety  ")

    assert request.topic == "AI safety"
    assert request.max_tasks == 5
