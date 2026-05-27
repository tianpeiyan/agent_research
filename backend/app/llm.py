import json
from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import BaseModel, StringConstraints

from app.models import ToolCallRequest, ToolCallingTurn, ToolDefinition


MessageRole = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    pass


class LLMToolCallUnsupported(LLMError):
    pass


class LLMMessage(BaseModel):
    role: MessageRole
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    tool_call_id: str | None = None
    tool_calls: list[dict[str, object]] | None = None


class LLMProvider(Protocol):
    supports_native_tools: bool

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        pass

    async def complete_with_tools(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolDefinition],
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> ToolCallingTurn:
        pass


class BailianLLMProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        supports_native_tools: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport
        self.supports_native_tools = supports_native_tools

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        if not self.api_key:
            raise LLMError("DASHSCOPE_API_KEY is required for Bailian LLM calls.")

        payload = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            data = await self._post_chat_completion(payload, headers)
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Bailian LLM request failed or returned an invalid response.") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("Bailian LLM returned empty content.")
        return content.strip()

    async def complete_with_tools(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolDefinition],
        tool_choice: str = "auto",
        temperature: float = 0.2,
    ) -> ToolCallingTurn:
        if not self.supports_native_tools:
            raise LLMToolCallUnsupported("Native tool calling is disabled for this provider.")
        if not self.api_key:
            raise LLMError("DASHSCOPE_API_KEY is required for Bailian LLM calls.")

        payload = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "temperature": temperature,
            "tools": [self._format_tool_definition(tool) for tool in tools],
            "tool_choice": tool_choice,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            data = await self._post_chat_completion(payload, headers)
            message = data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            if self._is_tools_unsupported(exc):
                raise LLMToolCallUnsupported(
                    "Bailian LLM provider does not support native tool calling."
                ) from exc
            raise LLMError("Bailian LLM tool request failed.") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "Bailian LLM tool request failed or returned an invalid response."
            ) from exc

        return self._parse_tool_turn(message)

    async def _post_chat_completion(
        self,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    def _format_tool_definition(self, tool: ToolDefinition) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _parse_tool_turn(self, message: object) -> ToolCallingTurn:
        if not isinstance(message, dict):
            raise LLMError("Bailian LLM tool response message is invalid.")

        raw_content = message.get("content")
        content = raw_content.strip() if isinstance(raw_content, str) and raw_content.strip() else None
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raise LLMError("Bailian LLM tool_calls field is invalid.")

        tool_calls: list[ToolCallRequest] = []
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                raise LLMError("Bailian LLM returned an invalid tool call.")
            function = raw_tool_call.get("function")
            if not isinstance(function, dict):
                raise LLMError("Bailian LLM returned a tool call without function data.")
            name = function.get("name")
            raw_arguments = function.get("arguments") or "{}"
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                raise LLMError("Bailian LLM returned invalid tool call function data.")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMError("Bailian LLM returned non-JSON tool arguments.") from exc
            if not isinstance(arguments, dict):
                raise LLMError("Bailian LLM tool arguments must be a JSON object.")
            call_id = raw_tool_call.get("id")
            tool_calls.append(
                ToolCallRequest(
                    action=name,
                    arguments=arguments,
                    reason=content or "native tool call",
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )

        if not content and not tool_calls:
            raise LLMError("Bailian LLM returned empty tool response.")
        return ToolCallingTurn(content=content, tool_calls=tool_calls)

    def _is_tools_unsupported(self, exc: httpx.HTTPStatusError) -> bool:
        text = exc.response.text.casefold()
        return "tool" in text and (
            "not support" in text
            or "unsupported" in text
            or "invalid parameter" in text
            or "unknown parameter" in text
        )
