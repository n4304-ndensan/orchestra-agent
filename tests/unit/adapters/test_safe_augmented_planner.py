from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestra_agent.adapters.planner import (
    LlmPlanner,
    LlmStepProposalProvider,
    SafeAugmentedLlmPlanner,
)
from orchestra_agent.domain.workflow import ReplanContext, Workflow
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
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    reference_file = base / "notes.txt"
    reference_file.write_text("column D is preferred", encoding="utf-8")
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
        reference_files=[str(reference_file)],
    )
    try:
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
        assert client.last_request.messages[1].attachments[0].path == str(reference_file)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_proposal_provider_includes_replan_context() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=2,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
        feedback_history=["Need a revised export step."],
        replan_context=ReplanContext(
            trigger="feedback",
            change_summary="Review the source workflow and patch the export step only.",
            source_workflow_document="<workflow id=\"wf-1\" version=\"1\" />",
            source_step_plan_document='{"step_plan_id":"sp-1"}',
        ),
    )
    draft_plan = LlmPlanner().compile_step_plan(workflow)
    client = FakeLlmClient(
        response_text='{"steps":[{"step_id":"save_file","description":"Export the reviewed file"}]}'
    )
    provider = LlmStepProposalProvider(client)

    provider.propose(workflow, draft_plan)

    assert client.last_request is not None
    request_body = client.last_request.messages[1].content
    assert '"replan_context"' in request_body
    assert '"trigger": "feedback"' in request_body
    assert "<workflow id=\\\"wf-1\\\" version=\\\"1\\\" />" in request_body


def test_llm_step_proposal_provider_localizes_system_prompt() -> None:
    workflow = Workflow(
        workflow_id="wf-lang",
        name="Localized patch prompt",
        version=1,
        objective="Revise the draft plan.",
    )
    draft_plan = LlmPlanner().compile_step_plan(workflow)
    client = FakeLlmClient(response_text='{"steps":[]}')
    provider = LlmStepProposalProvider(client, language="es")

    provider.propose(workflow, draft_plan)

    assert client.last_request is not None
    assert "Usa espanol para el razonamiento en lenguaje natural" in (
        client.last_request.messages[0].content
    )
