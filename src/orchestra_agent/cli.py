from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from orchestra_agent.adapters import FilesystemStepPlanRepository
from orchestra_agent.api import RunAPI
from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.runtime import (
    AppRuntime,
    RuntimeConfig,
    build_runtime,
    resolve_file_arg,
    resolve_path,
)


def build_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    defaults = config or AppConfig()
    parser = argparse.ArgumentParser(
        description="Run orchestra-agent workflow orchestration from a single prompt."
    )
    parser.add_argument(
        "--config",
        default=str(config.source_path) if config and config.source_path is not None else None,
        help="Path to orchestra-agent TOML config file.",
    )
    parser.add_argument(
        "objective",
        nargs="?",
        help="High-level objective text, e.g. 売上Excelを集計してsummary.xlsxへ",
    )
    parser.add_argument(
        "--reference-file",
        action="append",
        default=None,
        help="Attach a local reference file to LLM requests. Repeatable.",
    )
    parser.add_argument("--workflow-id", default=None, help="Existing workflow ID to execute")
    parser.add_argument(
        "--workflow-xml",
        default=None,
        help="Path to workflow XML file to import and execute",
    )
    parser.add_argument(
        "--name",
        default=defaults.runtime.workflow_name,
        help="Workflow display name",
    )
    parser.add_argument("--run-id", default=defaults.runtime.run_id, help="Run identifier")
    parser.add_argument(
        "--workspace",
        default=defaults.workspace.root,
        help="Workspace root for relative file paths",
    )
    parser.add_argument(
        "--workflow-root",
        default=defaults.workspace.workflow_root,
        help="Workflow storage root directory",
    )
    parser.add_argument(
        "--plan-root",
        default=defaults.workspace.plan_root,
        help="StepPlan storage root directory",
    )
    parser.add_argument(
        "--snapshots-dir",
        default=defaults.workspace.snapshots_dir,
        help="Directory to store filesystem snapshots",
    )
    parser.add_argument(
        "--state-root",
        default=defaults.workspace.state_root,
        help="Directory to store persistent run state JSON files",
    )
    parser.add_argument(
        "--audit-root",
        default=defaults.workspace.audit_root,
        help="Directory to store persistent audit events",
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
        help="LLM proposal source for planner augmentation",
    )
    parser.add_argument(
        "--llm-proposal-file",
        default=defaults.llm.proposal_file,
        help="JSON patch file path when --llm-provider file",
    )
    parser.add_argument(
        "--llm-openai-model",
        default=defaults.llm.openai_model,
        help="OpenAI model name when --llm-provider openai",
    )
    parser.add_argument(
        "--llm-openai-api-key-env",
        default=defaults.llm.openai_api_key_env,
        help="Environment variable containing OpenAI API key",
    )
    parser.add_argument(
        "--llm-openai-base-url",
        default=defaults.llm.openai_base_url,
        help="OpenAI API base URL",
    )
    parser.add_argument(
        "--llm-openai-timeout",
        type=float,
        default=defaults.llm.openai_timeout,
        help="OpenAI request timeout seconds",
    )
    parser.add_argument(
        "--llm-google-model",
        default=defaults.llm.google_model,
        help="Google Gemini model name when --llm-provider google",
    )
    parser.add_argument(
        "--llm-google-api-key-env",
        default=defaults.llm.google_api_key_env,
        help="Primary environment variable containing Google Gemini API key",
    )
    parser.add_argument(
        "--llm-google-base-url",
        default=defaults.llm.google_base_url,
        help="Google Gemini Developer API base URL",
    )
    parser.add_argument(
        "--llm-google-timeout",
        type=float,
        default=defaults.llm.google_timeout,
        help="Google Gemini request timeout seconds",
    )
    parser.add_argument(
        "--llm-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=defaults.llm.tls_verify,
        help="Enable TLS certificate verification for live LLM HTTP calls.",
    )
    parser.add_argument(
        "--llm-tls-ca-bundle",
        default=defaults.llm.tls_ca_bundle,
        help="Path to custom CA bundle file for live LLM HTTP calls.",
    )
    parser.add_argument(
        "--llm-planner-mode",
        choices=["deterministic", "augmented", "full"],
        default=defaults.llm.planner_mode,
        help="Planner mode override. Defaults to full for live LLMs, augmented for file patches.",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=defaults.llm.temperature,
        help="Sampling temperature used for live LLM proposal",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=defaults.llm.max_tokens,
        help="Max tokens for live LLM proposal response",
    )
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.auto_approve,
        help="Automatically approve and resume pending approvals",
    )
    parser.add_argument(
        "--interactive-approval",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.interactive_approval,
        help=(
            "When auto-approve is off, ask approval decisions in yes/no format "
            "on the terminal."
        ),
    )
    parser.add_argument(
        "--max-resume",
        type=int,
        default=defaults.runtime.max_resume,
        help="Maximum auto-resume attempts when approval becomes pending",
    )
    parser.add_argument(
        "--repair-max-attempts",
        type=int,
        default=defaults.runtime.repair_max_attempts,
        help="Maximum failure/feedback-driven replans before the run is rejected",
    )
    parser.add_argument(
        "--print-plan",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.print_plan,
        help="Print generated step plan summary",
    )
    return parser


