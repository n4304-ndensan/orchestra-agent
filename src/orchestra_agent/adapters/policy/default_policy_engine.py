from __future__ import annotations

from dataclasses import replace

from orchestra_agent.domain.enums import ApprovalStatus, RiskLevel
from orchestra_agent.domain.step import Step
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.ports.policy_engine import IPolicyEngine, PolicyEvaluationResult


class DefaultPolicyEngine(IPolicyEngine):
    def evaluate(self, step_plan: StepPlan) -> PolicyEvaluationResult:
        normalized_steps = []
        reasons: list[str] = []

        for step in step_plan.steps:
            normalized = step
            if not step.run:
                normalized = replace(step, skip=True)
            if step.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                reasons.append(
                    f"Step '{step.step_id}' has elevated risk level '{step.risk_level.value}'."
                )
            if step.requires_approval:
                reasons.append(f"Step '{step.step_id}' explicitly requires approval.")
            normalized_steps.append(normalized)

        normalized_steps = self._require_first_executable_step_approval(
            step_plan=step_plan,
            steps=normalized_steps,
            reasons=reasons,
        )

        updated_plan = StepPlan(
            step_plan_id=step_plan.step_plan_id,
            workflow_id=step_plan.workflow_id,
            version=step_plan.version,
            steps=normalized_steps,
        )

        if updated_plan.requires_runtime_approval:
            return PolicyEvaluationResult(
                step_plan=updated_plan,
                approval_status=ApprovalStatus.PENDING,
                reasons=reasons,
            )
        return PolicyEvaluationResult(
            step_plan=updated_plan,
            approval_status=ApprovalStatus.NOT_REQUIRED,
            reasons=["No high-risk step detected."],
        )

    @staticmethod
    def _require_first_executable_step_approval(
        *,
        step_plan: StepPlan,
        steps: list[Step],
        reasons: list[str],
    ) -> list[Step]:
        ordered_steps = [
            step
            for step in step_plan.ordered_steps()
            if step.run and not step.skip
        ]
        if not ordered_steps:
            return steps

        if not any(step.requires_runtime_approval for step in steps):
            return steps

        first_step_id = ordered_steps[0].step_id
        updated_steps: list[Step] = []
        changed = False
        for step in steps:
            if step.step_id != first_step_id or step.requires_runtime_approval:
                updated_steps.append(step)
                continue
            updated_steps.append(replace(step, requires_approval=True))
            changed = True

        if changed:
            reasons.append(
                f"Step '{first_step_id}' requires approval as the first executable checkpoint."
            )
            return updated_steps
        return steps

