from __future__ import annotations

from dataclasses import replace

from orchestra_agent.domain.enums import ApprovalStatus, RiskLevel
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.ports.policy_engine import IPolicyEngine, PolicyEvaluationResult


class DefaultPolicyEngine(IPolicyEngine):
    def evaluate(self, step_plan: StepPlan) -> PolicyEvaluationResult:
        normalized_steps = []
        reasons: list[str] = []
        approval_needed = False

        for step in step_plan.steps:
            normalized = step
            if not step.run:
                normalized = replace(step, skip=True)
            if step.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                approval_needed = True
                reasons.append(
                    f"Step '{step.step_id}' has elevated risk level '{step.risk_level.value}'."
                )
            if step.requires_approval:
                approval_needed = True
                reasons.append(f"Step '{step.step_id}' explicitly requires approval.")
            normalized_steps.append(normalized)

        updated_plan = StepPlan(
            step_plan_id=step_plan.step_plan_id,
            workflow_id=step_plan.workflow_id,
            version=step_plan.version,
            steps=normalized_steps,
        )

        if approval_needed:
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

