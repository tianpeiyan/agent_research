import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from app.models import SSEEvent, SSEEventType, ToolCallLog, ToolCallStatus


_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b([a-z0-9_/-]*(?:api[_-]?key|authorization|token|secret))"
        r"\s*[:=]\s*['\"]?[^'\"\s]+"
    ),
    re.compile(r"(?i)\bbearer\s+[^,\s]+"),
]


class ResearchProgressTracker:
    def __init__(self) -> None:
        self.events: list[SSEEvent] = []
        self.tool_logs: list[ToolCallLog] = []
        self._queue: asyncio.Queue[SSEEvent] | None = None

    def reset(self) -> None:
        self.events.clear()
        self.tool_logs.clear()
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()

    def enable_streaming(self) -> None:
        self._queue = asyncio.Queue()

    def emit(self, event_type: SSEEventType, data: dict[str, Any] | None = None) -> None:
        event = SSEEvent(type=event_type, data=data or {})
        self.events.append(event)
        if self._queue is not None:
            self._queue.put_nowait(event)

    async def next_event(self) -> SSEEvent:
        if self._queue is None:
            raise RuntimeError("Streaming is not enabled for this progress tracker.")
        return await self._queue.get()

    def drain_events(self) -> list[SSEEvent]:
        if self._queue is None:
            return []
        events: list[SSEEvent] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    def status(self, message: str, **data: Any) -> None:
        self.emit(SSEEventType.STATUS, {"message": message, **data})

    def error(self, message: str, **data: Any) -> None:
        self.emit(
            SSEEventType.ERROR,
            {"message": self.sanitize(message), **data},
        )

    def log_tool_call(
        self,
        stage: str,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        status: ToolCallStatus,
        error: str | None = None,
    ) -> None:
        self.tool_logs.append(
            ToolCallLog(
                created_at=datetime.now(UTC),
                stage=stage,
                tool_name=tool_name,
                input_summary=input_summary,
                output_summary=output_summary,
                status=status,
                error=self.sanitize(error) if error else None,
            )
        )

    def sanitize(self, message: str) -> str:
        sanitized = message
        for pattern in _SECRET_PATTERNS:
            sanitized = pattern.sub(lambda match: self._redact_match(match.group(0)), sanitized)
        return sanitized

    def _redact_match(self, value: str) -> str:
        if value.casefold().startswith("bearer"):
            return "Bearer <redacted>"
        separator = "=" if "=" in value else ":"
        key = value.split(separator, 1)[0].strip()
        return f"{key}{separator} <redacted>"
