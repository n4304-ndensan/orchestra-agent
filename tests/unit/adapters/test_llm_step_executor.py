from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.adapters import InMemoryAuditLogger
from orchestra_agent.adapters.execution import LlmStepExecutor
from orchestra_agent.domain import Step, Workflow
from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.observability import bind_observation_context
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


class FailingToolDiscoveryMcpClient(FakeMcpClient):
    def list_tools(self) -> list[str]:
        raise RuntimeError("MCP endpoint request failed for tools/list: http://127.0.0.1:8010/mcp")

    def describe_tools(self) -> list[dict[str, str]]:
        raise RuntimeError("MCP endpoint request failed for tools/list: http://127.0.0.1:8010/mcp")


class CountingToolDiscoveryMcpClient(FakeMcpClient):
    def __init__(self) -> None:
        super().__init__()
        self.describe_calls = 0

    def describe_tools(self) -> list[dict[str, str]]:
        self.describe_calls += 1
        return super().describe_tools()


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
                  "type": "write_file",
                  "path": "reports/summary.txt",
                  "content": "done"
                }
                """,
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.read_sheet",
                  "input": {"file": "sales.xlsx", "sheet": "Sheet1"}
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Wrote the report file and inspected the worksheet.",
                    "written_files": ["reports/summary.txt"],
                    "source_sheet": "Sheet1"
                  }
                }
                """,
            ]
        )
        audit_logger = InMemoryAuditLogger()
        executor = LlmStepExecutor(client, workspace_root=base, audit_logger=audit_logger)
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

        with bind_observation_context(run_id="run-1", step_id=step.step_id):
            result = executor.execute(
                workflow=workflow,
                step=step,
                resolved_input=step.resolved_input,
                step_results={},
                mcp_client=mcp_client,
            )

        assert result == {
            "status": "ok",
            "summary": "Wrote the report file and inspected the worksheet.",
            "written_files": ["reports/summary.txt"],
            "source_sheet": "Sheet1",
        }
        assert (base / "reports" / "summary.txt").read_text(encoding="utf-8") == "done"
        assert mcp_client.calls == [
            ("excel.read_sheet", {"file": "sales.xlsx", "sheet": "Sheet1"})
        ]
        assert len(client.requests) == 3
        assert '"write_file_result"' in client.requests[1].messages[-1].content
        assert '"tool_result"' in client.requests[2].messages[-1].content
        assert client.requests[0].messages[1].attachments[0].path == str(reference_file.resolve())
        assert '"description": "Read worksheet rows as dictionaries keyed by column letters."' in (
            client.requests[0].messages[1].content
        )
        file_write_event = [
            event
            for event in audit_logger.events
            if event["event_type"] == "workspace_file_written"
        ][-1]
        assert file_write_event["run_id"] == "run-1"
        assert file_write_event["path"].endswith("reports\\summary.txt")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_applies_batched_actions_in_single_round() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "actions": [
                    {
                      "type": "call_mcp_tool",
                      "tool_ref": "excel.create_file",
                      "input": {"file": "output/HelloWorld.xlsx"}
                    },
                    {
                      "type": "call_mcp_tool",
                      "tool_ref": "excel.write_cells",
                      "input": {
                        "file": "output/HelloWorld.xlsx",
                        "sheet": "Sheet1",
                        "cells": {"A1": "HelloWorld"}
                      }
                    },
                    {
                      "type": "call_mcp_tool",
                      "tool_ref": "excel.save_file",
                      "input": {
                        "file": "output/HelloWorld.xlsx",
                        "output": "output/HelloWorld.xlsx"
                      }
                    },
                    {
                      "type": "finish",
                      "result": {
                        "status": "completed",
                        "summary": "Created, updated, and saved the workbook.",
                        "output_file": "output/HelloWorld.xlsx"
                      }
                    }
                  ]
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-batch",
            name="Batched Excel write",
            version=1,
            objective="Create and save an Excel workbook quickly.",
        )
        step = Step(
            step_id="create_excel_file",
            name="Create Excel file",
            description="Create and populate an Excel workbook.",
            tool_ref="orchestra.llm_execute",
            resolved_input={
                "allowed_mcp_tools": [
                    "excel.create_file",
                    "excel.write_cells",
                    "excel.save_file",
                ]
            },
        )
        mcp_client = FakeMcpClient()

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert result == {
            "status": "completed",
            "summary": "Created, updated, and saved the workbook.",
            "output_file": "output/HelloWorld.xlsx",
        }
        assert mcp_client.calls == [
            ("excel.create_file", {"file": "output/HelloWorld.xlsx"}),
            (
                "excel.write_cells",
                {
                    "file": "output/HelloWorld.xlsx",
                    "sheet": "Sheet1",
                    "cells": {"A1": "HelloWorld"},
                },
            ),
            (
                "excel.save_file",
                {
                    "file": "output/HelloWorld.xlsx",
                    "output": "output/HelloWorld.xlsx",
                },
            ),
        ]
        assert len(client.requests) == 1
        assert '"compact_multi_action_batch"' in client.requests[0].messages[1].content
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
                  "type": "request_file_attachments",
                  "paths": ["specs/requirements.txt"],
                  "reason": "Need the requirements document"
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "used-attachment",
                    "summary": "Reviewed the attached requirements file."
                  }
                }
                """,
            ]
        )
        audit_logger = InMemoryAuditLogger()
        executor = LlmStepExecutor(client, workspace_root=base, audit_logger=audit_logger)
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

        with bind_observation_context(run_id="run-attach", step_id=step.step_id):
            result = executor.execute(
                workflow=workflow,
                step=step,
                resolved_input=step.resolved_input,
                step_results={},
                mcp_client=FakeMcpClient(),
            )

        assert result == {
            "status": "used-attachment",
            "summary": "Reviewed the attached requirements file.",
        }
        assert len(client.requests) == 2
        assert client.requests[0].messages[1].attachments == ()
        assert client.requests[1].messages[-1].attachments[0].path == str(requested_file.resolve())
        attachment_event = [
            event
            for event in audit_logger.events
            if event["event_type"] == "llm_attachment_requested"
        ][-1]
        assert attachment_event["paths"] == ["specs/requirements.txt"]
        assert attachment_event["run_id"] == "run-attach"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_supports_attachment_requests_inside_actions_payload() -> None:
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
                  "type": "finish",
                  "result": {
                    "status": "used-attachment",
                    "summary": "Reviewed the attached requirements file."
                  }
                }
                """,
            ]
        )
        audit_logger = InMemoryAuditLogger()
        executor = LlmStepExecutor(client, workspace_root=base, audit_logger=audit_logger)
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

        with bind_observation_context(run_id="run-attach-actions", step_id=step.step_id):
            result = executor.execute(
                workflow=workflow,
                step=step,
                resolved_input=step.resolved_input,
                step_results={},
                mcp_client=FakeMcpClient(),
            )

        assert result == {
            "status": "used-attachment",
            "summary": "Reviewed the attached requirements file.",
        }
        assert len(client.requests) == 2
        assert client.requests[1].messages[-1].attachments[0].path == str(requested_file.resolve())
        attachment_event = [
            event
            for event in audit_logger.events
            if event["event_type"] == "llm_attachment_requested"
        ][-1]
        assert attachment_event["run_id"] == "run-attach-actions"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_rejects_paths_outside_workspace_then_allows_correction() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "write_file",
                  "path": "../outside.txt",
                  "content": "nope"
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "corrected",
                    "summary": "Adjusted after sandbox rejection."
                  }
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

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "corrected",
            "summary": "Adjusted after sandbox rejection.",
        }
        assert '"kind": "workspace_sandbox"' in client.requests[1].messages[-1].content
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_supports_ai_review_steps() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "program.py"
        source_file.write_text("print('hello')", encoding="utf-8")
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "reviewed",
                    "summary": "No critical issues"
                  }
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-review",
            name="Review program",
            version=1,
            objective="Review a program file",
        )
        step = Step(
            step_id="review_program",
            name="Review program",
            description="Review the source file and summarize issues.",
            tool_ref="orchestra.ai_review",
            resolved_input={"llm_reference_files": ["program.py"]},
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=FakeMcpClient(),
        )

        assert result == {"status": "reviewed", "summary": "No critical issues"}
        assert "orchestra.ai_review" in client.requests[0].messages[1].content
        assert client.requests[0].messages[1].attachments[0].path == str(source_file.resolve())
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_includes_workflow_constraints_and_step_contract_in_prompt() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Used the full execution payload."
                  }
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-contract",
            name="Execution contract",
            version=3,
            objective="Create the requested summary artifact.",
            constraints=["Do not overwrite the original workbook."],
            success_criteria=["Create reports/summary.txt inside the workspace."],
            feedback_history=["Prefer concise output."],
        )
        step = Step(
            step_id="create_summary",
            name="Create summary",
            description="Create the summary artifact from the prepared findings.",
            tool_ref="orchestra.llm_execute",
            resolved_input={
                "target_files": ["reports/summary.txt"],
                "prior_step_result_requirements": ["prepared_findings"],
            },
            depends_on=["prepared_findings"],
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            backup_scope=BackupScope.WORKSPACE,
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={"prepared_findings": {"summary": "Draft findings are ready."}},
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "ok",
            "summary": "Used the full execution payload.",
        }
        system_prompt = client.requests[0].messages[0].content
        request_body = client.requests[0].messages[1].content
        assert "Work on the current step only. Do not re-plan the workflow." in system_prompt
        assert "Use write_file only for UTF-8 text files inside workspace_root." in system_prompt
        assert '"constraints": [' in request_body
        assert '"Do not overwrite the original workbook."' in request_body
        assert '"success_criteria": [' in request_body
        assert '"Create reports/summary.txt inside the workspace."' in request_body
        assert '"depends_on": [' in request_body
        assert '"risk_level": "MEDIUM"' in request_body
        assert '"requires_approval": true' in request_body
        assert '"backup_scope": "WORKSPACE"' in request_body
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_includes_previous_step_results_in_next_step_prompt() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Used the prior step summary."
                  }
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-next-step",
            name="Next step input",
            version=1,
            objective="Use previous step results",
        )
        step = Step(
            step_id="finalize_report",
            name="Finalize report",
            description="Finalize the output using the previous step summary.",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={
                "prepare_report": {
                    "summary": "Workbook inspected and report drafted.",
                    "output_file": "reports/summary.txt",
                }
            },
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "ok",
            "summary": "Used the prior step summary.",
        }
        request_body = client.requests[0].messages[1].content
        assert '"prepare_report"' in request_body
        assert '"output_file": "reports/summary.txt"' in request_body
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_sanitizes_workspace_paths_in_prompt() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        workbook = (base / "output" / "HelloWorld.xlsx").resolve()
        workbook.parent.mkdir(parents=True, exist_ok=True)
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Prompt paths were normalized."
                  }
                }
                """
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-paths",
            name="Prompt path normalization",
            version=1,
            objective="Normalize workspace paths",
        )
        step = Step(
            step_id="normalize_paths",
            name="Normalize paths",
            description="Use workspace-relative paths in the LLM prompt.",
            tool_ref="orchestra.llm_execute",
            resolved_input={
                "file": str(workbook),
                "output": str(workbook),
            },
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={
                "prepare_workbook": {
                    "output_file": str(workbook),
                }
            },
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "ok",
            "summary": "Prompt paths were normalized.",
        }
        request_body = client.requests[0].messages[1].content
        assert '"file": "output/HelloWorld.xlsx"' in request_body
        assert '"output": "output/HelloWorld.xlsx"' in request_body
        assert '"output_file": "output/HelloWorld.xlsx"' in request_body
        assert str(workbook) not in request_body
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_repairs_invalid_json_backslashes_from_windows_paths() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        raw_windows_path = r"C:\Users\syogo\Documents\sales.xlsx"
        client = FakeLlmClient(
            [
                (
                    '{"type":"call_mcp_tool","tool_ref":"excel.read_sheet","input":'
                    '{"file":"C:\\Users\\syogo\\Documents\\sales.xlsx","sheet":"Sheet1"}}'
                ),
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Recovered invalid JSON escapes."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-invalid-json",
            name="Repair invalid escapes",
            version=1,
            objective="Handle Windows paths safely",
        )
        step = Step(
            step_id="repair_invalid_json",
            name="Repair invalid JSON",
            description="Recover from common Windows path escaping mistakes.",
            tool_ref="orchestra.llm_execute",
            resolved_input={"allowed_mcp_tools": ["excel.read_sheet"]},
        )
        mcp_client = FakeMcpClient()

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert result == {
            "status": "ok",
            "summary": "Recovered invalid JSON escapes.",
        }
        assert mcp_client.calls == [
            ("excel.read_sheet", {"file": raw_windows_path, "sheet": "Sheet1"})
        ]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_normalizes_excel_alias_inputs_before_mcp_call() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.create_file",
                  "input": {"path": "output/HelloWorld.xlsx"}
                }
                """,
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.write_cells",
                  "input": {
                    "path": "output/HelloWorld.xlsx",
                    "sheet_name": "Sheet1",
                    "cells": {"A1": "HelloWorld"}
                  }
                }
                """,
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.save_file",
                  "input": {"path": "output/HelloWorld.xlsx"}
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "completed",
                    "summary": "Created, updated, and saved the workbook.",
                    "output_file": "output/HelloWorld.xlsx"
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-1",
            name="Excel alias repair",
            version=1,
            objective="Normalize Excel MCP aliases",
        )
        step = Step(
            step_id="create_excel_file",
            name="Create Excel file",
            description="Create and populate an Excel workbook.",
            tool_ref="orchestra.llm_execute",
            resolved_input={
                "allowed_mcp_tools": [
                    "excel.create_file",
                    "excel.write_cells",
                    "excel.save_file",
                ]
            },
        )
        mcp_client = FakeMcpClient()

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert mcp_client.calls == [
            ("excel.create_file", {"file": "output/HelloWorld.xlsx"}),
            (
                "excel.write_cells",
                {
                    "file": "output/HelloWorld.xlsx",
                    "sheet": "Sheet1",
                    "cells": {"A1": "HelloWorld"},
                },
            ),
            (
                "excel.save_file",
                {
                    "file": "output/HelloWorld.xlsx",
                    "output": "output/HelloWorld.xlsx",
                },
            ),
        ]
        assert result == {
            "status": "completed",
            "summary": "Created, updated, and saved the workbook.",
            "output_file": "output/HelloWorld.xlsx",
        }
        assert len(client.requests) == 4
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_recovers_when_model_returns_steps_payload() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "steps": [
                    {
                      "step_id": "wrong",
                      "name": "Wrong payload"
                    }
                  ]
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Recovered after runtime error."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-recover",
            name="Recover runtime output",
            version=1,
            objective="Finish the step after an invalid action payload.",
        )
        step = Step(
            step_id="recover_runtime",
            name="Recover runtime",
            description="Return runtime actions only.",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "ok",
            "summary": "Recovered after runtime error.",
        }
        assert len(client.requests) == 2
        assert '"runtime_error"' in client.requests[1].messages[-1].content
        assert '"kind": "returned_step_plan"' in client.requests[1].messages[-1].content
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_continues_when_tool_catalog_lookup_fails() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.read_sheet",
                  "input": {"file": "sales.xlsx", "sheet": "Sheet1"}
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Continued with explicit allowed_mcp_tools."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-tool-warning",
            name="Tool warning",
            version=1,
            objective="Keep the LLM runtime active even when tool discovery fails.",
        )
        step = Step(
            step_id="inspect_workbook",
            name="Inspect workbook",
            description="Inspect the workbook via the LLM runtime.",
            tool_ref="orchestra.llm_execute",
            resolved_input={"allowed_mcp_tools": ["excel.read_sheet"]},
        )
        mcp_client = FailingToolDiscoveryMcpClient()

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert result == {
            "status": "ok",
            "summary": "Continued with explicit allowed_mcp_tools.",
        }
        assert mcp_client.calls == [
            ("excel.read_sheet", {"file": "sales.xlsx", "sheet": "Sheet1"})
        ]
        assert len(client.requests) == 2
        assert '"mcp_tool_catalog_warning"' in client.requests[0].messages[1].content
        assert '"excel.read_sheet"' in client.requests[0].messages[1].content
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_sends_incremental_turns_when_context_is_remembered() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "write_file",
                  "path": "reports/summary.txt",
                  "content": "done"
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Used remembered context."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(
            client,
            workspace_root=base,
            remembers_context=True,
            language="ja",
        )
        workflow = Workflow(
            workflow_id="wf-memory",
            name="Remembered context",
            version=1,
            objective="Use incremental runtime turns.",
        )
        step = Step(
            step_id="memory_step",
            name="Memory step",
            description="Write a file and finish.",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )

        result = executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=FakeMcpClient(),
        )

        assert result == {
            "status": "ok",
            "summary": "Used remembered context.",
        }
        assert len(client.requests[0].messages) == 2
        assert len(client.requests[1].messages) == 1
        assert '"write_file_result"' in client.requests[1].messages[0].content
        assert "この runtime は、現在の会話で model が前の turn を記憶している前提です" in (
            client.requests[0].messages[0].content
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_reuses_cached_workspace_index_between_steps() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        specs_dir = base / "specs"
        specs_dir.mkdir()
        (specs_dir / "requirements.txt").write_text("content", encoding="utf-8")
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "First step finished."
                  }
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Second step finished."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        scan_count = 0
        original_scan = executor._workspace_inventory._scan_roots

        def counting_scan(
            *,
            roots: tuple[Path, ...],
            indexed_files: list[dict[str, object]],
            indexed_paths: set[str],
        ) -> None:
            nonlocal scan_count
            scan_count += 1
            original_scan(
                roots=roots,
                indexed_files=indexed_files,
                indexed_paths=indexed_paths,
            )

        executor._workspace_inventory._scan_roots = counting_scan
        workflow = Workflow(
            workflow_id="wf-cache",
            name="Cache workspace index",
            version=1,
            objective="Reuse the workspace file index across steps.",
        )
        step = Step(
            step_id="cache_step",
            name="Cache step",
            description="Finish immediately.",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )
        mcp_client = FakeMcpClient()

        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )
        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert scan_count == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_invalidates_workspace_index_after_mcp_tool_call() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Warm cache."
                  }
                }
                """,
                """
                {
                  "type": "call_mcp_tool",
                  "tool_ref": "excel.read_sheet",
                  "input": {"file": "sales.xlsx", "sheet": "Sheet1"}
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Tool call finished."
                  }
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Cache rebuilt."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        scan_count = 0
        original_scan = executor._workspace_inventory._scan_roots

        def counting_scan(
            *,
            roots: tuple[Path, ...],
            indexed_files: list[dict[str, object]],
            indexed_paths: set[str],
        ) -> None:
            nonlocal scan_count
            scan_count += 1
            original_scan(
                roots=roots,
                indexed_files=indexed_files,
                indexed_paths=indexed_paths,
            )

        executor._workspace_inventory._scan_roots = counting_scan
        workflow = Workflow(
            workflow_id="wf-invalidate",
            name="Invalidate cache",
            version=1,
            objective="Rebuild the workspace index after tool side effects.",
        )
        step = Step(
            step_id="invalidate_step",
            name="Invalidate step",
            description="Call a tool, then finish.",
            tool_ref="orchestra.llm_execute",
            resolved_input={"allowed_mcp_tools": ["excel.read_sheet"]},
        )
        mcp_client = FakeMcpClient()

        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )
        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )
        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert scan_count == 2
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_llm_step_executor_caches_tool_catalog_between_steps() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        client = FakeLlmClient(
            [
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "First step finished."
                  }
                }
                """,
                """
                {
                  "type": "finish",
                  "result": {
                    "status": "ok",
                    "summary": "Second step finished."
                  }
                }
                """,
            ]
        )
        executor = LlmStepExecutor(client, workspace_root=base)
        workflow = Workflow(
            workflow_id="wf-tools",
            name="Cache tool catalog",
            version=1,
            objective="Reuse tool discovery between steps.",
        )
        step = Step(
            step_id="tool_cache_step",
            name="Tool cache step",
            description="Finish immediately.",
            tool_ref="orchestra.llm_execute",
            resolved_input={},
        )
        mcp_client = CountingToolDiscoveryMcpClient()

        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )
        executor.execute(
            workflow=workflow,
            step=step,
            resolved_input=step.resolved_input,
            step_results={},
            mcp_client=mcp_client,
        )

        assert mcp_client.describe_calls == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)
