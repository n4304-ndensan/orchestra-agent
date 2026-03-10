from __future__ import annotations

from http import HTTPStatus

import httpx

from orchestra_agent.shared.error_handling import classify_exception


def test_classify_exception_marks_missing_api_key_as_configuration_error() -> None:
    report = classify_exception(
        ValueError("Google Gemini API key is required. Set 'GEMINI_API_KEY' or 'GOOGLE_API_KEY'.")
    )

    assert report.code == "invalid_configuration"
    assert report.exit_code == 2
    assert report.http_status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "--llm-provider none" in str(report.hint)


def test_classify_exception_detects_wrapped_rate_limit_error() -> None:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(429, request=request)
    cause = httpx.HTTPStatusError("rate limited", request=request, response=response)
    exc = RuntimeError("Google Gemini request failed with HTTP 429.")
    exc.__cause__ = cause
    report = classify_exception(exc)

    assert report.code == "upstream_rate_limited"
    assert report.http_status == HTTPStatus.TOO_MANY_REQUESTS
    assert report.details == {
        "method": "POST",
        "status_code": 429,
        "url": "https://example.com",
    }


def test_classify_exception_detects_malformed_llm_output() -> None:
    report = classify_exception(RuntimeError("LLM step executor output is not valid JSON."))

    assert report.code == "malformed_llm_output"
    assert "deterministic" in str(report.hint)
