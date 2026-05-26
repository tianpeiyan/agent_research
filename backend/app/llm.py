from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import BaseModel, StringConstraints


MessageRole = Literal["system", "user", "assistant"]


class LLMError(RuntimeError):
    pass


class LLMMessage(BaseModel):
    role: MessageRole
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        pass


class BailianLLMProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float = 0.2,
    ) -> str:
        if not self.api_key:
            raise LLMError("DASHSCOPE_API_KEY is required for Bailian LLM calls.")

        payload = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "temperature": temperature,
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
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Bailian LLM request failed or returned an invalid response.") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("Bailian LLM returned empty content.")
        return content.strip()
