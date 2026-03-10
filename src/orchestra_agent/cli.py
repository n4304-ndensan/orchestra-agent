from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestra_agent import __version__
from orchestra_agent.adapters import (
    FilesystemAgentStateStore,
    FilesystemAuditLogger,
    FilesystemStepPlanRepository,
)
from orchestra_agent.api import RunAPI
from orchestra_agent.api.run_api import serialize_run_state
from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.domain.serialization import step_plan_to_dict
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.runtime import (
    AppRuntime,
    RuntimeArtifacts,
    RuntimeConfig,
    build_runtime,
    resolve_file_arg,
    resolve_mcp_endpoints,
    resolve_path,
)
from orchestra_agent.shared.error_handling import classify_exception, human_error_lines
from orchestra_agent.shared.preview import mapping_preview
from orchestra_agent.shared.tool_input_normalization import (
    normalize_step_plan_inputs,
    normalize_tool_input,
)

_CLI_COMMANDS = frozenset({"run", "plan", "resume", "status"})
_TOP_LEVEL_FLAGS = frozenset({"-h", "--help", "--version"})
_DEFAULT_AUDIT_LIMIT = 5


@dataclass(slots=True, frozen=True)
class ResolvedCliPaths:
    workspace: Path
    workflow_root: Path
    plan_root: Path
    snapshots_dir: Path
    state_root: Path
    audit_root: Path

    def artifacts(self) -> RuntimeArtifacts:
        return RuntimeArtifacts(
            workspace_root=self.workspace,
            workflow_root=self.workflow_root,
            plan_root=self.plan_root,
            snapshots_dir=self.snapshots_dir,
            state_root=self.state_root,
            audit_root=self.audit_root,
        )


