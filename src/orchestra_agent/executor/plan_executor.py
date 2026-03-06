from __future__ import annotations

import re
from typing import Any, TypeAlias, cast

from orchestra_agent.domain import (
    AgentState,
    ApprovalStatus,
    BackupScope,
    ExecutionRecord,
    ExecutionStatus,
    Step,
    StepPlan,
    Workflow,
)
from orchestra_agent.executor.failure_handler import FailureContext, FailureHandler
from orchestra_agent.ports import IAgentStateStore, IAuditLogger, IMcpClient, ISnapshotManager

ResolvedValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None


class PlanExecutor:
    _template_pattern = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)\s*\}\}")

    def __init__(
        self,
        mcp_client: IMcpClient,
        state_store: IAgentStateStore,
        snapshot_manager: ISnapshotManager,
        audit_logger: IAuditLogger,
        failure_handler: FailureHandler,
    ) -> None:
        self._mcp_client = mcp_client
        self._state_store = state_store
        self._snapshot_manager = snapshot_manager
        self._audit_logger = audit_logger
        self._failure_handler = failure_handler

    def execute(self, workflow: Workflow, step_plan: StepPlan, state: AgentState) -> AgentState:
        replan_attempt = 0
        active_workflow = workflow
        active_plan = step_plan

        while True:
            status, context = self._execute_single_plan(active_workflow, active_plan, state)
            if status in ("completed", "paused"):
                return state

            if context is None:
                raise RuntimeError("Execution failed without failure context.")

            decision = self._failure_handler.handle_failure(
                context=context,
                replan_attempt=replan_attempt,
            )
            if not decision.should_replan:
                state.approval_status = ApprovalStatus.REJECTED
                state.last_error = context.error_message
                self._state_store.save(state)
                return state

            assert decision.workflow is not None
            assert decision.step_plan is not None
            replan_attempt += 1

            active_workflow = decision.workflow
            active_plan = decision.step_plan
            state.workflow_id = active_workflow.workflow_id
            state.workflow_version = active_workflow.version
            state.step_plan_id = active_plan.step_plan_id
            state.step_plan_version = active_plan.version
            state.approval_status = decision.approval_status
            self._state_store.save(state)

            if decision.approval_status == ApprovalStatus.PENDING:
                return state

    def _execute_single_plan(
        self, workflow: Workflow, step_plan: StepPlan, state: AgentState
    ) -> tuple[str, FailureContext | None]:
        step_results = self._collect_step_results(state)

        for step in step_plan.ordered_steps():
            if self._already_processed_for_plan(state, step.step_id):
                continue

            if step.skip or not step.run:
                record = ExecutionRecord.pending(step.step_id)
                record.metadata["step_plan_version"] = step_plan.version
                record.mark_skipped()
                self._append_record(state, record)
                continue

            if step.requires_approval and state.approval_status != ApprovalStatus.APPROVED:
                state.current_step_id = step.step_id
                state.approval_status = ApprovalStatus.PENDING
                self._state_store.save(state)
                self._audit_logger.record(
                    {
                        "event_type": "approval_wait",
                        "run_id": state.run_id,
                        "workflow_id": workflow.workflow_id,
                        "step_plan_id": step_plan.step_plan_id,
                        "step_id": step.step_id,
                    }
                )
                return "paused", None

            snapshot_ref = self._create_snapshot_if_needed(step, step_plan)
            record = ExecutionRecord.pending(step.step_id)
            record.metadata["step_plan_version"] = step_plan.version
            record.snapshot_ref = snapshot_ref
            record.mark_running()

            try:
                resolved_input = self._resolve_step_input(step.resolved_input, step_results)
                result = self._mcp_client.call_tool(step.tool_ref, resolved_input)
                record.mark_success(result=result)
                step_results[step.step_id] = result
                self._append_record(state, record)
                self._audit_logger.record(
                    {
                        "event_type": "step_success",
                        "run_id": state.run_id,
                        "step_plan_id": step_plan.step_plan_id,
                        "step_id": step.step_id,
                        "tool_ref": step.tool_ref,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                record.mark_failed(error_message)
                self._append_record(state, record)

                return "failed", FailureContext(
                    workflow=workflow,
                    step_plan=step_plan,
                    state=state,
                    failed_step=step,
                    error_message=error_message,
                    snapshot_ref=snapshot_ref,
                )

        state.current_step_id = None
        state.last_error = None
        if state.approval_status == ApprovalStatus.PENDING:
            state.approval_status = ApprovalStatus.APPROVED
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "plan_complete",
                "run_id": state.run_id,
                "step_plan_id": step_plan.step_plan_id,
            }
        )
        return "completed", None

    def _append_record(self, state: AgentState, record: ExecutionRecord) -> None:
        state.append_execution(record)
        self._state_store.save(state)

    @staticmethod
    def _collect_step_results(state: AgentState) -> dict[str, dict[str, Any]]:
        outputs: dict[str, dict[str, Any]] = {}
        for record in state.execution_history:
            if record.status == ExecutionStatus.SUCCESS and record.result is not None:
                outputs[record.step_id] = record.result
        return outputs

    @staticmethod
    def _already_processed_for_plan(state: AgentState, step_id: str) -> bool:
        for record in reversed(state.execution_history):
            if record.step_id != step_id:
                continue
            record_plan_version = record.metadata.get("step_plan_version")
            if record_plan_version != state.step_plan_version:
                continue
            return record.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED)
        return False

    def _create_snapshot_if_needed(self, step: Step, step_plan: StepPlan) -> str | None:
        if step.backup_scope == BackupScope.NONE:
            return None

        metadata = {
            "step_plan_id": step_plan.step_plan_id,
            "step_id": step.step_id,
            "tool_ref": step.tool_ref,
        }
        file_path = step.resolved_input.get("file")
        if isinstance(file_path, str):
            metadata["file"] = file_path
        return self._snapshot_manager.create_snapshot(scope=step.backup_scope, metadata=metadata)

    def _resolve_step_input(
        self,
        input_payload: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        resolved = self._resolve_input(input_payload, step_results)
        if not isinstance(resolved, dict):
            raise TypeError("Resolved step input must be a dictionary.")
        return resolved

    def _resolve_input(
        self,
        value: ResolvedValue,
        step_results: dict[str, dict[str, Any]],
    ) -> ResolvedValue:
        if isinstance(value, dict):
            resolved_dict: dict[str, Any] = {
                k: self._resolve_input(v, step_results) for k, v in value.items()
            }
            return resolved_dict
        if isinstance(value, list):
            resolved_list: list[Any] = [self._resolve_input(v, step_results) for v in value]
            return resolved_list
        if not isinstance(value, str):
            return value

        full_match = self._template_pattern.fullmatch(value)
        if full_match is not None:
            return self._lookup_step_result(
                step_results=step_results,
                step_id=full_match.group(1),
                key_path=full_match.group(2),
            )

        def replace(match: re.Match[str]) -> str:
            replacement = self._lookup_step_result(
                step_results=step_results,
                step_id=match.group(1),
                key_path=match.group(2),
            )
            return str(replacement)

        return self._template_pattern.sub(replace, value)

    @staticmethod
    def _lookup_step_result(
        step_results: dict[str, dict[str, Any]],
        step_id: str,
        key_path: str,
    ) -> ResolvedValue:
        data = step_results.get(step_id)
        if data is None:
            raise KeyError(f"No execution result found for dependency step '{step_id}'.")
        value: Any = data
        for part in key_path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise KeyError(f"Missing key path '{key_path}' in step result '{step_id}'.")
        return PlanExecutor._to_resolved_value(value)

    @staticmethod
    def _to_resolved_value(value: Any) -> ResolvedValue:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        if isinstance(value, list):
            return value
        raise TypeError(f"Unsupported resolved value type: {type(value).__name__}")
