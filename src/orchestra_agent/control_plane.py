from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from orchestra_agent import __version__
from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.control_plane_inspector import ControlPlaneInspector
from orchestra_agent.domain.serialization import step_plan_to_dict, workflow_to_dict
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.runtime import (
    AppRuntime,
    RuntimeConfig,
    build_runtime,
    describe_mcp_tools,
    resolve_mcp_endpoints,
)
from orchestra_agent.shared.error_handling import classify_exception, human_error_lines


class ControlPlaneServer(ThreadingHTTPServer):
    runtime: AppRuntime
    workspace: Path
    started_at: datetime

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        runtime: AppRuntime,
        workspace: Path,
    ) -> None:
        self.runtime = runtime
        self.workspace = workspace
        self.started_at = datetime.now(UTC)
        super().__init__(server_address, request_handler_class)


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = _path_segments(parsed.path)
        try:
            static_response = self._dispatch_static_get(parsed.path)
            if static_response is not None:
                status, payload = static_response
                self._send_json(status, payload)
                return

            resource_response = self._dispatch_resource_get(segments, parsed.query)
            if resource_response is not None:
                status, payload = resource_response
                self._send_json(status, payload)
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")
        except Exception as exc:  # noqa: BLE001
            report = classify_exception(exc)
            self._send_error_json(
                report.http_status,
                report.code,
                report.message,
                hint=report.hint,
                details=report.details,
            )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = _path_segments(parsed.path)
        try:
            body = self._read_json_body()
            post_response = self._dispatch_post(parsed.path, segments, body)
            if post_response is not None:
                status, payload = post_response
                self._send_json(status, payload)
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")
        except Exception as exc:  # noqa: BLE001
            report = classify_exception(exc)
            self._send_error_json(
                report.http_status,
                report.code,
                report.message,
                hint=report.hint,
                details=report.details,
            )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _create_workflow(self, body: dict[str, Any]) -> dict[str, Any]:
        name = _required_str(body, "name")
        objective = _required_str(body, "objective")
        reference_files = self._resolve_reference_files(
            _optional_str_list(body.get("reference_files"))
        )
        constraints = _optional_str_list(body.get("constraints"))
        success_criteria = _optional_str_list(body.get("success_criteria"))
        workflow_id = _optional_str(body.get("workflow_id"))
        return self.server.runtime.workflow_api.create_workflow(
            name=name,
            objective=objective,
            reference_files=reference_files,
            constraints=constraints,
            success_criteria=success_criteria,
            workflow_id=workflow_id,
        )

    def _get_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.server.runtime.workflow_repo.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Workflow '{workflow_id}' not found.")
        return _serialize_workflow(workflow)

    def _get_plan(self, step_plan_id: str) -> dict[str, Any]:
        step_plan = self.server.runtime.step_plan_repo.get(step_plan_id)
        if step_plan is None:
            raise KeyError(f"StepPlan '{step_plan_id}' not found.")
        return _serialize_step_plan(step_plan)

    def _approve_plan(self, step_plan_id: str, body: dict[str, Any]) -> dict[str, Any]:
        run_flags = _optional_bool_map(body.get("run_flags"))
        skip_flags = _optional_bool_map(body.get("skip_flags"))
        reject = _as_yes_no_bool(body.get("reject"), field_name="reject", default=False)
        return self.server.runtime.approval_api.approve_step_plan(
            step_plan_id=step_plan_id,
            run_flags=run_flags,
            skip_flags=skip_flags,
            reject=reject,
        )

    def _start_run(self, body: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _required_str(body, "workflow_id")
        step_plan_id = _required_str(body, "step_plan_id")
        run_id = _optional_str(body.get("run_id"))
        approved = _as_yes_no_bool(body.get("approved"), field_name="approved", default=False)
        return self.server.runtime.run_api.start_run(
            workflow_id=workflow_id,
            step_plan_id=step_plan_id,
            run_id=run_id,
            approved=approved,
        )

    def _respond_to_approval(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        approve = _as_yes_no_bool(body.get("approve"), field_name="approve", default=True)
        feedback = _optional_str(body.get("feedback"))
        return self.server.runtime.run_api.respond_to_approval(
            run_id=run_id,
            approve=approve,
            feedback=feedback,
        )

    def _resolve_reference_files(self, raw_files: list[str] | None) -> list[str] | None:
        if raw_files is None:
            return None
        resolved_files: list[str] = []
        for raw_file in raw_files:
            resolved = (self.server.workspace / raw_file).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"Reference file '{resolved}' was not found.")
            resolved_files.append(str(resolved))
        return resolved_files

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        if hint is not None:
            payload["hint"] = hint
        if details:
            payload["details"] = details
        self._send_json(status, {"error": payload})

    def _inspector(self) -> ControlPlaneInspector:
        return ControlPlaneInspector(
            runtime=self.server.runtime,
            workspace=self.server.workspace,
            started_at=self.server.started_at,
        )

    def _dispatch_static_get(self, path: str) -> tuple[HTTPStatus, dict[str, Any]] | None:
        inspector = self._inspector()
        if path == "/":
            return HTTPStatus.OK, inspector.service_index()
        if path == "/health":
            return HTTPStatus.OK, inspector.health_payload()
        if path == "/ready":
            return inspector.readiness_payload()
        if path == "/system":
            return HTTPStatus.OK, inspector.system_payload()
        if path == "/tools":
            return (
                HTTPStatus.OK,
                {"tools": describe_mcp_tools(self.server.runtime.mcp_client)},
            )
        return None

    def _dispatch_resource_get(
        self,
        segments: list[str],
        query: str,
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        if len(segments) == 2 and segments[0] == "workflows":
            return HTTPStatus.OK, self._get_workflow(segments[1])
        if len(segments) == 2 and segments[0] == "plans":
            return HTTPStatus.OK, self._get_plan(segments[1])
        if len(segments) == 2 and segments[0] == "runs":
            return HTTPStatus.OK, self.server.runtime.run_api.get_run(segments[1])
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "audit":
            limit = _query_limit(query)
            return (
                HTTPStatus.OK,
                {
                    "events": self.server.runtime.audit_logger.list_events(
                        run_id=segments[1],
                        limit=limit,
                    )
                },
            )
        return None

    def _dispatch_post(
        self,
        path: str,
        segments: list[str],
        body: dict[str, Any],
    ) -> tuple[HTTPStatus, dict[str, Any]] | None:
        if path == "/workflows":
            return HTTPStatus.CREATED, self._create_workflow(body)
        if len(segments) == 3 and segments[0] == "workflows" and segments[2] == "plans":
            return (
                HTTPStatus.CREATED,
                self.server.runtime.workflow_api.generate_step_plan(segments[1]),
            )
        if len(segments) == 3 and segments[0] == "plans" and segments[2] == "approve":
            return HTTPStatus.OK, self._approve_plan(segments[1], body)
        if path == "/runs":
            return HTTPStatus.CREATED, self._start_run(body)
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "approval":
            return HTTPStatus.OK, self._respond_to_approval(segments[1], body)
        return None


def build_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    defaults = config or AppConfig()
    parser = argparse.ArgumentParser(description="Run orchestra-agent control plane API server.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        default=str(config.source_path) if config and config.source_path is not None else None,
        help="Path to orchestra-agent TOML config file.",
    )
    parser.add_argument(
        "--workspace",
        default=defaults.workspace.root,
        help="Workspace root for orchestration.",
    )
    parser.add_argument(
        "--workflow-root",
        default=defaults.workspace.workflow_root,
        help="Workflow storage root.",
    )
    parser.add_argument(
        "--plan-root",
        default=defaults.workspace.plan_root,
        help="Step plan storage root.",
    )
    parser.add_argument(
        "--snapshots-dir",
        default=defaults.workspace.snapshots_dir,
        help="Snapshot storage directory.",
    )
    parser.add_argument(
        "--state-root",
        default=defaults.workspace.state_root,
        help="Persistent run state directory.",
    )
    parser.add_argument(
        "--audit-root",
        default=defaults.workspace.audit_root,
        help="Persistent audit directory.",
    )
    parser.add_argument(
        "--mcp-endpoint",
        action="append",
        default=None,
        help="JSON-RPC MCP endpoint URL. Repeat to aggregate multiple MCP servers.",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["none", "file", "openai", "google"],
        default=defaults.llm.provider,
        help="LLM proposal source for planner augmentation.",
    )
    parser.add_argument(
        "--llm-proposal-file",
        default=defaults.llm.proposal_file,
        help="JSON patch file path.",
    )
    parser.add_argument("--llm-openai-model", default=defaults.llm.openai_model)
    parser.add_argument("--llm-openai-api-key-env", default=defaults.llm.openai_api_key_env)
    parser.add_argument("--llm-openai-base-url", default=defaults.llm.openai_base_url)
    parser.add_argument("--llm-openai-timeout", type=float, default=defaults.llm.openai_timeout)
    parser.add_argument("--llm-google-model", default=defaults.llm.google_model)
    parser.add_argument("--llm-google-api-key-env", default=defaults.llm.google_api_key_env)
    parser.add_argument(
        "--llm-google-base-url",
        default=defaults.llm.google_base_url,
    )
    parser.add_argument("--llm-google-timeout", type=float, default=defaults.llm.google_timeout)
    parser.add_argument(
        "--llm-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=defaults.llm.tls_verify,
    )
    parser.add_argument("--llm-tls-ca-bundle", default=defaults.llm.tls_ca_bundle)
    parser.add_argument(
        "--llm-planner-mode",
        choices=["deterministic", "augmented", "full"],
        default=defaults.llm.planner_mode,
    )
    parser.add_argument("--llm-temperature", type=float, default=defaults.llm.temperature)
    parser.add_argument("--llm-max-tokens", type=int, default=defaults.llm.max_tokens)
    parser.add_argument(
        "--repair-max-attempts",
        type=int,
        default=defaults.runtime.repair_max_attempts,
    )
    parser.add_argument("--host", default=defaults.api.host, help="HTTP bind host.")
    parser.add_argument("--port", type=int, default=defaults.api.port, help="HTTP bind port.")
    return parser


def main(argv: list[str] | None = None) -> int:
    runtime: AppRuntime | None = None
    server: ControlPlaneServer | None = None
    try:
        config_path = resolve_config_path(argv)
        config = load_app_config(config_path)
        parser = build_parser(config)
        args = parser.parse_args(argv)

        workspace = config.resolve_workspace(args.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        llm_tls_ca_bundle = (
            config.resolve_from_config(args.llm_tls_ca_bundle)
            if args.llm_tls_ca_bundle is not None and str(args.llm_tls_ca_bundle).strip()
            else None
        )
        runtime = build_runtime(
            RuntimeConfig(
                workspace=workspace,
                workflow_root=config.resolve_within_workspace(args.workflow_root, workspace),
                plan_root=config.resolve_within_workspace(args.plan_root, workspace),
                snapshots_dir=config.resolve_within_workspace(args.snapshots_dir, workspace),
                state_root=config.resolve_within_workspace(args.state_root, workspace),
                audit_root=config.resolve_within_workspace(args.audit_root, workspace),
                mcp_endpoints=resolve_mcp_endpoints(args.mcp_endpoint, config),
                llm_provider=args.llm_provider,
                llm_proposal_file=args.llm_proposal_file,
                llm_openai_model=args.llm_openai_model,
                llm_openai_api_key_env=args.llm_openai_api_key_env,
                llm_openai_base_url=args.llm_openai_base_url,
                llm_openai_timeout=args.llm_openai_timeout,
                llm_google_model=args.llm_google_model,
                llm_google_api_key_env=args.llm_google_api_key_env,
                llm_google_base_url=args.llm_google_base_url,
                llm_google_timeout=args.llm_google_timeout,
                llm_tls_verify=args.llm_tls_verify,
                llm_tls_ca_bundle=llm_tls_ca_bundle,
                llm_planner_mode=args.llm_planner_mode,
                llm_temperature=args.llm_temperature,
                llm_max_tokens=args.llm_max_tokens,
                repair_max_attempts=args.repair_max_attempts,
            )
        )
        server = ControlPlaneServer(
            (args.host, args.port),
            ControlPlaneRequestHandler,
            runtime=runtime,
            workspace=workspace,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        report = classify_exception(exc)
        for line in human_error_lines(report):
            print(line, file=sys.stderr)
        return report.exit_code
    finally:
        if runtime is not None:
            runtime.close()
        if server is not None:
            server.server_close()
    return 0


def _serialize_workflow(workflow: Workflow) -> dict[str, Any]:
    return workflow_to_dict(workflow)


def _serialize_step_plan(step_plan: StepPlan) -> dict[str, Any]:
    return step_plan_to_dict(step_plan)


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.strip("/").split("/") if segment]


def _query_limit(query: str) -> int | None:
    params = parse_qs(query)
    raw_limit = params.get("limit", [])
    if not raw_limit:
        return None
    return int(raw_limit[0])


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Field '{key}' must be a non-empty string.")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected a string or null.")
    return value


def _optional_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected an array of strings.")
    return value


def _optional_bool_map(value: Any) -> dict[str, bool] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Expected an object whose values are booleans.")
    for item in value.values():
        if not isinstance(item, bool):
            raise ValueError("Expected an object whose values are booleans.")
    return {str(key): value for key, value in value.items()}


def _as_yes_no_bool(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "y", "true", "1"}:
            return True
        if normalized in {"no", "n", "false", "0"}:
            return False
    raise ValueError(f"Field '{field_name}' must be a boolean or yes/no string.")


if __name__ == "__main__":
    raise SystemExit(main())