def build_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    defaults = config or AppConfig()
    parser = argparse.ArgumentParser(
        description="Product CLI for planning, executing, resuming, and inspecting orchestra runs.",
        epilog=_cli_examples(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    config_parent = argparse.ArgumentParser(add_help=False)
    _add_config_argument(config_parent, config)

    runtime_parent = argparse.ArgumentParser(add_help=False)
    _add_config_argument(runtime_parent, config)
    _add_runtime_arguments(runtime_parent, defaults)
    _add_llm_arguments(runtime_parent, defaults)

    workflow_parent = argparse.ArgumentParser(add_help=False)
    _add_workflow_arguments(workflow_parent, defaults)

    execution_parent = argparse.ArgumentParser(add_help=False)
    _add_execution_arguments(execution_parent, defaults)

    output_parent = argparse.ArgumentParser(add_help=False)
    _add_output_arguments(output_parent)

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Create or load a workflow, compile a step plan, and execute it.",
        parents=[runtime_parent, workflow_parent, execution_parent, output_parent],
    )
    run_parser.add_argument(
        "--run-id",
        default=defaults.runtime.run_id,
        help="Run identifier. When omitted, a fresh run ID is generated.",
    )
    run_parser.add_argument(
        "--print-plan",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.print_plan,
        help="Print generated step plan summary.",
    )

    subparsers.add_parser(
        "plan",
        help="Create or load a workflow and compile a step plan without execution.",
        parents=[runtime_parent, workflow_parent, output_parent],
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a pending run, optionally approving, rejecting, or applying feedback.",
        parents=[runtime_parent, execution_parent, output_parent],
    )
    resume_parser.add_argument("run_id", help="Existing run ID to resume.")
    resume_parser.add_argument(
        "--print-plan",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.print_plan,
        help="Print the latest step plan when feedback generates a new plan.",
    )
    resume_action_group = resume_parser.add_mutually_exclusive_group()
    resume_action_group.add_argument(
        "--reject",
        action="store_true",
        help="Reject the pending approval instead of resuming it.",
    )
    resume_action_group.add_argument(
        "--feedback",
        default=None,
        help="Submit feedback and trigger re-planning before resuming.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Inspect a saved run state without requiring live LLM or MCP credentials.",
        parents=[config_parent, output_parent],
    )
    _add_workspace_storage_arguments(status_parser, defaults)
    status_parser.add_argument("run_id", help="Existing run ID to inspect.")
    status_parser.add_argument(
        "--audit-limit",
        type=int,
        default=_DEFAULT_AUDIT_LIMIT,
        help="How many recent audit events to show. Use 0 to disable.",
    )
    return parser


def _add_config_argument(parser: argparse.ArgumentParser, config: AppConfig | None) -> None:
    parser.add_argument(
        "--config",
        default=str(config.source_path) if config and config.source_path is not None else None,
        help="Path to orchestra-agent TOML config file.",
    )


def _add_workflow_arguments(parser: argparse.ArgumentParser, defaults: AppConfig) -> None:
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
    parser.add_argument("--workflow-id", default=None, help="Existing workflow ID to execute.")
    parser.add_argument(
        "--workflow-xml",
        default=None,
        help="Path to workflow XML file to import and execute.",
    )
    parser.add_argument(
        "--name",
        default=defaults.runtime.workflow_name,
        help="Workflow display name.",
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser, defaults: AppConfig) -> None:
    _add_workspace_storage_arguments(parser, defaults)
    parser.add_argument(
        "--mcp-endpoint",
        action="append",
        default=None,
        help="JSON-RPC MCP endpoint URL. Repeat to aggregate multiple MCP servers.",
    )
    parser.add_argument(
        "--repair-max-attempts",
        type=int,
        default=defaults.runtime.repair_max_attempts,
        help="Maximum failure/feedback-driven replans before the run is rejected.",
    )


def _add_workspace_storage_arguments(parser: argparse.ArgumentParser, defaults: AppConfig) -> None:
    parser.add_argument(
        "--workspace",
        default=defaults.workspace.root,
        help="Workspace root for relative file paths.",
    )
    parser.add_argument(
        "--workflow-root",
        default=defaults.workspace.workflow_root,
        help="Workflow storage root directory.",
    )
    parser.add_argument(
        "--plan-root",
        default=defaults.workspace.plan_root,
        help="Step plan storage root directory.",
    )
    parser.add_argument(
        "--snapshots-dir",
        default=defaults.workspace.snapshots_dir,
        help="Directory to store filesystem snapshots.",
    )
    parser.add_argument(
        "--state-root",
        default=defaults.workspace.state_root,
        help="Directory to store persistent run state JSON files.",
    )
    parser.add_argument(
        "--audit-root",
        default=defaults.workspace.audit_root,
        help="Directory to store persistent audit events.",
    )


def _add_llm_arguments(parser: argparse.ArgumentParser, defaults: AppConfig) -> None:
    parser.add_argument(
        "--llm-provider",
        choices=["none", "file", "openai", "google"],
        default=defaults.llm.provider,
        help="LLM proposal source for planner augmentation.",
    )
    parser.add_argument(
        "--llm-proposal-file",
        default=defaults.llm.proposal_file,
        help="JSON patch file path when --llm-provider file.",
    )
    parser.add_argument(
        "--llm-openai-model",
        default=defaults.llm.openai_model,
        help="OpenAI model name when --llm-provider openai.",
    )
    parser.add_argument(
        "--llm-openai-api-key-env",
        default=defaults.llm.openai_api_key_env,
        help="Environment variable containing OpenAI API key.",
    )
    parser.add_argument(
        "--llm-openai-base-url",
        default=defaults.llm.openai_base_url,
        help="OpenAI API base URL.",
    )
    parser.add_argument(
        "--llm-openai-timeout",
        type=float,
        default=defaults.llm.openai_timeout,
        help="OpenAI request timeout seconds.",
    )
    parser.add_argument(
        "--llm-google-model",
        default=defaults.llm.google_model,
        help="Google Gemini model name when --llm-provider google.",
    )
    parser.add_argument(
        "--llm-google-api-key-env",
        default=defaults.llm.google_api_key_env,
        help="Primary environment variable containing Google Gemini API key.",
    )
    parser.add_argument(
        "--llm-google-base-url",
        default=defaults.llm.google_base_url,
        help="Google Gemini Developer API base URL.",
    )
    parser.add_argument(
        "--llm-google-timeout",
        type=float,
        default=defaults.llm.google_timeout,
        help="Google Gemini request timeout seconds.",
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
        help="Planner mode override.",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=defaults.llm.temperature,
        help="Sampling temperature used for live LLM proposal.",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=defaults.llm.max_tokens,
        help="Max tokens for live LLM proposal response.",
    )


def _add_execution_arguments(parser: argparse.ArgumentParser, defaults: AppConfig) -> None:
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.auto_approve,
        help="Automatically approve and resume pending approvals.",
    )
    parser.add_argument(
        "--interactive-approval",
        action=argparse.BooleanOptionalAction,
        default=defaults.runtime.interactive_approval,
        help="When auto-approve is off, ask approval decisions in yes/no/feedback format.",
    )
    parser.add_argument(
        "--max-resume",
        type=int,
        default=defaults.runtime.max_resume,
        help="Maximum resume attempts when approval becomes pending.",
    )


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of human-readable CLI output.",
    )