def _rewrite_step_plan_paths(
    step_plan_repo: FilesystemStepPlanRepository,
    step_plan_id: str,
    workspace: Path,
) -> StepPlan:
    step_plan = step_plan_repo.get(step_plan_id)
    if step_plan is None:
        raise KeyError(f"StepPlan '{step_plan_id}' not found.")
    for step in step_plan.steps:
        for key in ("file", "output"):
            raw = step.resolved_input.get(key)
            if isinstance(raw, str):
                step.resolved_input[key] = resolve_path(raw, workspace)
    step_plan_repo.save(step_plan)
    return step_plan


def _ensure_mock_source_file(step_plan: StepPlan) -> None:
    for step in step_plan.steps:
        if step.step_id != "open_file":
            continue
        file_value = step.resolved_input.get("file")
        if not isinstance(file_value, str):
            return
        source = Path(file_value)
        if source.exists():
            return
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("mock workbook placeholder", encoding="utf-8")
        return


def _resolve_workflow_id(
    args: argparse.Namespace,
    runtime: AppRuntime,
    workspace: Path,
) -> str:
    if args.workflow_xml is not None:
        xml_path = resolve_file_arg(args.workflow_xml, workspace)
        imported = runtime.workflow_repo.import_from_xml(xml_path)
        return imported.workflow_id

    if args.workflow_id is not None:
        existing = runtime.workflow_repo.get(args.workflow_id)
        if existing is not None:
            return existing.workflow_id
        if args.objective is None:
            raise ValueError("Workflow ID not found. Provide objective text to create it.")
        created = runtime.workflow_api.create_workflow(
            name=args.name,
            objective=args.objective,
            reference_files=_resolve_reference_files(args.reference_file, workspace),
            workflow_id=args.workflow_id,
        )
        return str(created["workflow_id"])

    if args.objective is None:
        raise ValueError("Objective is required when workflow is not specified.")

    created = runtime.workflow_api.create_workflow(
        name=args.name,
        objective=args.objective,
        reference_files=_resolve_reference_files(args.reference_file, workspace),
    )
    return str(created["workflow_id"])


def _resolve_reference_files(raw_files: list[str] | None, workspace: Path) -> list[str]:
    if not raw_files:
        return []
    resolved_files: list[str] = []
    for raw_file in raw_files:
        resolved = Path(resolve_path(raw_file, workspace))
        if not resolved.is_file():
            raise FileNotFoundError(f"Reference file '{resolved}' was not found.")
        resolved_files.append(str(resolved))
    return resolved_files


def _resolve_mcp_endpoints(
    raw_endpoints: list[str] | None,
    config: AppConfig,
) -> tuple[str, ...]:
    if raw_endpoints is not None:
        candidates = raw_endpoints
    else:
        candidates = list(config.mcp.runtime_endpoints())
    resolved: list[str] = []
    for candidate in candidates:
        if not candidate.strip():
            continue
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _start_and_resume(
    args: argparse.Namespace,
    run_api: RunAPI,
    workflow_id: str,
    step_plan_id: str,
) -> dict[str, Any]:
    run = run_api.start_run(
        workflow_id=workflow_id,
        step_plan_id=step_plan_id,
        run_id=args.run_id,
        approved=args.auto_approve,
    )
    resume_attempt = 0
    while args.auto_approve and run["approval_status"] == "PENDING":
        if resume_attempt >= args.max_resume:
            break
        run = run_api.respond_to_approval(run_id=run["run_id"], approve=True)
        resume_attempt += 1
    if args.auto_approve:
        return run

    if run.get("approval_status") != "PENDING":
        return run
    if not args.interactive_approval:
        return run
    if not sys.stdin.isatty():
        print("[approval] run is pending and stdin is non-interactive. Use API or --auto-approve.")
        return run

    while run.get("approval_status") == "PENDING":
        if resume_attempt >= args.max_resume:
            break
        approve = _prompt_yes_no(run.get("pending_approval"))
        run = run_api.respond_to_approval(run_id=run["run_id"], approve=approve)
        resume_attempt += 1
        if not approve:
            break
    return run


