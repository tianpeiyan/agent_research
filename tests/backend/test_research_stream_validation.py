from fastapi.testclient import TestClient

from app.main import app


def test_research_stream_returns_clear_error_for_empty_topic() -> None:
    client = TestClient(app)

    response = client.get("/research/stream", params={"topic": "   "})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_research_request"
    assert "Research topic must be non-empty" in detail["message"]


def test_research_stream_returns_clear_error_for_too_long_topic() -> None:
    client = TestClient(app)

    response = client.get("/research/stream", params={"topic": "x" * 201})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_research_request"
    assert "Research topic must be non-empty" in detail["message"]


def test_research_stream_returns_clear_error_for_invalid_max_tasks() -> None:
    client = TestClient(app)

    response = client.get("/research/stream", params={"topic": "AI agents", "max_tasks": 2})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_research_request"
    assert "max_tasks must be between 3 and 5" in detail["message"]