def _rewrite_step_plan_paths(
    step_plan_repo: FilesystemStepPlanRepository,
    step_plan_id: str,
    workspace: Path,
) -> StepPlan:
    step_plan = step_plan_repo.get(step_plan_id)
    if step_plan is None:
        raise KeyError(f"StepPlan '{step_plan_id}' not found.")
    normalize_step_plan_inputs(step_plan)
    for step in step_plan.steps:
        if step.tool_ref.startswith("orchestra."):
            continue
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


def _start_and_resume(
    args: argparse.Namespace,
    workspace: Path,
    runtime: AppRuntime,
    run_api: RunAPI,
    workflow_id: str,
    step_plan_id: str,
) -> dict[str, Any]:
    run_id = _resolve_run_id(args.run_id)
    run = run_api.start_run(
        workflow_id=workflow_id,
        step_plan_id=step_plan_id,
        run_id=run_id,
        approved=args.auto_approve,
    )
    run, resume_attempt = _resume_auto_approvals(args, run_api, run)
    if args.auto_approve:
        return run

    return _resume_interactive_approvals(
        args=args,
        workspace=workspace,
        runtime=runtime,
        run_api=run_api,
        run=run,
        resume_attempt=resume_attempt,
    )


def _resume_existing_run(
    args: argparse.Namespace,
    workspace: Path,
    runtime: AppRuntime,
    run_api: RunAPI,
) -> dict[str, Any]:
    if args.feedback is not None:
        existing_run = run_api.get_run(args.run_id)
        run = _apply_feedback_and_resume(
            workspace=workspace,
            runtime=runtime,
            run_api=run_api,
            run=existing_run,
            feedback=args.feedback,
        )
        _maybe_print_updated_plan(args, runtime, run)
    elif args.reject:
        return run_api.respond_to_approval(run_id=args.run_id, approve=False)
    else:
        run = run_api.respond_to_approval(run_id=args.run_id, approve=True)

    run, resume_attempt = _resume_auto_approvals(args, run_api, run)
    if args.auto_approve:
        return run
    return _resume_interactive_approvals(
        args=args,
        workspace=workspace,
        runtime=runtime,
        run_api=run_api,
        run=run,
        resume_attempt=resume_attempt,
    )


def _resolve_run_id(explicit_run_id: str | None) -> str:
    if isinstance(explicit_run_id, str) and explicit_run_id.strip():
        return explicit_run_id.strip()
    return f"run-{uuid4().hex[:10]}"


