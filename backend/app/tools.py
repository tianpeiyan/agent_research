import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import ValidationError

from app.models import NoteRecord, ResearchTask, SearchResult, TaskSummary


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TavilySearchTool:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        max_results: int = 5,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_results < 1 or max_results > 5:
            raise ValueError("TavilySearchTool max_results must be between 1 and 5.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_results = max_results
        self.timeout = timeout
        self.transport = transport

    async def search(self, task: ResearchTask) -> list[SearchResult]:
        if not self.api_key:
            raise ToolError("missing_search_api_key", "缺少 TAVILY_API_KEY。")

        payload = {
            "query": task.query,
            "max_results": self.max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/search",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise ToolError("search_timeout", "Tavily 搜索超时。") from exc
        except httpx.HTTPError as exc:
            raise ToolError("search_failed", "Tavily 搜索请求失败。") from exc

        results = data.get("results")
        if not isinstance(results, list):
            raise ToolError("invalid_search_response", "Tavily 响应缺少 results。")

        search_results = self._parse_results(results)
        if not search_results:
            raise ToolError("no_search_results", "Tavily 没有返回搜索结果。")
        return search_results

    def _parse_results(self, raw_results: list[object]) -> list[SearchResult]:
        deduped: dict[str, SearchResult] = {}

        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                raise ToolError("invalid_search_response", "Tavily 单条结果格式错误。")

            url = raw_result.get("url")
            if not isinstance(url, str):
                raise ToolError("invalid_search_response", "Tavily 单条结果缺少 URL。")

            try:
                result = SearchResult(
                    title=raw_result.get("title", ""),
                    url=url,
                    snippet=raw_result.get("content") or raw_result.get("snippet") or "",
                    source=self._source_from_url(url),
                )
            except ValidationError as exc:
                raise ToolError(
                    "invalid_search_response",
                    "Tavily 单条结果无法转换为内部搜索结果。",
                ) from exc

            normalized_url = self._normalize_url(str(result.url))
            if normalized_url not in deduped:
                deduped[normalized_url] = result

        return list(deduped.values())

    def _source_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc or "unknown"

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


class NoteTool:
    def __init__(self, notes_path: Path) -> None:
        self.notes_path = notes_path

    def save(
        self,
        summary: TaskSummary,
        tags: list[str] | None = None,
    ) -> NoteRecord:
        record = NoteRecord(
            task_title=summary.task_title,
            summary_content=summary.content,
            sources=summary.sources,
            tags=tags or [],
            created_at=datetime.now(UTC),
        )
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        with self.notes_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
            file.write("\n")
        return record
