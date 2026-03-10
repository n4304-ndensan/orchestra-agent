from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx


@dataclass(slots=True, frozen=True)
class ErrorReport:
    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None
    exit_code: int = 1
    http_status: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.hint is not None:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


def classify_exception(exc: BaseException) -> ErrorReport:
    message = clean_exception_message(exc)
    chained_report = _classify_chained_exception(exc, message)
    if chained_report is not None:
        return chained_report

    if isinstance(exc, OSError) and _is_port_in_use_error(exc):
        return ErrorReport(
            code="port_in_use",
            message=message or "Network port is already in use.",
            hint="Choose another port or stop the conflicting process.",
            exit_code=2,
            http_status=HTTPStatus.CONFLICT,
        )

    if isinstance(exc, ValueError):
        return _classify_value_error(message)
    if isinstance(exc, RuntimeError):
        return _classify_runtime_error(message)

    return ErrorReport(
        code="internal_error",
        message=message or f"{type(exc).__name__} was raised.",
        hint="Inspect logs for details and retry once the underlying issue is fixed.",
    )


def clean_exception_message(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        raw = exc.args[0]
        if isinstance(raw, str):
            return raw
    text = str(exc).strip()
    return text or exc.__class__.__name__


def human_error_lines(report: ErrorReport) -> list[str]:
    lines = [f"[error] {report.code}", f"  message  {report.message}"]
    if report.hint is not None:
        lines.append(f"  hint     {report.hint}")
    if report.details:
        for key in sorted(report.details):
            value = report.details[key]
            lines.append(f"  {key:<8} {value}")
    return lines


def text_preview(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _classify_chained_exception(
    exc: BaseException,
    message: str,
) -> ErrorReport | None:
    for current in _exception_chain(exc):
        report = _classify_single_exception(current, message)
        if report is not None:
            return report
    return None


def _classify_single_exception(
    exc: BaseException,
    message: str,
) -> ErrorReport | None:
    if isinstance(exc, KeyboardInterrupt):
        return ErrorReport(
            code="interrupted",
            message="Operation interrupted by user.",
            exit_code=130,
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, json.JSONDecodeError):
        return ErrorReport(
            code="invalid_json",
            message=message or "JSON payload is invalid.",
            hint="Check the JSON syntax and retry.",
            exit_code=2,
            http_status=HTTPStatus.BAD_REQUEST,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_http_status_error(exc, message)
    if isinstance(exc, httpx.TimeoutException):
        return ErrorReport(
            code="upstream_timeout",
            message=message or "Upstream request timed out.",
            hint="Check service health or increase the timeout before retrying.",
            details=_httpx_request_details(exc),
            http_status=HTTPStatus.GATEWAY_TIMEOUT,
        )
    if isinstance(exc, httpx.RequestError):
        return ErrorReport(
            code="upstream_unavailable",
            message=message or "Upstream request failed.",
            hint="Check endpoint URL, container health, and network connectivity.",
            details=_httpx_request_details(exc),
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    if isinstance(exc, PermissionError):
        return ErrorReport(
            code="permission_denied",
            message=message or "Permission denied.",
            hint="Check filesystem permissions and retry.",
            exit_code=2,
            http_status=HTTPStatus.FORBIDDEN,
        )
    if isinstance(exc, FileNotFoundError):
        return ErrorReport(
            code="missing_file",
            message=message or "Required file was not found.",
            hint="Verify the path exists inside the configured workspace.",
            exit_code=2,
            http_status=HTTPStatus.BAD_REQUEST,
        )
    if isinstance(exc, KeyError):
        return ErrorReport(
            code="not_found",
            message=message or "Requested resource was not found.",
            exit_code=2,
            http_status=HTTPStatus.NOT_FOUND,
        )
    return None


def _classify_http_status_error(
    exc: httpx.HTTPStatusError,
    message: str,
) -> ErrorReport:
    status_code = exc.response.status_code
    details = _httpx_request_details(exc)
    details["status_code"] = status_code
    if status_code == HTTPStatus.TOO_MANY_REQUESTS:
        return ErrorReport(
            code="upstream_rate_limited",
            message=message or "Upstream service rate limited the request.",
            hint="Retry later, reduce concurrency, or switch provider/model.",
            details=details,
            http_status=HTTPStatus.TOO_MANY_REQUESTS,
        )
    if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return ErrorReport(
            code="upstream_auth_failed",
            message=message or "Upstream authentication failed.",
            hint="Check API keys, auth headers, and provider permissions.",
            exit_code=2,
            details=details,
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return ErrorReport(
            code="upstream_server_error",
            message=message or "Upstream service returned a server error.",
            hint="Retry later and inspect the upstream service logs if it persists.",
            details=details,
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    return ErrorReport(
        code="upstream_http_error",
        message=message or "Upstream service rejected the request.",
        hint="Review the request payload and upstream service configuration.",
        details=details,
        exit_code=2,
        http_status=HTTPStatus.BAD_GATEWAY,
    )


def _classify_value_error(message: str) -> ErrorReport:
    if "API key is required" in message or "Environment variable '" in message:
        return ErrorReport(
            code="invalid_configuration",
            message=message,
            hint="Set the required credential env var or run with --llm-provider none.",
            exit_code=2,
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if "LLM TLS CA bundle was not found" in message:
        return ErrorReport(
            code="invalid_configuration",
            message=message,
            hint="Point llm.tls_ca_bundle to an existing certificate file or unset it.",
            exit_code=2,
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if "Config " in message or message.startswith("mcp.") or message.startswith("llm."):
        return ErrorReport(
            code="invalid_configuration",
            message=message,
            hint="Review the TOML config and fix invalid values before retrying.",
            exit_code=2,
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return ErrorReport(
        code="invalid_request",
        message=message,
        hint="Review the command or request payload and retry.",
        exit_code=2,
        http_status=HTTPStatus.BAD_REQUEST,
    )


def _classify_runtime_error(message: str) -> ErrorReport:
    if "not valid JSON" in message:
        return ErrorReport(
            code="malformed_llm_output",
            message=message,
            hint="Retry, lower model temperature, or switch to deterministic planning/execution.",
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    if message.startswith("MCP error for") or message.startswith("MCP endpoint"):
        return ErrorReport(
            code="mcp_execution_failed",
            message=message,
            hint="Check the MCP server logs and validate the tool input/output contract.",
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    if "Duplicate MCP tool registrations detected" in message:
        return ErrorReport(
            code="invalid_configuration",
            message=message,
            hint="Expose each MCP tool from exactly one endpoint.",
            exit_code=2,
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if "Google Gemini" in message or "OpenAI" in message:
        return ErrorReport(
            code="llm_request_failed",
            message=message,
            hint="Check provider credentials, quotas, and upstream response logs.",
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    return ErrorReport(
        code="operation_failed",
        message=message,
        hint="Retry once. If it keeps failing, inspect logs and the persisted run state.",
        http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


def _httpx_request_details(exc: httpx.RequestError) -> dict[str, Any]:
    details: dict[str, Any] = {}
    request = exc.request
    details["method"] = request.method
    details["url"] = str(request.url)
    return details


def _is_port_in_use_error(exc: OSError) -> bool:
    if getattr(exc, "errno", None) in {48, 98, 10048}:
        return True
    return "address already in use" in clean_exception_message(exc).lower()
