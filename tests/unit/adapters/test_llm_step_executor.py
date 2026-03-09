from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.adapters.execution import LlmStepExecutor
from orchestra_agent.domain import Step, Workflow
from orchestra_agent.domain.enums import BackupScope, RiskLevel
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


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_tools(self) -> list[str]:
        return ["excel.read_sheet"]

    def describe_tools(self) -> list[dict[str, str]]:
        return [
            {
                "name": "excel.read_sheet",
                "description": "Read worksheet rows as dictionaries keyed by column letters.",
            }
        ]

    def call_tool(self, tool_ref: str, input: dict[str, object]) -> dict[str, object]:
        self.calls.append((tool_ref, input))
        return {"tool_ref": tool_ref, "input": input}


def test_llm_step_executor_applies_workspace_edits_and_mcp_calls() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        reference_file = base / "requirements.txt"
        reference_file.write_text("Use the attached notes.", encoding="utf-8")
        client = FakeLlmClient(
            [
                """
                {
                  "actions": [
                    {
                      "type": "write_file",
                      "path": "reports/summary.txt",
                      "content": "done"
                    },
                    {
                      "type": "call_mcp_tool",
                      "tool_ref": "excel.read_sheet",
                      "input": {"file": "sales.xlsx", "sheet": "Sheet1"}
                    }
                  ],
                  "result": {"status": "ok"}
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-1",
            name="Workspace summary",
            version=1,
            objective="Create report",
            reference_files=[str(reference_file)],
        )
        step = Step(
            step_id="orchestrate",
            name="Orchestrate report",
            description="Read and write files",
            tool_ref="orchestra.llm_execute",
            resolved_input={"allowed_mcp_tools": ["excel.read_sheet"]},
            risk_level=RiskLevel.MEDIUM,
            backup_scope=BackupScope.WORKSPACE,
        )
        mcp_client = FakeMcpClient()

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert result == {"status": "ok"}
        assert (base / "reports" / "summary.txt").read_text(encoding="utf-8") == "done"
        assert mcp_client.calls == [
            ("excel.read_sheet", {"file": "sales.xlsx", "sheet": "Sheet1"})
        ]
        assert client.requests[0].messages[1].attachments[0].path == str(reference_file.resolve())
        assert '"description": "Read worksheet rows as dictionaries keyed by column letters."' in (
            client.requests[0].messages[1].content
        )
    finally:
        for child in sorted(base.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        base.rmdir()


def test_llm_step_executor_can_attach_workspace_files_on_demand() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        specs_dir = base / "specs"
        specs_dir.mkdir()
        requested_file = specs_dir / "requirements.txt"
        requested_file.write_text("Use this attached file.", encoding="utf-8")
        client = FakeLlmClient(
            [
                """
                {
                  "actions": [
                    {
                      "type": "request_file_attachments",
                      "paths": ["specs/requirements.txt"],
                      "reason": "Need the requirements document"
                    }
                  ]
                }
                """,
                """
                {
                  "actions": [],
                  "result": {"status": "used-attachment"}
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-1",
            name="Workspace summary",
            version=1,
            objective="Create report",
        )
        step = Step(
            step_id="orchestrate",
            name="Orchestrate report",
            description="Read and write files",
            tool_ref="orchestra.llm_execute",
            resolved_input={"allowed_mcp_tools": ["excel.read_sheet"]},
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=FakeMcpClient(),
        )

        assert result == {"status": "used-attachment"}
        assert len(client.requests) == 2
        assert client.requests[0].messages[1].attachments == ()
        assert client.requests[1].messages[1].attachments[0].path == str(requested_file.resolve())
    finally:
        for child in sorted(base.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        base.rmdir()


def test_llm_step_executor_rejects_paths_outside_workspace() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "actions": [
                    {
                      "type": "write_file",
                      "path": "../outside.txt",
                      "content": "nope"
                    }
                  ]
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-1",
            name="Workspace summary",
            version=1,
            objective="Create report",
        )
        step = Step(
            step_id="orchestrate",
            name="Orchestrate report",
            description="Read and write files",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )

        with pytest.raises(ValueError, match="Workspace sandbox rejected path"):
            executor.execute(
                workflow=workflow,
                step=step,
                resolved_input=step.resolved_input,
                step_results={},
                mcp_client=FakeMcpClient(),
            )
    finally:
        base.rmdir()