def _prompt_yes_no(pending_approval: Any) -> bool:
    message = "Approval is pending."
    if isinstance(pending_approval, dict):
        detail = pending_approval.get("message")
        if isinstance(detail, str) and detail.strip():
            message = detail

    print(f"[approval] {message}")
    while True:
        raw = input("Approve? (yes/no): ").strip().lower()
        if raw in {"yes", "y"}:
            return True
        if raw in {"no", "n"}:
            return False
        print("Please answer 'yes' or 'no'.")


def _print_plan(step_plan: StepPlan) -> None:
    print("StepPlan:")
    print(
        json.dumps(
            {
                "step_plan_id": step_plan.step_plan_id,
                "version": step_plan.version,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "tool_ref": step.tool_ref,
                        "run": step.run,
                        "skip": step.skip,
                        "requires_approval": step.requires_approval,
                        "resolved_input": step.resolved_input,
                    }
                    for step in step_plan.steps
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _print_result(run: dict[str, Any], warning: str | None) -> None:
    if warning is not None:
        print(f"[safe-llm-warning] {warning}")

    print("RunResult:")
    execution_history = run.get("execution_history", [])
    executed_steps = []
    if isinstance(execution_history, list):
        for item in execution_history:
            if not isinstance(item, dict):
                continue
            executed_steps.append(
                {
                    "step_id": item.get("step_id"),
                    "status": item.get("status"),
                }
            )

    print(
        json.dumps(
            {
                "run_id": run.get("run_id"),
                "approval_status": run.get("approval_status"),
                "current_step_id": run.get("current_step_id"),
                "pending_approval": run.get("pending_approval"),
                "last_error": run.get("last_error"),
                "executed_steps": executed_steps,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _execute_runtime(args: argparse.Namespace, workspace: Path, runtime: AppRuntime) -> int:
    try:
        workflow_id = _resolve_workflow_id(args, runtime, workspace)
        plan = runtime.workflow_api.generate_step_plan(workflow_id)
        rewritten_plan = _rewrite_step_plan_paths(
            runtime.step_plan_repo,
            str(plan["step_plan_id"]),
            workspace,
        )

        if runtime.using_mock:
            _ensure_mock_source_file(rewritten_plan)
        if args.print_plan:
            _print_plan(rewritten_plan)

        run = _start_and_resume(
            args=args,
            run_api=runtime.run_api,
            workflow_id=workflow_id,
            step_plan_id=rewritten_plan.step_plan_id,
        )
        _print_result(run, getattr(runtime.planner, "last_warning", None))

        if run.get("last_error") is not None:
            return 1
        if run.get("approval_status") == "PENDING":
            return 2
        return 0
    finally:
        runtime.close()


def _run(args: argparse.Namespace, config: AppConfig) -> int:
    workspace = config.resolve_workspace(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    snapshots_dir = config.resolve_within_workspace(args.snapshots_dir, workspace)
    workflow_root = config.resolve_within_workspace(args.workflow_root, workspace)
    plan_root = config.resolve_within_workspace(args.plan_root, workspace)
    state_root = config.resolve_within_workspace(args.state_root, workspace)
    audit_root = config.resolve_within_workspace(args.audit_root, workspace)
    llm_tls_ca_bundle = (
        config.resolve_from_config(args.llm_tls_ca_bundle)
        if args.llm_tls_ca_bundle is not None and str(args.llm_tls_ca_bundle).strip()
        else None
    )

    runtime = build_runtime(
        RuntimeConfig(
            workspace=workspace,
            snapshots_dir=snapshots_dir,
            workflow_root=workflow_root,
            plan_root=plan_root,
            state_root=state_root,
            audit_root=audit_root,
            mcp_endpoints=_resolve_mcp_endpoints(args.mcp_endpoint, config),
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
    return _execute_runtime(args, workspace, runtime)


def main(argv: list[str] | None = None) -> int:
    config_path = resolve_config_path(argv)
    config = load_app_config(config_path)
    parser = build_parser(config)
    args = parser.parse_args(argv)
    return _run(args, config)
