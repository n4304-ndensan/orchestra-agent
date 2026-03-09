from __future__ import annotations

from orchestra_agent.adapters.planner import LlmPlanner, StructuredLlmPlanner
from orchestra_agent.domain import Workflow
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
    workflow = Workflow(
        workflow_id="wf-1",
        name="Workspace summary",
        version=1,
        objective="Read sales.xlsx and write a local report file.",
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
    planner = StructuredLlmPlanner(
        llm_client=client,
        available_tools_supplier=lambda: ["excel.read_sheet", "excel.save_file"],
    )

    plan = planner.compile_step_plan(workflow)

    assert [step.step_id for step in plan.steps] == ["prepare_report", "export_file"]
    assert plan.steps[0].tool_ref == "orchestra.llm_execute"
    assert plan.steps[0].backup_scope.value == "WORKSPACE"
    assert plan.steps[1].depends_on == ["prepare_report"]


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
