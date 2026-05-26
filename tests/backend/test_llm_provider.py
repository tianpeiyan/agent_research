import asyncio
import json

import httpx
import pytest

from app.llm import BailianLLMProvider, LLMError, LLMMessage


def test_bailian_provider_posts_openai_compatible_chat_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Provider response"}}]},
        )

    provider = BailianLLMProvider(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        transport=httpx.MockTransport(handler),
    )

    content = asyncio.run(
        provider.complete([LLMMessage(role="user", content="Hello")])
    )

    assert content == "Provider response"
    assert captured["url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert captured["auth"] == "Bearer test-key"
    assert captured["payload"] == {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": "Hello"}],
        "temperature": 0.2,
    }


def test_bailian_provider_requires_api_key() -> None:
    provider = BailianLLMProvider(
        api_key="",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
    )

    with pytest.raises(LLMError, match="DASHSCOPE_API_KEY"):
        asyncio.run(provider.complete([LLMMessage(role="user", content="Hello")]))


def test_bailian_provider_rejects_invalid_response_shape() -> None:
    provider = BailianLLMProvider(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with pytest.raises(LLMError, match="invalid response"):
        asyncio.run(provider.complete([LLMMessage(role="user", content="Hello")]))
