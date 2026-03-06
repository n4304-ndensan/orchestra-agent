from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra_agent.adapters import (
    DefaultPolicyEngine,
    FilesystemSnapshotManager,
    FilesystemStepPlanRepository,
    InMemoryAuditLogger,
    JsonFileStepProposalProvider,
    JsonRpcMcpClient,
    LlmPlanner,
    LlmStepProposalProvider,
    MockExcelMcpClient,
    OpenAILlmClient,
    PostgresAgentStateStore,
    SafeAugmentedLlmPlanner,
    XmlWorkflowRepository,
)
from orchestra_agent.adapters.planner import IStepProposalProvider
from orchestra_agent.api import ApprovalAPI, RunAPI, WorkflowAPI
from orchestra_agent.application.use_cases import (
    ApproveStepPlanUseCase,
    CompileStepPlanUseCase,
    CreateWorkflowUseCase,
    ExecutePlanUseCase,
)
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.executor import FailureHandler, PlanExecutor


@dataclass
class CliRuntime:
    workflow_api: WorkflowAPI
    approval_api: ApprovalAPI
    run_api: RunAPI
    workflow_repo: XmlWorkflowRepository
    step_plan_repo: FilesystemStepPlanRepository
    planner: SafeAugmentedLlmPlanner
    mcp_client: JsonRpcMcpClient | MockExcelMcpClient
    llm_client: OpenAILlmClient | None
    using_mock: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run orchestra-agent Excel workflow from a single prompt."
    )
    parser.add_argument(
        "objective",
        nargs="?",
        help="High-level objective text, e.g. sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    parser.add_argument("--workflow-id", default=None, help="Existing workflow ID to execute")
    parser.add_argument(
        "--workflow-xml",
        default=None,
        help="Path to workflow XML file to import and execute",
    )
    parser.add_argument("--name", default="Excel Automation Workflow", help="Workflow display name")
    parser.add_argument("--run-id", default="run-cli", help="Run identifier")
    parser.add_argument("--workspace", default=".", help="Workspace root for relative file paths")
    parser.add_argument(
        "--workflow-root",
        default="workflow",
        help="Workflow storage root directory",
    )
    parser.add_argument("--plan-root", default="plan", help="StepPlan storage root directory")
    parser.add_argument(
        "--snapshots-dir",
        default=".orchestra_snapshots",
        help="Directory to store filesystem snapshots",
    )
    parser.add_argument("--mcp-endpoint", default=None, help="JSON-RPC MCP endpoint URL")
    parser.add_argument(
        "--llm-provider",
        choices=["none", "file", "openai"],
        default="none",
        help="LLM proposal source for planner augmentation",
    )
    parser.add_argument(
        "--llm-proposal-file",
        default=None,
        help="JSON patch file path when --llm-provider file",
    )
    parser.add_argument(
        "--llm-openai-model",
        default="gpt-4.1-mini",
        help="OpenAI model name when --llm-provider openai",
    )
    parser.add_argument(
        "--llm-openai-api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable containing OpenAI API key",
    )
    parser.add_argument(
        "--llm-openai-base-url",
        default="https://api.openai.com",
        help="OpenAI API base URL",
    )
    parser.add_argument(
        "--llm-openai-timeout",
        type=float,
        default=60.0,
        help="OpenAI request timeout seconds",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=0.0,
        help="Sampling temperature used for live LLM proposal",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=1200,
        help="Max tokens for live LLM proposal response",
    )
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically approve and resume pending approvals",
    )
    parser.add_argument(
        "--max-resume",
        type=int,
        default=50,
        help="Maximum auto-resume attempts when approval becomes pending",
    )
    parser.add_argument(
        "--print-plan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print generated step plan summary",
    )
    return parser


