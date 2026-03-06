from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestra_agent.adapters.planner import LlmPlanner, SafeAugmentedLlmPlanner
from orchestra_agent.domain.workflow import Workflow


@dataclass
class StaticProvider:
    payload: dict[str, Any] | None

    def propose(self, workflow: Workflow, draft_plan: object) -> dict[str, Any] | None:
        return self.payload


def test_safe_augmented_planner_applies_valid_patch() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    planner = SafeAugmentedLlmPlanner(
        base_planner=LlmPlanner(),
        proposal_provider=StaticProvider(
            {
                "steps": [
                    {
                        "step_id": "calculate_totals",
                        "resolved_input": {
                            "file": "sales.xlsx",
                            "sheet": "Sheet1",
                            "column": "D",
                        },
                    }
                ]
            }
        ),
    )

    plan = planner.compile_step_plan(workflow)

    assert plan.step_map()["calculate_totals"].resolved_input["column"] == "D"
    assert planner.last_warning is None


def test_safe_augmented_planner_rejects_unsafe_tool_ref() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    planner = SafeAugmentedLlmPlanner(
        base_planner=LlmPlanner(),
        proposal_provider=StaticProvider(
            {
                "steps": [
                    {
                        "step_id": "save_file",
                        "tool_ref": "terminal.exec",
                    }
                ]
            }
        ),
    )

    plan = planner.compile_step_plan(workflow)

    assert plan.step_map()["save_file"].tool_ref == "excel.save_file"
    assert planner.last_warning is not None

