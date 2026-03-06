from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from orchestra_agent.adapters import (
    DefaultPolicyEngine,
    FilesystemSnapshotManager,
    InMemoryAuditLogger,
    InMemoryStepPlanRepository,
    InMemoryWorkflowRepository,
    JsonFileStepProposalProvider,
    JsonRpcMcpClient,
    LlmPlanner,
    MockExcelMcpClient,
    PostgresAgentStateStore,
    SafeAugmentedLlmPlanner,
)
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
    step_plan_repo: InMemoryStepPlanRepository
    planner: SafeAugmentedLlmPlanner
    mcp_client: JsonRpcMcpClient | MockExcelMcpClient
    using_mock: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run orchestra-agent Excel workflow from a single prompt."
    )
    parser.add_argument(
        "objective",
        help="High-level objective text, e.g. sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    parser.add_argument("--name", default="Excel Automation Workflow", help="Workflow display name")
    parser.add_argument("--run-id", default="run-cli", help="Run identifier")
    parser.add_argument("--workspace", default=".", help="Workspace root for relative file paths")
    parser.add_argument(
        "--snapshots-dir",
        default=".orchestra_snapshots",
        help="Directory to store filesystem snapshots",
    )
    parser.add_argument("--mcp-endpoint", default=None, help="JSON-RPC MCP endpoint URL")
    parser.add_argument(
        "--llm-proposal-file",
        default=None,
        help="Optional JSON patch file for safe LLM augmentation",
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
        default=3,
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


def _rewrite_step_plan_paths(
    step_plan_repo: InMemoryStepPlanRepository,
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


def _build_runtime(args: argparse.Namespace, workspace: Path, snapshots_dir: Path) -> CliRuntime:
    workflow_repo = InMemoryWorkflowRepository()
    step_plan_repo = InMemoryStepPlanRepository()
    state_store = PostgresAgentStateStore()
    audit_logger = InMemoryAuditLogger()
    base_planner = LlmPlanner()

    proposal_provider = None
    if args.llm_proposal_file is not None:
        proposal_path = Path(args.llm_proposal_file)
        if not proposal_path.is_absolute():
            proposal_path = (workspace / proposal_path).resolve()
        proposal_provider = JsonFileStepProposalProvider(proposal_path)

    planner = SafeAugmentedLlmPlanner(
        base_planner=base_planner,
        proposal_provider=proposal_provider,
    )
    policy_engine = DefaultPolicyEngine()
    snapshot_manager = FilesystemSnapshotManager(snapshots_dir, workspace_root=workspace)

    using_mock = args.mcp_endpoint is None
    if using_mock:
        mcp_client: JsonRpcMcpClient | MockExcelMcpClient = MockExcelMcpClient()
    else:
        mcp_client = JsonRpcMcpClient(endpoint=args.mcp_endpoint)

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
        step_plan_repo=step_plan_repo,
        planner=planner,
        mcp_client=mcp_client,
        using_mock=using_mock,
    )


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


def _print_result(run: dict[str, object], warning: str | None) -> None:
    if warning is not None:
        print(f"[safe-llm-warning] {warning}")

    print("RunResult:")
    execution_history = run.get("execution_history", [])
    if not isinstance(execution_history, list):
        execution_history = []
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

    print(
        json.dumps(
            {
                "run_id": run.get("run_id"),
                "approval_status": run.get("approval_status"),
                "current_step_id": run.get("current_step_id"),
                "last_error": run.get("last_error"),
                "executed_steps": executed_steps,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    snapshots_dir = Path(args.snapshots_dir)
    if not snapshots_dir.is_absolute():
        snapshots_dir = (workspace / snapshots_dir).resolve()

    runtime = _build_runtime(args, workspace, snapshots_dir)
    try:
        created = runtime.workflow_api.create_workflow(name=args.name, objective=args.objective)
        plan = runtime.workflow_api.generate_step_plan(created["workflow_id"])
        rewritten_plan = _rewrite_step_plan_paths(
            runtime.step_plan_repo,
            plan["step_plan_id"],
            workspace,
        )

        if runtime.using_mock:
            _ensure_mock_source_file(rewritten_plan)
        if args.print_plan:
            _print_plan(rewritten_plan)

        if args.auto_approve:
            runtime.approval_api.approve_step_plan(rewritten_plan.step_plan_id)

        run = runtime.run_api.start_run(
            workflow_id=created["workflow_id"],
            step_plan_id=rewritten_plan.step_plan_id,
            run_id=args.run_id,
            approved=args.auto_approve,
        )

        resume_attempt = 0
        while args.auto_approve and run["approval_status"] == "PENDING":
            if resume_attempt >= args.max_resume:
                break
            run = runtime.run_api.resume_run(run_id=run["run_id"], approved=True)
            resume_attempt += 1

        _print_result(run, runtime.planner.last_warning)

        if run["last_error"] is not None:
            return 1
        if run["approval_status"] == "PENDING":
            return 2
        return 0
    finally:
        if hasattr(runtime.mcp_client, "close"):
            runtime.mcp_client.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)
