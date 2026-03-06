from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestra_agent.adapters.planner import (
    LlmPlanner,
    LlmStepProposalProvider,
    SafeAugmentedLlmPlanner,
)
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports import ILlmClient, LlmGenerateRequest


@dataclass
class StaticProvider:
    payload: dict[str, Any] | None

    def propose(self, workflow: Workflow, draft_plan: object) -> dict[str, Any] | None:
        return self.payload


@dataclass
class FakeLlmClient(ILlmClient):
    response_text: str
    last_request: LlmGenerateRequest | None = None

    def generate(self, request: LlmGenerateRequest) -> str:
        self.last_request = request
        return self.response_text


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


def test_llm_step_proposal_provider_parses_response() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    draft_plan = LlmPlanner().compile_step_plan(workflow)
    client = FakeLlmClient(
        response_text='{"steps":[{"step_id":"calculate_totals","resolved_input":{"column":"D"}}]}'
    )
    provider = LlmStepProposalProvider(client, temperature=0.2, max_tokens=500)

    proposal = provider.propose(workflow, draft_plan)

    assert proposal is not None
    assert proposal["steps"][0]["step_id"] == "calculate_totals"
    assert client.last_request is not None
    assert client.last_request.response_format == "json_object"
    assert client.last_request.temperature == 0.2