def _resume_auto_approvals(
    args: argparse.Namespace,
    run_api: RunAPI,
    run: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    resume_attempt = 0
    while args.auto_approve and run["approval_status"] == "PENDING":
        if resume_attempt >= args.max_resume:
            break
        run = run_api.respond_to_approval(run_id=run["run_id"], approve=True)
        resume_attempt += 1
    return run, resume_attempt


def _resume_interactive_approvals(
    *,
    args: argparse.Namespace,
    workspace: Path,
    runtime: AppRuntime,
    run_api: RunAPI,
    run: dict[str, Any],
    resume_attempt: int,
) -> dict[str, Any]:
    if not args.interactive_approval or not _run_requires_terminal_action(run):
        return run
    if not sys.stdin.isatty():
        print(
            "[approval] run is waiting for approval/feedback and stdin is non-interactive. "
            "Use `resume` or enable --auto-approve."
        )
        return run

    while resume_attempt < args.max_resume and _run_requires_terminal_action(run):
        action, feedback = _prompt_approval_action(run)
        if action == "feedback":
            run = _apply_feedback_and_resume(
                workspace=workspace,
                runtime=runtime,
                run_api=run_api,
                run=run,
                feedback=feedback,
            )
            _maybe_print_updated_plan(args, runtime, run)
            resume_attempt += 1
            continue
        run = run_api.respond_to_approval(run_id=run["run_id"], approve=(action == "approve"))
        resume_attempt += 1
        if action == "reject":
            break
    return run


def _apply_feedback_and_resume(
    *,
    workspace: Path,
    runtime: AppRuntime,
    run_api: RunAPI,
    run: dict[str, Any],
    feedback: str | None,
) -> dict[str, Any]:
    assert feedback is not None
    previous_run = run
    run = run_api.respond_to_approval(run_id=run["run_id"], feedback=feedback)
    if isinstance(run.get("step_plan_id"), str):
        _rewrite_step_plan_paths(
            runtime.step_plan_repo,
            str(run["step_plan_id"]),
            workspace,
        )
    _print_feedback_replan_summary(previous_run, run, runtime)
    return run


def _prompt_approval_action(run: dict[str, Any]) -> tuple[str, str | None]:
    pending_approval = run.get("pending_approval")
    if isinstance(pending_approval, dict):
        message = "Approval is pending."
        detail = pending_approval.get("message")
        if isinstance(detail, str) and detail.strip():
            message = detail
        _print_approval_preview(message, pending_approval)
        return _prompt_choice(
            prompt="Decision? (yes/no/feedback): ",
            retry_aliases={"yes", "y"},
        )

    last_error = run.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        _print_failure_preview(last_error)
        return _prompt_choice(
            prompt="Action? (retry/no/feedback): ",
            retry_aliases={"retry", "r", "yes", "y"},
        )

    _print_approval_preview("Approval is pending.", pending_approval)
    return _prompt_choice(
        prompt="Decision? (yes/no/feedback): ",
        retry_aliases={"yes", "y"},
    )


def _prompt_choice(prompt: str, retry_aliases: set[str]) -> tuple[str, str | None]:
    while True:
        raw = input(prompt).strip()
        normalized = raw.lower()
        if normalized in retry_aliases:
            return "approve", None
        if normalized in {"no", "n"}:
            return "reject", None
        inline_feedback = _parse_feedback_input(raw)
        if inline_feedback is not None:
            if inline_feedback:
                return "feedback", inline_feedback
            feedback = input("Feedback message: ").strip()
            if feedback:
                return "feedback", feedback
            print("Feedback message cannot be empty.")
            continue
        print("Please answer 'yes', 'no', or 'feedback'.")


def _parse_feedback_input(raw: str) -> str | None:
    stripped = raw.strip()
    normalized = stripped.lower()
    if normalized in {"feedback", "f"}:
        return ""
    for prefix in ("feedback ", "f "):
        if normalized.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def _run_requires_terminal_action(run: dict[str, Any]) -> bool:
    if run.get("approval_status") == "PENDING":
        return True
    last_error = run.get("last_error")
    return isinstance(last_error, str) and bool(last_error.strip())


def _print_approval_preview(message: str, pending_approval: Any) -> None:
    stage = pending_approval.get("stage") if isinstance(pending_approval, dict) else None
    title = {
        "PLAN": "Plan review",
        "PRE_STEP": "Step approval",
        "POST_STEP": "Step result review",
    }.get(stage, "Approval required")
    print(f"[approval] {title}")
    print(f"  {message}")
    if not isinstance(pending_approval, dict):
        return
    details = pending_approval.get("details")
    if not isinstance(details, list):
        return
    for raw_line in details:
        if not isinstance(raw_line, str):
            continue
        line = raw_line.strip()
        if line:
            print(f"  {line}")


def _print_failure_preview(error_message: str) -> None:
    print("[failure] Run failed")
    print(f"  error     {error_message}")
    hint = _failure_hint(error_message)
    if hint is not None:
        print(f"  hint      {hint}")
    print("  next      retry / no / feedback")


def _failure_hint(error_message: str) -> str | None:
    if "429 Too Many Requests" in error_message:
        return "LLM rate limit hit. Retry later or switch to --llm-provider none."
    if "not valid JSON" in error_message:
        return "LLM returned malformed JSON. Retry is safe; fallback may be needed."
    return None


def _print_plan(step_plan: StepPlan) -> None:
    print("Step Plan")
    print(f"  id        {step_plan.step_plan_id}")
    print(f"  version   {step_plan.version}")
    print(f"  steps     {len(step_plan.steps)}")

    for index, step in enumerate(step_plan.ordered_steps(), start=1):
        normalized_input = normalize_tool_input(step.tool_ref, step.resolved_input)
        print()
        print(f"  {index:02d}. {step.name or step.step_id}")
        print(f"      step      {step.step_id}")
        print(f"      tool      {step.tool_ref}")
        if step.description.strip():
            print(f"      what      {step.description.strip()}")
        input_preview = mapping_preview(normalized_input)
        if input_preview:
            print(f"      input     {input_preview}")
        print(f"      review    {'yes' if step.requires_runtime_approval else 'no'}")


def _print_plan_ready(
    workflow: Workflow,
    step_plan: StepPlan,
    approval_status: str,
    reasons: list[str],
) -> None:
    print("Plan Ready")
    print(f"  workflow  {workflow.workflow_id} v{workflow.version}")
    print(f"  step plan {step_plan.step_plan_id} v{step_plan.version}")
    print(f"  approval  {approval_status}")
    if reasons:
        print(f"  reasons   {'; '.join(reasons)}")


def _print_feedback_replan_summary(
    previous_run: dict[str, Any],
    updated_run: dict[str, Any],
    runtime: AppRuntime,
) -> None:
    workflow_id = updated_run.get("workflow_id")
    workflow_version = updated_run.get("workflow_version")
    step_plan_id = updated_run.get("step_plan_id")
    step_plan_version = updated_run.get("step_plan_version")
    if not all(
        (
            isinstance(workflow_id, str),
            isinstance(workflow_version, int),
            isinstance(step_plan_id, str),
            isinstance(step_plan_version, int),
        )
    ):
        return

    workflow_changed = (
        updated_run.get("workflow_version") != previous_run.get("workflow_version")
        or updated_run.get("workflow_id") != previous_run.get("workflow_id")
    )
    step_plan_changed = (
        updated_run.get("step_plan_id") != previous_run.get("step_plan_id")
        or updated_run.get("step_plan_version") != previous_run.get("step_plan_version")
    )
    target = _feedback_target_label(previous_run, updated_run)
    summary = "replanned" if workflow_changed or step_plan_changed else "updated"
    approval_status = updated_run.get("approval_status")
    if approval_status == "PENDING":
        outcome = "approval pending"
    elif approval_status == "APPROVED":
        outcome = "approved"
    elif approval_status == "REJECTED":
        outcome = "rejected"
    else:
        outcome = "state changed"

    print(f"[feedback] {target} -> {summary}, {outcome}")
    print(f"  workflow  {runtime.artifacts.workflow_path(workflow_id, workflow_version)}")
    print(
        "  step plan "
        f"{runtime.artifacts.step_plan_json_path(workflow_id, step_plan_id, step_plan_version)}"
    )


def _feedback_target_label(previous_run: dict[str, Any], updated_run: dict[str, Any]) -> str:
    updated_metadata = updated_run.get("metadata")
    if isinstance(updated_metadata, dict):
        step_id = updated_metadata.get("feedback_step_id")
        if isinstance(step_id, str) and step_id.strip():
            return step_id

    pending_approval = previous_run.get("pending_approval")
    if isinstance(pending_approval, dict):
        step_id = pending_approval.get("step_id")
        if step_id == "__plan__":
            return "plan"
        if isinstance(step_id, str) and step_id.strip():
            return step_id
    return "plan"


def _print_result(run: dict[str, Any], warning: str | None) -> None:
    if warning is not None:
        print(f"[safe-llm-warning] {warning}")

    print("Run Result")
    print(f"  run id    {run.get('run_id')}")
    for line in _run_identity_lines(run):
        print(line)
    for line in _run_status_lines(run):
        print(line)
    executed_steps = _executed_step_summary(run)
    if executed_steps:
        print("  steps")
        for item in executed_steps:
            print(f"    {item.get('step_id')}  {item.get('status')}")


def _run_identity_lines(run: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    workflow_id = run.get("workflow_id")
    workflow_version = run.get("workflow_version")
    if isinstance(workflow_id, str) and isinstance(workflow_version, int):
        lines.append(f"  workflow  {workflow_id} v{workflow_version}")
    step_plan_id = run.get("step_plan_id")
    step_plan_version = run.get("step_plan_version")
    if isinstance(step_plan_id, str) and isinstance(step_plan_version, int):
        lines.append(f"  step plan {step_plan_id} v{step_plan_version}")
    return lines


def _run_status_lines(run: dict[str, Any]) -> list[str]:
    lines = [f"  status    {run.get('approval_status')}"]
    current_step_id = run.get("current_step_id")
    if isinstance(current_step_id, str) and current_step_id.strip():
        lines.append(f"  current   {current_step_id}")
    pending_approval = run.get("pending_approval")
    if isinstance(pending_approval, dict):
        lines.append(
            f"  waiting   {pending_approval.get('stage')} / {pending_approval.get('step_id')}"
        )
    metadata = run.get("metadata")
    if isinstance(metadata, dict) and metadata.get("artifacts_locked") is True:
        lines.append("  locked    yes")
    last_error = run.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        lines.append(f"  error     {last_error}")
    if run.get("approval_status") == "PENDING" and isinstance(run.get("run_id"), str):
        lines.append(f"  next      orchestra-agent resume {run['run_id']}")
    return lines


def _executed_step_summary(run: dict[str, Any]) -> list[dict[str, Any]]:
    execution_history = run.get("execution_history", [])
    if not isinstance(execution_history, list):
        return []
    executed_steps = []
    for item in execution_history:
        if not isinstance(item, dict):
            continue
        executed_steps.append(
            {
                "step_id": item.get("step_id"),
                "status": item.get("status"),
            }
        )
    return executed_steps


def _print_artifacts(artifact_paths: dict[str, str]) -> None:
    if not artifact_paths:
        return
    print("Artifacts")
    labels = {
        "workflow": "workflow",
        "step_plan": "step plan",
        "run_state": "run state",
        "audit_log": "audit",
    }
    for key in ("workflow", "step_plan", "run_state", "audit_log"):
        path = artifact_paths.get(key)
        if path is None:
            continue
        print(f"  {labels[key]:<9} {path}")


def _print_audit_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    print("Recent Audit")
    for event in events:
        event_type = event.get("event_type")
        timestamp = event.get("timestamp")
        step_id = event.get("step_id")
        suffix = f" / {step_id}" if isinstance(step_id, str) and step_id.strip() else ""
        print(f"  {timestamp}  {event_type}{suffix}")


def _maybe_print_updated_plan(
    args: argparse.Namespace,
    runtime: AppRuntime,
    run: dict[str, Any],
) -> None:
    if not getattr(args, "print_plan", False) or getattr(args, "json", False):
        return
    step_plan_id = run.get("step_plan_id")
    if not isinstance(step_plan_id, str):
        return
    step_plan = runtime.step_plan_repo.get(step_plan_id)
    if step_plan is None:
        return
    print()
    _print_plan(step_plan)


def _prepare_plan(
    args: argparse.Namespace,
    workspace: Path,
    runtime: AppRuntime,
) -> tuple[Workflow, StepPlan, dict[str, Any]]:
    workflow_id = _resolve_workflow_id(args, runtime, workspace)
    workflow = runtime.workflow_repo.get(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found.")
    plan_payload = runtime.workflow_api.generate_step_plan(workflow_id)
    rewritten_plan = _rewrite_step_plan_paths(
        runtime.step_plan_repo,
        str(plan_payload["step_plan_id"]),
        workspace,
    )
    return workflow, rewritten_plan, plan_payload


def _resolve_cli_paths(args: argparse.Namespace, config: AppConfig) -> ResolvedCliPaths:
    workspace = config.resolve_workspace(args.workspace)
    return ResolvedCliPaths(
        workspace=workspace,
        workflow_root=config.resolve_within_workspace(args.workflow_root, workspace),
        plan_root=config.resolve_within_workspace(args.plan_root, workspace),
        snapshots_dir=config.resolve_within_workspace(args.snapshots_dir, workspace),
        state_root=config.resolve_within_workspace(args.state_root, workspace),
        audit_root=config.resolve_within_workspace(args.audit_root, workspace),
    )


def _build_runtime_from_args(
    args: argparse.Namespace,
    config: AppConfig,
) -> tuple[ResolvedCliPaths, AppRuntime]:
    paths = _resolve_cli_paths(args, config)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    llm_tls_ca_bundle = (
        config.resolve_from_config(args.llm_tls_ca_bundle)
        if args.llm_tls_ca_bundle is not None and str(args.llm_tls_ca_bundle).strip()
        else None
    )
    runtime = build_runtime(
        RuntimeConfig(
            workspace=paths.workspace,
            snapshots_dir=paths.snapshots_dir,
            workflow_root=paths.workflow_root,
            plan_root=paths.plan_root,
            state_root=paths.state_root,
            audit_root=paths.audit_root,
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
    return paths, runtime


def _artifact_paths(
    artifacts: RuntimeArtifacts,
    *,
    run_id: str | None = None,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
    step_plan_id: str | None = None,
    step_plan_version: int | None = None,
) -> dict[str, str]:
    payload: dict[str, str] = {"audit_log": str(artifacts.audit_root / "events.ndjson")}
    if isinstance(workflow_id, str):
        payload["workflow"] = str(artifacts.workflow_path(workflow_id, workflow_version))
    if isinstance(workflow_id, str) and isinstance(step_plan_id, str):
        payload["step_plan"] = str(
            artifacts.step_plan_json_path(workflow_id, step_plan_id, step_plan_version)
        )
    if isinstance(run_id, str):
        payload["run_state"] = str(artifacts.state_root / f"{run_id}.json")
    return payload


def _artifact_paths_for_run(
    artifacts: RuntimeArtifacts,
    run: dict[str, Any],
) -> dict[str, str]:
    return _artifact_paths(
        artifacts,
        run_id=str(run.get("run_id")) if isinstance(run.get("run_id"), str) else None,
        workflow_id=run.get("workflow_id") if isinstance(run.get("workflow_id"), str) else None,
        workflow_version=(
            run.get("workflow_version") if isinstance(run.get("workflow_version"), int) else None
        ),
        step_plan_id=run.get("step_plan_id") if isinstance(run.get("step_plan_id"), str) else None,
        step_plan_version=(
            run.get("step_plan_version")
            if isinstance(run.get("step_plan_version"), int)
            else None
        ),
    )


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _emit_cli_error(
    report: Any,
    *,
    json_output: bool,
) -> None:
    if json_output:
        _emit_json({"command": "error", "error": report.to_dict()})
        return
    for line in human_error_lines(report):
        print(line, file=sys.stderr)


def _wants_json_output(argv: list[str]) -> bool:
    return "--json" in argv


def _plan_reasons(plan_payload: dict[str, Any]) -> list[str]:
    return [
        str(reason)
        for reason in plan_payload.get("reasons", [])
        if isinstance(reason, str)
    ]


def _execute_run_command(args: argparse.Namespace, config: AppConfig) -> int:
    paths, runtime = _build_runtime_from_args(args, config)
    try:
        workflow, rewritten_plan, plan_payload = _prepare_plan(args, paths.workspace, runtime)
        if runtime.using_mock:
            _ensure_mock_source_file(rewritten_plan)

        plan_reasons = _plan_reasons(plan_payload)
        plan_warning = getattr(runtime.planner, "last_warning", None)
        artifact_payload = _artifact_paths(
            runtime.artifacts,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            step_plan_id=rewritten_plan.step_plan_id,
            step_plan_version=rewritten_plan.version,
        )

        displayed_plan_warning = False
        if plan_warning is not None and not args.json:
            print(f"[safe-llm-warning] {plan_warning}")
            displayed_plan_warning = True

        if args.print_plan and not args.json:
            _print_plan_ready(
                workflow,
                rewritten_plan,
                str(plan_payload.get("approval_status", "UNKNOWN")),
                plan_reasons,
            )
            _print_artifacts(artifact_payload)
            print()
            _print_plan(rewritten_plan)
            print()

        run = _start_and_resume(
            args=args,
            workspace=paths.workspace,
            runtime=runtime,
            run_api=runtime.run_api,
            workflow_id=workflow.workflow_id,
            step_plan_id=rewritten_plan.step_plan_id,
        )
        warning = getattr(runtime.planner, "last_warning", None)
        final_artifacts = _artifact_paths_for_run(runtime.artifacts, run)
        result_warning = None if displayed_plan_warning and warning == plan_warning else warning

        if args.json:
            _emit_json(
                {
                    "command": "run",
                    "workflow": {
                        "workflow_id": workflow.workflow_id,
                        "version": workflow.version,
                    },
                    "step_plan": {
                        "step_plan_id": rewritten_plan.step_plan_id,
                        "version": rewritten_plan.version,
                        "approval_status": plan_payload.get("approval_status"),
                        "reasons": plan_reasons,
                    },
                    "artifacts": final_artifacts,
                    "run": run,
                    "planning_warning": plan_warning,
                    "warning": warning,
                }
            )
        else:
            _print_result(run, result_warning)
            _print_artifacts(final_artifacts)

        if run.get("last_error") is not None:
            return 1
        if run.get("approval_status") == "PENDING":
            return 2
        return 0
    finally:
        runtime.close()


def _execute_plan_command(args: argparse.Namespace, config: AppConfig) -> int:
    paths, runtime = _build_runtime_from_args(args, config)
    try:
        workflow, rewritten_plan, plan_payload = _prepare_plan(args, paths.workspace, runtime)
        plan_reasons = _plan_reasons(plan_payload)
        plan_warning = getattr(runtime.planner, "last_warning", None)
        artifact_payload = _artifact_paths(
            runtime.artifacts,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            step_plan_id=rewritten_plan.step_plan_id,
            step_plan_version=rewritten_plan.version,
        )
        if args.json:
            _emit_json(
                {
                    "command": "plan",
                    "workflow": {
                        "workflow_id": workflow.workflow_id,
                        "version": workflow.version,
                    },
                    "step_plan": {
                        **step_plan_to_dict(rewritten_plan),
                        "approval_status": plan_payload.get("approval_status"),
                        "reasons": plan_reasons,
                    },
                    "artifacts": artifact_payload,
                    "warning": plan_warning,
                }
            )
            return 0

        if plan_warning is not None:
            print(f"[safe-llm-warning] {plan_warning}")
        _print_plan_ready(
            workflow,
            rewritten_plan,
            str(plan_payload.get("approval_status", "UNKNOWN")),
            plan_reasons,
        )
        _print_artifacts(artifact_payload)
        print()
        _print_plan(rewritten_plan)
        return 0
    finally:
        runtime.close()


def _execute_resume_command(args: argparse.Namespace, config: AppConfig) -> int:
    paths, runtime = _build_runtime_from_args(args, config)
    try:
        run = _resume_existing_run(args, paths.workspace, runtime, runtime.run_api)
        warning = getattr(runtime.planner, "last_warning", None)
        artifact_payload = _artifact_paths_for_run(runtime.artifacts, run)

        if args.json:
            _emit_json(
                {
                    "command": "resume",
                    "artifacts": artifact_payload,
                    "run": run,
                    "warning": warning,
                }
            )
        else:
            _print_result(run, warning)
            _print_artifacts(artifact_payload)

        if run.get("last_error") is not None:
            return 1
        if run.get("approval_status") == "PENDING":
            return 2
        return 0
    finally:
        runtime.close()


def _execute_status_command(args: argparse.Namespace, config: AppConfig) -> int:
    paths = _resolve_cli_paths(args, config)
    state_path = paths.state_root / f"{args.run_id}.json"
    if not state_path.is_file():
        raise KeyError(f"Run '{args.run_id}' not found.")

    state = FilesystemAgentStateStore(paths.state_root).load(args.run_id)
    if state is None:
        raise KeyError(f"Run '{args.run_id}' not found.")
    run = serialize_run_state(state)

    audit_events: list[dict[str, Any]] = []
    if args.audit_limit > 0 and paths.audit_root.is_dir():
        audit_events = FilesystemAuditLogger(paths.audit_root).list_events(
            run_id=args.run_id,
            limit=args.audit_limit,
        )

    artifact_payload = _artifact_paths_for_run(paths.artifacts(), run)

    if args.json:
        _emit_json(
            {
                "command": "status",
                "artifacts": artifact_payload,
                "audit_events": audit_events,
                "run": run,
            }
        )
        return 0

    _print_result(run, warning=None)
    _print_artifacts(artifact_payload)
    if audit_events:
        _print_audit_events(audit_events)
    return 0


def _dispatch_command(args: argparse.Namespace, config: AppConfig) -> int:
    if args.command == "run":
        return _execute_run_command(args, config)
    if args.command == "plan":
        return _execute_plan_command(args, config)
    if args.command == "resume":
        return _execute_resume_command(args, config)
    if args.command == "status":
        return _execute_status_command(args, config)
    raise ValueError(f"Unsupported command '{args.command}'.")


def _normalize_cli_argv(argv: list[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        return raw
    if raw[0] in _TOP_LEVEL_FLAGS:
        return raw
    if raw[0] in _CLI_COMMANDS:
        return raw
    return ["run", *raw]


def _cli_examples() -> str:
    return "\n".join(
        [
            "Examples:",
            '  orchestra-agent run "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存"',
            '  orchestra-agent plan "output/HelloWorld.xlsx を作成し、Sheet1 の A1 に HelloWorld"',
            "  orchestra-agent status run-hello",
            "  orchestra-agent resume run-hello --auto-approve",
            '  orchestra-agent resume run-hello --feedback "save_file の output を修正して"',
            "",
            "Legacy compatibility:",
            '  orchestra-agent "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存"',
        ]
    )


def main(argv: list[str] | None = None) -> int:
    normalized_argv = _normalize_cli_argv(argv)
    json_output = _wants_json_output(normalized_argv)
    try:
        config_path = resolve_config_path(normalized_argv)
        config = load_app_config(config_path)
        parser = build_parser(config)
        args = parser.parse_args(normalized_argv)
        if args.command is None:
            parser.print_help()
            return 2
        return _dispatch_command(args, config)
    except KeyboardInterrupt as exc:
        report = classify_exception(exc)
        _emit_cli_error(report, json_output=json_output)
        return report.exit_code
    except Exception as exc:  # noqa: BLE001
        report = classify_exception(exc)
        _emit_cli_error(report, json_output=json_output)
        return report.exit_code
