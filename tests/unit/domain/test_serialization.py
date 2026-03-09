from __future__ import annotations

from orchestra_agent.domain import BackupScope, ReplanContext, RiskLevel, Step, StepPlan, Workflow
from orchestra_agent.domain.serialization import (
    step_plan_to_dict,
    step_plan_to_json_text,
    workflow_to_dict,
    workflow_to_xml_text,
)


def test_workflow_serialization_helpers_include_replan_context() -> None:
    workflow = Workflow(
        workflow_id="wf-serialize",
        name="Serialize workflow",
        version=3,
        objective="Review the source files and export a report.",
        reference_files=["refs/spec.pdf"],
        constraints=["Do not mutate the original workbook"],
        success_criteria=["report.txt exists"],
        feedback_history=["Initial draft was incorrect"],
        replan_context=ReplanContext(
            trigger="feedback",
            change_summary="Replace the review step with a stricter check.",
            source_workflow_document="<workflow id=\"wf-serialize\" version=\"2\" />",
            source_step_plan_document='{"step_plan_id":"sp-old"}',
        ),
    )

    payload = workflow_to_dict(workflow)
    xml_text = workflow_to_xml_text(workflow)

    assert payload["workflow_id"] == "wf-serialize"
    assert payload["replan_context"]["trigger"] == "feedback"
    assert "<replan_context>" in xml_text
    assert "Replace the review step with a stricter check." in xml_text


def test_step_plan_serialization_helpers_render_step_metadata() -> None:
    step_plan = StepPlan(
        step_plan_id="sp-serialize",
        workflow_id="wf-serialize",
        version=2,
        steps=[
            Step(
                step_id="review",
                name="Review output",
                description="Review the generated report",
                tool_ref="orchestra.ai_review",
                resolved_input={"message": "Review the report."},
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                backup_scope=BackupScope.WORKSPACE,
            )
        ],
    )

    payload = step_plan_to_dict(step_plan)
    json_text = step_plan_to_json_text(step_plan)

    assert payload["steps"][0]["tool_ref"] == "orchestra.ai_review"
    assert payload["steps"][0]["requires_approval"] is True
    assert '"backup_scope": "WORKSPACE"' in json_text
