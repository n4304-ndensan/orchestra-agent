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


def test_llm_step_executor_rejects_paths_outside_workspace() -> None:
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
