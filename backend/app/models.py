from enum import StrEnum
import json
from typing import Any, Annotated
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, StringConstraints


MAX_TOPIC_LENGTH = 200

NonEmptyShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SSEEventType(StrEnum):
    STATUS = "status"
    TASK = "task"
    SEARCH_RESULTS = "search_results"
    SUMMARY = "summary"
    REPORT = "report"
    ERROR = "error"
    DONE = "done"


class ToolCallStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class EvidenceConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ToolDefinition(BaseModel):
    name: NonEmptyShortText
    description: NonEmptyShortText
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCallRequest(BaseModel):
    action: NonEmptyShortText
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: NonEmptyShortText
    call_id: str | None = None


class ToolCallResult(BaseModel):
    action: NonEmptyShortText
    success: bool
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    call_id: str | None = None


class ToolCallingTurn(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ResearchRequest(BaseModel):
    topic: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_TOPIC_LENGTH,
        ),
    ]
    max_tasks: int = Field(default=5, ge=3, le=5)


class ResearchTask(BaseModel):
    title: NonEmptyShortText
    intent: NonEmptyShortText
    query: NonEmptyShortText
    status: TaskStatus = TaskStatus.PENDING


class SearchResult(BaseModel):
    title: NonEmptyShortText
    url: HttpUrl
    snippet: NonEmptyShortText
    source: NonEmptyShortText


class TaskSummary(BaseModel):
    task_title: NonEmptyShortText
    content: NonEmptyShortText
    sources: list[SearchResult] = Field(default_factory=list)


class EvidenceJudgement(BaseModel):
    is_sufficient: bool
    confidence: EvidenceConfidence
    gaps: list[NonEmptyShortText] = Field(default_factory=list)
    rationale: NonEmptyShortText


class NoteRecord(BaseModel):
    task_title: NonEmptyShortText
    summary_content: NonEmptyShortText
    sources: list[SearchResult] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class TaskExecutionRecord(BaseModel):
    task: ResearchTask
    search_results: list[SearchResult] = Field(default_factory=list)
    summary: TaskSummary | None = None
    note: NoteRecord | None = None
    status: TaskStatus
    error: str | None = None


class ToolCallLog(BaseModel):
    created_at: datetime
    stage: NonEmptyShortText
    tool_name: NonEmptyShortText
    input_summary: NonEmptyShortText
    output_summary: NonEmptyShortText
    status: ToolCallStatus
    error: str | None = None


class FinalReport(BaseModel):
    title: NonEmptyShortText
    markdown: NonEmptyShortText
    sources: list[SearchResult] = Field(default_factory=list)


class ResearchResult(BaseModel):
    topic: NonEmptyShortText
    tasks: list[TaskExecutionRecord]
    report: FinalReport
    tool_logs: list[ToolCallLog] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    code: NonEmptyShortText
    message: NonEmptyShortText
    details: dict[str, Any] | None = None


class SSEEvent(BaseModel):
    type: SSEEventType
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        payload = self.model_dump(mode="json")["data"]
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.type.value}\ndata: {data}\n\n"
