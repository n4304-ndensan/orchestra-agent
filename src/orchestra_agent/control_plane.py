from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.runtime import AppRuntime, RuntimeConfig, build_runtime


class ControlPlaneServer(ThreadingHTTPServer):
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
        super().__init__(server_address, request_handler_class)


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = _path_segments(parsed.path)
        try:
            if parsed.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "workspace": str(self.server.workspace),
                    },
                )
                return

            if len(segments) == 2 and segments[0] == "workflows":
                self._send_json(HTTPStatus.OK, self._get_workflow(segments[1]))
                return

            if len(segments) == 2 and segments[0] == "plans":
                self._send_json(HTTPStatus.OK, self._get_plan(segments[1]))
                return

            if len(segments) == 2 and segments[0] == "runs":
                self._send_json(HTTPStatus.OK, self.server.runtime.run_api.get_run(segments[1]))
                return

            if len(segments) == 3 and segments[0] == "runs" and segments[2] == "audit":
                limit = _query_limit(parsed.query)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "events": self.server.runtime.audit_logger.list_events(
                            run_id=segments[1],
                            limit=limit,
                        )
                    },
                )
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")
        except KeyError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", str(exc))
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = _path_segments(parsed.path)
        try:
            body = self._read_json_body()

            if parsed.path == "/workflows":
                self._send_json(HTTPStatus.CREATED, self._create_workflow(body))
                return

            if len(segments) == 3 and segments[0] == "workflows" and segments[2] == "plans":
                self._send_json(
                    HTTPStatus.CREATED,
                    self.server.runtime.workflow_api.generate_step_plan(segments[1]),
                )
                return

            if len(segments) == 3 and segments[0] == "plans" and segments[2] == "approve":
                self._send_json(HTTPStatus.OK, self._approve_plan(segments[1], body))
                return

            if parsed.path == "/runs":
                self._send_json(HTTPStatus.CREATED, self._start_run(body))
                return

            if len(segments) == 3 and segments[0] == "runs" and segments[2] == "approval":
                self._send_json(HTTPStatus.OK, self._respond_to_approval(segments[1], body))
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")
        except KeyError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", str(exc))
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except json.JSONDecodeError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid_json", str(exc))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _create_workflow(self, body: dict[str, Any]) -> dict[str, Any]:
        name = _required_str(body, "name")
        objective = _required_str(body, "objective")
        constraints = _optional_str_list(body.get("constraints"))
        success_criteria = _optional_str_list(body.get("success_criteria"))
        workflow_id = _optional_str(body.get("workflow_id"))
        return self.server.runtime.workflow_api.create_workflow(
            name=name,
            objective=objective,
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
        reject = bool(body.get("reject", False))
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
        approved = bool(body.get("approved", False))
        return self.server.runtime.run_api.start_run(
            workflow_id=workflow_id,
            step_plan_id=step_plan_id,
            run_id=run_id,
            approved=approved,
        )

    def _respond_to_approval(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        approve = bool(body.get("approve", True))
        feedback = _optional_str(body.get("feedback"))
        return self.server.runtime.run_api.respond_to_approval(
            run_id=run_id,
            approve=approve,
            feedback=feedback,
        )

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

    def _send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})


def build_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    defaults = config or AppConfig()
    parser = argparse.ArgumentParser(description="Run orchestra-agent control plane API server.")
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
        default=defaults.mcp.endpoint,
        help="JSON-RPC MCP endpoint URL.",
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
    config_path = resolve_config_path(argv)
    config = load_app_config(config_path)
    parser = build_parser(config)
    args = parser.parse_args(argv)

    workspace = config.resolve_workspace(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(
        RuntimeConfig(
            workspace=workspace,
            workflow_root=config.resolve_within_workspace(args.workflow_root, workspace),
            plan_root=config.resolve_within_workspace(args.plan_root, workspace),
            snapshots_dir=config.resolve_within_workspace(args.snapshots_dir, workspace),
            state_root=config.resolve_within_workspace(args.state_root, workspace),
            audit_root=config.resolve_within_workspace(args.audit_root, workspace),
            mcp_endpoint=args.mcp_endpoint,
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
    try:
        server.serve_forever()
    finally:
        runtime.close()
        server.server_close()
    return 0


def _serialize_workflow(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.workflow_id,
        "name": workflow.name,
        "version": workflow.version,
        "objective": workflow.objective,
        "constraints": workflow.constraints,
        "success_criteria": workflow.success_criteria,
        "feedback_history": workflow.feedback_history,
    }


def _serialize_step_plan(step_plan: StepPlan) -> dict[str, Any]:
    return {
        "step_plan_id": step_plan.step_plan_id,
        "workflow_id": step_plan.workflow_id,
        "version": step_plan.version,
        "steps": [
            {
                "step_id": step.step_id,
                "name": step.name,
                "description": step.description,
                "tool_ref": step.tool_ref,
                "resolved_input": step.resolved_input,
                "depends_on": step.depends_on,
                "risk_level": step.risk_level.value,
                "requires_approval": step.requires_approval,
                "run": step.run,
                "skip": step.skip,
                "backup_scope": step.backup_scope.value,
            }
            for step in step_plan.steps
        ],
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
