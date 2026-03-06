from __future__ import annotations

import re
from typing import Any, Literal, cast

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
from orchestra_agent.executor.failure_handler import (
    FailureContext,
    FailureHandler,
    RecoveryDecision,
)
from orchestra_agent.ports import IAgentStateStore, IAuditLogger, IMcpClient, ISnapshotManager

type ResolvedValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type ApprovalStage = Literal["PRE_STEP", "POST_STEP"]


class PlanExecutor:
    _template_pattern = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)\s*\}\}")
    _approval_context_key = "approval_context"

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

            self._apply_recovery_decision(state, decision)
            replan_attempt += 1

            if decision.approval_status == ApprovalStatus.PENDING:
                return state

    def submit_feedback(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        state: AgentState,
        feedback: str,
    ) -> AgentState:
        context = self._approval_context(state)
        if context is None:
            raise ValueError("No approval context is active. Feedback is not accepted now.")

        stage = context.get("stage")
        if stage != "POST_STEP":
            raise ValueError("Feedback is accepted only after step execution (POST_STEP stage).")

        step_id = context.get("step_id")
        if not isinstance(step_id, str):
            raise ValueError("Invalid approval context: step_id is missing.")
        reviewed_step = step_plan.step_map().get(step_id)
        if reviewed_step is None:
            raise KeyError(f"Feedback target step '{step_id}' does not exist in step plan.")

        snapshot_ref_raw = context.get("snapshot_ref")
        snapshot_ref = snapshot_ref_raw if isinstance(snapshot_ref_raw, str) else None

        decision = self._failure_handler.handle_feedback(
            workflow=workflow,
            step_plan=step_plan,
            state=state,
            reviewed_step=reviewed_step,
            feedback=feedback,
            snapshot_ref=snapshot_ref,
        )
        if not decision.should_replan:
            state.approval_status = ApprovalStatus.REJECTED
            state.last_error = feedback
            self._clear_approval_context(state)
            self._state_store.save(state)
            return state

        self._apply_recovery_decision(state, decision)
        self._clear_approval_context(state)
        state.approval_status = ApprovalStatus.PENDING
        state.current_step_id = None
        state.metadata["last_feedback"] = feedback
        state.metadata["feedback_step_id"] = step_id
        self._state_store.save(state)
        return state

    def _execute_single_plan(
        self, workflow: Workflow, step_plan: StepPlan, state: AgentState
    ) -> tuple[str, FailureContext | None]:
        if not self._resolve_pending_post_step_review(workflow, step_plan, state):
            return "paused", None

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

            if not self._ensure_pre_step_approval(workflow, step_plan, step, state):
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

                self._set_post_step_review_pending(
                    workflow=workflow,
                    step_plan=step_plan,
                    step=step,
                    state=state,
                    snapshot_ref=snapshot_ref,
                    result=result,
                )
                return "paused", None
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
        state.approval_status = ApprovalStatus.APPROVED
        self._clear_approval_context(state)
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "plan_complete",
                "run_id": state.run_id,
                "step_plan_id": step_plan.step_plan_id,
            }
        )
        return "completed", None

    def _resolve_pending_post_step_review(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        state: AgentState,
    ) -> bool:
        context = self._approval_context(state)
        if context is None:
            return True
        if context.get("stage") != "POST_STEP":
            return True

        context_version = context.get("step_plan_version")
        if context_version != step_plan.version:
            self._clear_approval_context(state)
            self._state_store.save(state)
            return True

        step_id = context.get("step_id")
        if not isinstance(step_id, str):
            self._clear_approval_context(state)
            self._state_store.save(state)
            return True

        if state.approval_status != ApprovalStatus.APPROVED:
            state.current_step_id = step_id
            state.approval_status = ApprovalStatus.PENDING
            self._state_store.save(state)
            return False

        self._clear_approval_context(state)
        state.current_step_id = step_id
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "step_review_approved",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_id": step_id,
            }
        )
        return True

    def _ensure_pre_step_approval(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        step: Step,
        state: AgentState,
    ) -> bool:
        context = self._approval_context(state)
        if (
            context is None
            or context.get("stage") != "PRE_STEP"
            or context.get("step_id") != step.step_id
            or context.get("step_plan_version") != step_plan.version
        ):
            self._set_pre_step_approval_pending(workflow, step_plan, step, state)
            return False

        if state.approval_status != ApprovalStatus.APPROVED:
            state.current_step_id = step.step_id
            state.approval_status = ApprovalStatus.PENDING
            self._state_store.save(state)
            return False

        self._clear_approval_context(state)
        return True

    def _set_pre_step_approval_pending(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        step: Step,
        state: AgentState,
    ) -> None:
        message = (
            f"次のstepを実行します。step_id={step.step_id} / 名称={step.name} / "
            f"内容={step.description}。実行してよければ approve してください。"
        )
        self._set_approval_context(
            state=state,
            stage="PRE_STEP",
            step_plan_version=step_plan.version,
            step_id=step.step_id,
            message=message,
        )
        state.current_step_id = step.step_id
        state.approval_status = ApprovalStatus.PENDING
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "step_pre_approval_wait",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_id": step.step_id,
                "message": message,
            }
        )

    def _set_post_step_review_pending(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        step: Step,
        state: AgentState,
        snapshot_ref: str | None,
        result: dict[str, Any],
    ) -> None:
        message = (
            f"step '{step.step_id}' が完了しました。成果物を確認し、問題なければ approve、"
            "修正が必要なら feedback を送ってください。"
        )
        self._set_approval_context(
            state=state,
            stage="POST_STEP",
            step_plan_version=step_plan.version,
            step_id=step.step_id,
            message=message,
            snapshot_ref=snapshot_ref,
            result=result,
        )
        state.current_step_id = step.step_id
        state.approval_status = ApprovalStatus.PENDING
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "step_post_review_wait",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_id": step.step_id,
                "message": message,
            }
        )

    def _set_approval_context(
        self,
        state: AgentState,
        stage: ApprovalStage,
        step_plan_version: int,
        step_id: str,
        message: str,
        snapshot_ref: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "stage": stage,
            "step_plan_version": step_plan_version,
            "step_id": step_id,
            "message": message,
        }
        if snapshot_ref is not None:
            context["snapshot_ref"] = snapshot_ref
        if result is not None:
            context["result"] = result
        state.metadata[self._approval_context_key] = context

    def _apply_recovery_decision(self, state: AgentState, decision: RecoveryDecision) -> None:
        assert decision.workflow is not None
        assert decision.step_plan is not None
        state.workflow_id = decision.workflow.workflow_id
        state.workflow_version = decision.workflow.version
        state.step_plan_id = decision.step_plan.step_plan_id
        state.step_plan_version = decision.step_plan.version
        state.approval_status = decision.approval_status
        state.current_step_id = None
        self._clear_approval_context(state)
        self._state_store.save(state)

    def _clear_approval_context(self, state: AgentState) -> None:
        state.metadata.pop(self._approval_context_key, None)

    def _approval_context(self, state: AgentState) -> dict[str, Any] | None:
        context = state.metadata.get(self._approval_context_key)
        if isinstance(context, dict):
            return context
        return None

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