def _resolve_path(value: str, workspace: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((workspace / path).resolve())


def _resolve_file_arg(value: str, workspace: Path) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw
    if raw.exists():
        return raw.resolve()
    return (workspace / raw).resolve()


def _build_llm_provider(
    args: argparse.Namespace,
    workspace: Path,
) -> tuple[IStepProposalProvider | None, OpenAILlmClient | None]:
    if args.llm_provider == "none":
        return None, None

    if args.llm_provider == "file":
        if args.llm_proposal_file is None:
            raise ValueError("--llm-proposal-file is required when --llm-provider file.")
        proposal_path = _resolve_file_arg(args.llm_proposal_file, workspace)
        return JsonFileStepProposalProvider(proposal_path), None

    api_key = os.getenv(args.llm_openai_api_key_env)
    if api_key is None or not api_key.strip():
        raise ValueError(
            f"Environment variable '{args.llm_openai_api_key_env}' is required for OpenAI LLM."
        )
    llm_client = OpenAILlmClient(
        api_key=api_key,
        model=args.llm_openai_model,
        base_url=args.llm_openai_base_url,
        timeout_seconds=args.llm_openai_timeout,
    )
    provider = LlmStepProposalProvider(
        llm_client=llm_client,
        temperature=args.llm_temperature,
        max_tokens=args.llm_max_tokens,
    )
    return provider, llm_client


def _build_runtime(
    args: argparse.Namespace,
    workspace: Path,
    snapshots_dir: Path,
    workflow_root: Path,
    plan_root: Path,
) -> CliRuntime:
    workflow_repo = XmlWorkflowRepository(workflow_root)
    step_plan_repo = FilesystemStepPlanRepository(plan_root)
    state_store = PostgresAgentStateStore()
    audit_logger = InMemoryAuditLogger()
    base_planner = LlmPlanner()

    proposal_provider, llm_client = _build_llm_provider(args, workspace)
    planner = SafeAugmentedLlmPlanner(
        base_planner=base_planner,
        proposal_provider=proposal_provider,
    )
    policy_engine = DefaultPolicyEngine()
    snapshot_manager = FilesystemSnapshotManager(snapshots_dir, workspace_root=workspace)

    endpoint = args.mcp_endpoint
    if isinstance(endpoint, str) and not endpoint.strip():
        endpoint = None
    using_mock = endpoint is None
    if using_mock:
        mcp_client: JsonRpcMcpClient | MockExcelMcpClient = MockExcelMcpClient()
    else:
        assert endpoint is not None
        mcp_client = JsonRpcMcpClient(endpoint=endpoint)

    compile_uc = CompileStepPlanUseCase(planner, policy_engine, step_plan_repo, audit_logger)
    create_workflow_uc = CreateWorkflowUseCase(workflow_repo, audit_logger)
    approve_uc = ApproveStepPlanUseCase(step_plan_repo, audit_logger)
    failure_handler = FailureHandler(
        snapshot_manager=snapshot_manager,
        planner=planner,
        policy_engine=policy_engine,
        step_plan_repository=step_plan_repo,
        audit_logger=audit_logger,
        workflow_repository=workflow_repo,
    )
    executor = PlanExecutor(
        mcp_client=mcp_client,
        state_store=state_store,
        snapshot_manager=snapshot_manager,
        audit_logger=audit_logger,
        failure_handler=failure_handler,
    )
    execute_uc = ExecutePlanUseCase(executor, state_store, audit_logger)

    workflow_api = WorkflowAPI(create_workflow_uc, compile_uc, workflow_repo)
    approval_api = ApprovalAPI(approve_uc, step_plan_repo)
    run_api = RunAPI(execute_uc, workflow_repo, step_plan_repo, state_store)

    return CliRuntime(
        workflow_api=workflow_api,
        approval_api=approval_api,
        run_api=run_api,
        workflow_repo=workflow_repo,
        step_plan_repo=step_plan_repo,
        planner=planner,
        mcp_client=mcp_client,
        llm_client=llm_client,
        using_mock=using_mock,
    )


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
                step.resolved_input[key] = _resolve_path(raw, workspace)
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
    runtime: CliRuntime,
    workspace: Path,
) -> str:
    if args.workflow_xml is not None:
        xml_path = _resolve_file_arg(args.workflow_xml, workspace)
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
            workflow_id=args.workflow_id,
        )
        return str(created["workflow_id"])

    if args.objective is None:
        raise ValueError("Objective is required when workflow is not specified.")

    created = runtime.workflow_api.create_workflow(
        name=args.name,
        objective=args.objective,
    )
    return str(created["workflow_id"])


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
        run = run_api.resume_run(run_id=run["run_id"], approved=True)
        resume_attempt += 1
    return run


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


def _execute_runtime(args: argparse.Namespace, workspace: Path, runtime: CliRuntime) -> int:
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
        if args.auto_approve:
            runtime.approval_api.approve_step_plan(rewritten_plan.step_plan_id)

        run = _start_and_resume(
            args=args,
            run_api=runtime.run_api,
            workflow_id=workflow_id,
            step_plan_id=rewritten_plan.step_plan_id,
        )
        _print_result(run, runtime.planner.last_warning)

        if run.get("last_error") is not None:
            return 1
        if run.get("approval_status") == "PENDING":
            return 2
        return 0
    finally:
        if hasattr(runtime.mcp_client, "close"):
            runtime.mcp_client.close()
        if runtime.llm_client is not None:
            runtime.llm_client.close()


def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    snapshots_dir = Path(args.snapshots_dir)
    if not snapshots_dir.is_absolute():
        snapshots_dir = (workspace / snapshots_dir).resolve()

    workflow_root = Path(args.workflow_root)
    if not workflow_root.is_absolute():
        workflow_root = (workspace / workflow_root).resolve()

    plan_root = Path(args.plan_root)
    if not plan_root.is_absolute():
        plan_root = (workspace / plan_root).resolve()

    runtime = _build_runtime(
        args=args,
        workspace=workspace,
        snapshots_dir=snapshots_dir,
        workflow_root=workflow_root,
        plan_root=plan_root,
    )
    return _execute_runtime(args, workspace, runtime)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)
