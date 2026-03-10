from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.adapters.planner import LlmPlanner, StructuredLlmPlanner
from orchestra_agent.domain import ReplanContext, Workflow
from orchestra_agent.ports import LlmGenerateRequest


class FakeLlmClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.requests: list[LlmGenerateRequest] = []

    def generate(self, request: LlmGenerateRequest) -> str:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("No fake LLM response is queued.")
        return self._responses.pop(0)


def test_structured_llm_planner_builds_full_step_plan() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    reference_file = base / "requirements.txt"
    reference_file.write_text("Use the attached workbook notes.", encoding="utf-8")
    workflow = Workflow(
        workflow_id="wf-1",
        name="Workspace summary",
        version=1,
        objective="Read sales.xlsx and write a local report file.",
        reference_files=[str(reference_file)],
    )
    client = FakeLlmClient(
        [
            """
            {
              "steps": [
                {
                  "step_id": "prepare_report",
                  "name": "Prepare report",
                  "description": "Use MCP and write a local report",
                  "tool_ref": "orchestra.llm_execute",
                  "resolved_input": {
                    "allowed_mcp_tools": ["excel.read_sheet"],
                    "instruction": "Read sheet and write report.txt"
                  },
                  "depends_on": [],
                  "risk_level": "MEDIUM",
                  "requires_approval": true,
                  "run": true,
                  "skip": false,
                  "backup_scope": "WORKSPACE"
                },
                {
                  "step_id": "export_file",
                  "name": "Export workbook",
                  "description": "Save workbook",
                  "tool_ref": "excel.save_file",
                  "resolved_input": {
                    "file": "sales.xlsx",
                    "output": "summary.xlsx"
                  },
                  "depends_on": ["prepare_report"],
                  "risk_level": "HIGH",
                  "requires_approval": true,
                  "run": true,
                  "skip": false,
                  "backup_scope": "FILE"
                }
              ]
            }
            """
        ]
    )
    try:
        planner = StructuredLlmPlanner(
            llm_client=client,
            available_tools_supplier=lambda: ["excel.read_sheet", "excel.save_file"],
            available_tool_catalog_supplier=lambda: [
                {
                    "name": "excel.read_sheet",
                    "description": "Read worksheet rows.",
                },
                {
                    "name": "excel.save_file",
                    "description": "Save workbook output.",
                },
            ],
        )

        plan = planner.compile_step_plan(workflow)

        assert [step.step_id for step in plan.steps] == ["prepare_report", "export_file"]
        assert plan.steps[0].tool_ref == "orchestra.llm_execute"
        assert plan.steps[0].backup_scope.value == "WORKSPACE"
        assert plan.steps[1].depends_on == ["prepare_report"]
        assert client.requests[0].messages[1].attachments[0].path == str(reference_file)
        assert '"description": "Read worksheet rows."' in client.requests[0].messages[1].content
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_structured_llm_planner_falls_back_when_response_is_invalid() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective="Summarize sales.xlsx column C and export as summary.xlsx",
    )
    client = FakeLlmClient(["not-json"])
    planner = StructuredLlmPlanner(
        llm_client=client,
        available_tools_supplier=lambda: ["excel.open_file"],
        fallback_planner=LlmPlanner(),
    )

    plan = planner.compile_step_plan(workflow)

    assert plan.steps[0].step_id == "open_file"
    assert planner.last_warning is not None
    assert "preview=not-json" in planner.last_warning


def test_structured_llm_planner_accepts_ai_review_builtin_tool() -> None:
    workflow = Workflow(
        workflow_id="wf-2",
        name="Review program",
        version=1,
        objective="Review the referenced program and summarize risks.",
    )
    client = FakeLlmClient(
        [
            """
            {
              "steps": [
                {
                  "step_id": "review_program",
                  "name": "Review program",
                  "description": "Read the target file and review it",
                  "tool_ref": "orchestra.ai_review",
                  "resolved_input": {
                    "message": "Review this file and summarize issues."
                  },
                  "depends_on": [],
                  "risk_level": "MEDIUM",
                  "requires_approval": true,
                  "run": true,
                  "skip": false,
                  "backup_scope": "NONE"
                }
              ]
            }
            """
        ]
    )
    planner = StructuredLlmPlanner(
        llm_client=client,
        available_tools_supplier=lambda: ["fs_read_text"],
    )

    plan = planner.compile_step_plan(workflow)

    assert plan.steps[0].tool_ref == "orchestra.ai_review"


def test_structured_llm_planner_includes_replan_source_document() -> None:
    workflow = Workflow(
        workflow_id="wf-replan",
        name="Replan workflow",
        version=2,
        objective="Update the procedure after review.",
        feedback_history=["Prior correction"],
        replan_context=ReplanContext(
            trigger="feedback",
            change_summary="Read the original workflow doc and replace the review step.",
            source_workflow_document="<workflow id=\"wf-replan\" version=\"1\" />",
            source_step_plan_document='{"step_plan_id":"sp-old","steps":[]}',
        ),
    )
    client = FakeLlmClient(
        [
            """
            {
              "steps": [
                {
                  "step_id": "review_program",
                  "name": "Review program",
                  "description": "Read the target file and review it",
                  "tool_ref": "orchestra.ai_review",
                  "resolved_input": {
                    "message": "Review this file and summarize issues."
                  },
                  "depends_on": [],
                  "risk_level": "MEDIUM",
                  "requires_approval": true,
                  "run": true,
                  "skip": false,
                  "backup_scope": "NONE"
                }
              ]
            }
            """
        ]
    )
    planner = StructuredLlmPlanner(
        llm_client=client,
        available_tools_supplier=lambda: ["fs_read_text"],
    )

    planner.compile_step_plan(workflow)

    request_body = client.requests[0].messages[1].content
    assert '"replan_context"' in request_body
    assert '"change_summary": "Read the original workflow doc and replace the review step."' in (
        request_body
    )
    assert "<workflow id=\\\"wf-replan\\\" version=\\\"1\\\" />" in request_body
