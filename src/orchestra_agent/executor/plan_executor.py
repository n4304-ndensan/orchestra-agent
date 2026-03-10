from __future__ import annotations

import json
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
from orchestra_agent.observability import bind_observation_context
from orchestra_agent.ports import (
    IAgentStateStore,
    IAuditLogger,
    IMcpClient,
    ISnapshotManager,
    IStepExecutor,
)

type ResolvedValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type ApprovalStage = Literal["PLAN", "PRE_STEP", "POST_STEP"]


class PlanExecutor:
    _template_pattern = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)\s*\}\}")
    _approval_context_key = "approval_context"
    _approved_plan_version_key = "approved_step_plan_version"
    _repair_attempt_key = "repair_attempts"

    def __init__(
        self,
        mcp_client: IMcpClient,
        state_store: IAgentStateStore,
        snapshot_manager: ISnapshotManager,
        audit_logger: IAuditLogger,
        failure_handler: FailureHandler,
        step_executor: IStepExecutor | None = None,
        default_snapshot_scope: BackupScope = BackupScope.WORKSPACE,
    ) -> None:
        self._mcp_client = mcp_client
        self._state_store = state_store
        self._snapshot_manager = snapshot_manager
        self._audit_logger = audit_logger
        self._failure_handler = failure_handler
        self._step_executor = step_executor
        self._default_snapshot_scope = default_snapshot_scope

    def execute(self, workflow: Workflow, step_plan: StepPlan, state: AgentState) -> AgentState:
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
                replan_attempt=self._repair_attempts(state),
            )
            if not decision.should_replan:
                state.approval_status = ApprovalStatus.REJECTED
                state.last_error = context.error_message
                self._state_store.save(state)
                return state

            self._apply_recovery_decision(state, decision)
            assert decision.workflow is not None
            assert decision.step_plan is not None
            active_workflow = decision.workflow
            active_plan = decision.step_plan

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
        if context is not None:
            stage = context.get("stage")
            if stage not in ("PLAN", "PRE_STEP", "POST_STEP"):
                raise ValueError("Invalid approval context stage for feedback.")

            step_id = context.get("step_id")
            if not isinstance(step_id, str):
                raise ValueError("Invalid approval context: step_id is missing.")
            if step_id != "__plan__" and step_plan.step_map().get(step_id) is None:
                raise KeyError(f"Feedback target step '{step_id}' does not exist in step plan.")

            snapshot_ref_raw = context.get("snapshot_ref")
            snapshot_ref = snapshot_ref_raw if isinstance(snapshot_ref_raw, str) else None
            review_target = "plan" if step_id == "__plan__" else step_id
        else:
            failed_record = self._latest_failed_record(state)
            if failed_record is None:
                raise ValueError(
                    "No approval context is active and there is no failed step to attach feedback."
                )
            review_target = failed_record.step_id
            snapshot_ref = failed_record.snapshot_ref

        decision = self._failure_handler.handle_feedback(
            workflow=workflow,
            step_plan=step_plan,
            state=state,
            review_target=review_target,
            feedback=feedback,
            snapshot_ref=snapshot_ref,
            replan_attempt=self._repair_attempts(state),
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
        state.metadata["feedback_step_id"] = review_target
        self._state_store.save(state)
        return state

    def _execute_single_plan(
        self, workflow: Workflow, step_plan: StepPlan, state: AgentState
    ) -> tuple[str, FailureContext | None]:
        if not self._ensure_plan_approval(workflow, step_plan, state):
            return "paused", None

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

            resolved_input = self._resolve_step_input(step.resolved_input, step_results)
            snapshot_ref = self._create_pre_step_snapshot(step, step_plan, resolved_input)
            record = ExecutionRecord.pending(step.step_id)
            record.metadata["step_plan_version"] = step_plan.version
            record.snapshot_ref = snapshot_ref
            record.mark_running()
            self._audit_logger.record(
                {
                    "event_type": "step_started",
                    "run_id": state.run_id,
                    "step_plan_id": step_plan.step_plan_id,
                    "step_id": step.step_id,
                    "tool_ref": step.tool_ref,
                    "snapshot_ref": snapshot_ref,
                }
            )

            try:
                with bind_observation_context(
                    phase="execute_step",
                    run_id=state.run_id,
                    workflow_id=workflow.workflow_id,
                    workflow_version=workflow.version,
                    step_plan_id=step_plan.step_plan_id,
                    step_plan_version=step_plan.version,
                    step_id=step.step_id,
                    tool_ref=step.tool_ref,
                ):
                    result = self._execute_step(
                        workflow=workflow,
                        step=step,
                        resolved_input=resolved_input,
                        step_results=step_results,
                    )
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

                if self._set_post_step_review_pending(
                    workflow=workflow,
                    step_plan=step_plan,
                    step=step,
                    state=state,
                    snapshot_ref=snapshot_ref,
                    result=result,
                ):
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

    def _ensure_plan_approval(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        state: AgentState,
    ) -> bool:
        if not step_plan.requires_runtime_approval:
            self._clear_approval_context(state)
            return True

        approved_version = state.metadata.get(self._approved_plan_version_key)
        if approved_version == step_plan.version:
            return True

        context = self._approval_context(state)
        if (
            context is None
            or context.get("stage") != "PLAN"
            or context.get("step_plan_version") != step_plan.version
        ):
            if state.approval_status == ApprovalStatus.APPROVED:
                state.metadata[self._approved_plan_version_key] = step_plan.version
                self._clear_approval_context(state)
                self._state_store.save(state)
                return True
            self._set_plan_approval_pending(workflow, step_plan, state)
            return False

        if state.approval_status != ApprovalStatus.APPROVED:
            state.current_step_id = None
            state.approval_status = ApprovalStatus.PENDING
            self._state_store.save(state)
            return False

        state.metadata[self._approved_plan_version_key] = step_plan.version
        self._clear_approval_context(state)
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "plan_review_approved",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_plan_version": step_plan.version,
            }
        )
        return True

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
        if not step.requires_runtime_approval:
            return True

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

    def _set_plan_approval_pending(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        state: AgentState,
    ) -> None:
        details = self._plan_approval_details(step_plan)
        message = (
            f"step plan v{step_plan.version} を実行します。workflow={workflow.workflow_id} / "
            "内容を確認し、実行してよければ approve してください。"
        )
        self._set_approval_context(
            state=state,
            stage="PLAN",
            step_plan_version=step_plan.version,
            step_id="__plan__",
            message=message,
            details=details,
        )
        state.current_step_id = None
        state.approval_status = ApprovalStatus.PENDING
        self._state_store.save(state)
        self._audit_logger.record(
            {
                "event_type": "plan_review_wait",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_plan_version": step_plan.version,
                "message": message,
                "details": details,
            }
        )

    def _set_pre_step_approval_pending(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        step: Step,
        state: AgentState,
    ) -> None:
        details = self._step_approval_details(step)
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
            details=details,
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
                "details": details,
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
    ) -> bool:
        if not step.requires_runtime_approval:
            return False

        details = self._post_step_review_details(step, result)
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
            details=details,
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
                "details": details,
            }
        )
        return True

    def _set_approval_context(
        self,
        state: AgentState,
        stage: ApprovalStage,
        step_plan_version: int,
        step_id: str,
        message: str,
        snapshot_ref: str | None = None,
        result: dict[str, Any] | None = None,
        details: list[str] | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "stage": stage,
            "step_plan_version": step_plan_version,
            "step_id": step_id,
            "message": message,
        }
        if details:
            context["details"] = details
        if snapshot_ref is not None:
            context["snapshot_ref"] = snapshot_ref
        if result is not None:
            context["result"] = result
        state.metadata[self._approval_context_key] = context

    @classmethod
    def _plan_approval_details(cls, step_plan: StepPlan) -> list[str]:
        ordered_steps = step_plan.ordered_steps()
        lines = [f"plan     {len(ordered_steps)} steps queued"]
        max_steps = 8
        for index, step in enumerate(ordered_steps[:max_steps], start=1):
            preview = cls._step_preview(step)
            lines.append(f"{index:02d}. {step.step_id} | {step.tool_ref} | {preview}")
        remaining = len(ordered_steps) - max_steps
        if remaining > 0:
            lines.append(f"... +{remaining} more steps")
        return lines

    @classmethod
    def _step_approval_details(cls, step: Step) -> list[str]:
        lines = [
            f"step     {step.step_id}",
            f"tool     {step.tool_ref}",
            f"what     {step.description}",
        ]
        input_preview = cls._mapping_preview(step.resolved_input)
        if input_preview:
            lines.append(f"input    {input_preview}")
        return lines

    @classmethod
    def _post_step_review_details(cls, step: Step, result: dict[str, Any]) -> list[str]:
        lines = [
            f"step     {step.step_id}",
            f"tool     {step.tool_ref}",
            f"what     {step.description}",
        ]
        result_preview = cls._mapping_preview(result)
        if result_preview:
            lines.append(f"result   {result_preview}")
        return lines

    @classmethod
    def _step_preview(cls, step: Step) -> str:
        parts = []
        if step.description.strip():
            parts.append(step.description.strip())
        input_preview = cls._mapping_preview(step.resolved_input)
        if input_preview:
            parts.append(input_preview)
        return " | ".join(parts) if parts else "-"

    @classmethod
    def _mapping_preview(cls, payload: dict[str, Any], max_items: int = 4) -> str:
        if not payload:
            return ""
        parts: list[str] = []
        keys = list(payload.keys())
        for key in keys[:max_items]:
            parts.append(f"{key}={cls._value_preview(payload[key])}")
        remaining = len(keys) - max_items
        if remaining > 0:
            parts.append(f"+{remaining} more")
        return ", ".join(parts)

    @classmethod
    def _value_preview(cls, value: Any) -> str:
        if isinstance(value, dict):
            return "{" + cls._mapping_preview(value, max_items=2) + "}"
        if isinstance(value, list):
            head = ", ".join(cls._value_preview(item) for item in value[:3])
            if len(value) > 3:
                head = f"{head}, +{len(value) - 3} more"
            return f"[{head}]"
        if isinstance(value, str):
            sanitized = value.replace("\r", " ").replace("\n", " ").strip()
            return sanitized if len(sanitized) <= 72 else f"{sanitized[:69]}..."
        return json.dumps(value, ensure_ascii=False)

    def _apply_recovery_decision(self, state: AgentState, decision: RecoveryDecision) -> None:
        assert decision.workflow is not None
        assert decision.step_plan is not None
        state.workflow_id = decision.workflow.workflow_id
        state.workflow_version = decision.workflow.version
        state.step_plan_id = decision.step_plan.step_plan_id
        state.step_plan_version = decision.step_plan.version
        state.approval_status = decision.approval_status
        state.current_step_id = None
        state.metadata[self._repair_attempt_key] = self._repair_attempts(state) + 1
        state.metadata.pop(self._approved_plan_version_key, None)
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
        if record.snapshot_ref is not None and record.snapshot_ref not in state.snapshot_refs:
            state.snapshot_refs.append(record.snapshot_ref)
        state.append_execution(record)
        self._state_store.save(state)

    def _execute_step(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if self._step_executor is not None:
            return self._step_executor.execute(
                workflow=workflow,
                step=step,
                resolved_input=resolved_input,
                step_results=step_results,
                mcp_client=self._mcp_client,
            )

        if step.tool_ref.startswith("orchestra."):
            raise RuntimeError(f"Unsupported orchestra tool_ref '{step.tool_ref}'.")
        return self._mcp_client.call_tool(step.tool_ref, resolved_input)

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

    def _create_pre_step_snapshot(
        self,
        step: Step,
        step_plan: StepPlan,
        resolved_input: dict[str, Any],
    ) -> str | None:
        snapshot_scope = step.backup_scope
        if snapshot_scope == BackupScope.NONE:
            snapshot_scope = self._default_snapshot_scope
        if snapshot_scope == BackupScope.NONE:
            return None
        metadata = {
            "step_plan_id": step_plan.step_plan_id,
            "step_id": step.step_id,
            "tool_ref": step.tool_ref,
            "snapshot_phase": "pre_step",
        }
        file_path = resolved_input.get("file")
        if isinstance(file_path, str):
            metadata["file"] = file_path
        return self._snapshot_manager.create_snapshot(scope=snapshot_scope, metadata=metadata)

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

    def _repair_attempts(self, state: AgentState) -> int:
        raw_value = state.metadata.get(self._repair_attempt_key, 0)
        if isinstance(raw_value, int):
            return raw_value
        return 0

    @staticmethod
    def _latest_failed_record(state: AgentState) -> ExecutionRecord | None:
        for record in reversed(state.execution_history):
            if record.status == ExecutionStatus.FAILED:
                return record
        return None
