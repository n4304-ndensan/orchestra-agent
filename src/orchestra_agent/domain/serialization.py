from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from orchestra_agent.domain.step import Step
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import ReplanContext, Workflow


def replan_context_to_dict(replan_context: ReplanContext | None) -> dict[str, str] | None:
    if replan_context is None:
        return None
    return {
        "trigger": replan_context.trigger,
        "change_summary": replan_context.change_summary,
        "source_workflow_document": replan_context.source_workflow_document,
        "source_step_plan_document": replan_context.source_step_plan_document,
    }


def workflow_to_dict(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.workflow_id,
        "name": workflow.name,
        "version": workflow.version,
        "objective": workflow.objective,
        "reference_files": list(workflow.reference_files),
        "constraints": list(workflow.constraints),
        "success_criteria": list(workflow.success_criteria),
        "feedback_history": list(workflow.feedback_history),
        "replan_context": replan_context_to_dict(workflow.replan_context),
    }


def workflow_to_xml_root(workflow: Workflow) -> ET.Element:
    root = ET.Element(
        "workflow",
        attrib={
            "id": workflow.workflow_id,
            "version": str(workflow.version),
        },
    )
    ET.SubElement(root, "name").text = workflow.name
    ET.SubElement(root, "objective").text = workflow.objective

    reference_files = ET.SubElement(root, "reference_files")
    for item in workflow.reference_files:
        ET.SubElement(reference_files, "item").text = item

    constraints = ET.SubElement(root, "constraints")
    for item in workflow.constraints:
        ET.SubElement(constraints, "item").text = item

    success_criteria = ET.SubElement(root, "success_criteria")
    for item in workflow.success_criteria:
        ET.SubElement(success_criteria, "item").text = item

    feedback_history = ET.SubElement(root, "feedback_history")
    for item in workflow.feedback_history:
        ET.SubElement(feedback_history, "item").text = item

    if workflow.replan_context is not None:
        replan_context = ET.SubElement(root, "replan_context")
        ET.SubElement(replan_context, "trigger").text = workflow.replan_context.trigger
        ET.SubElement(replan_context, "change_summary").text = (
            workflow.replan_context.change_summary
        )
        ET.SubElement(replan_context, "source_workflow_document").text = (
            workflow.replan_context.source_workflow_document
        )
        ET.SubElement(replan_context, "source_step_plan_document").text = (
            workflow.replan_context.source_step_plan_document
        )

    return root


def workflow_to_xml_text(workflow: Workflow) -> str:
    return ET.tostring(workflow_to_xml_root(workflow), encoding="unicode")


def step_to_dict(step: Step) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "name": step.name,
        "description": step.description,
        "tool_ref": step.tool_ref,
        "resolved_input": step.resolved_input,
        "depends_on": step.depends_on,
        "risk_level": step.risk_level.value,
        "requires_approval": step.requires_approval,
        "run": step.run,
        "skip": step.skip,
        "backup_scope": step.backup_scope.value,
    }


def step_plan_to_dict(step_plan: StepPlan) -> dict[str, Any]:
    return {
        "step_plan_id": step_plan.step_plan_id,
        "workflow_id": step_plan.workflow_id,
        "version": step_plan.version,
        "steps": [step_to_dict(step) for step in step_plan.steps],
    }


def step_plan_to_json_text(step_plan: StepPlan, *, indent: int = 2) -> str:
    return json.dumps(step_plan_to_dict(step_plan), ensure_ascii=False, indent=indent)
