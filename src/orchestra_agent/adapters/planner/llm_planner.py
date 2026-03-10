from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.domain.step import Step
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.planner import IPlanner


@dataclass(slots=True)
class PlannerDefaults:
    source_sheet: str = "Sheet1"
    summary_sheet: str = "Summary"
    target_column: str = "C"


@dataclass(slots=True, frozen=True)
class CellWriteRequest:
    file: str
    output: str
    sheet: str
    cell: str
    value: str
    create_file: bool


class LlmPlanner(IPlanner):
    """
    Deterministic planner adapter with LLM-compatible interface.
    In production this adapter can delegate to an LLM and then validate output.
    """

    def __init__(self, defaults: PlannerDefaults | None = None) -> None:
        self._defaults = defaults or PlannerDefaults()

    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        objective = workflow.objective
        cell_write_request = self._extract_cell_write_request(objective)
        if cell_write_request is not None:
            steps = self._build_cell_write_steps(cell_write_request)
        else:
            source_file = self._extract_excel_file(objective) or "input.xlsx"
            source_sheet = self._extract_sheet(objective) or self._defaults.source_sheet
            target_column = self._extract_column(objective) or self._defaults.target_column
            summary_sheet = self._defaults.summary_sheet
            output_file = self._extract_output_file(objective, source_file)
            steps = self._build_summary_steps(
                source_file=source_file,
                source_sheet=source_sheet,
                target_column=target_column,
                summary_sheet=summary_sheet,
                output_file=output_file,
            )

        return StepPlan(
            step_plan_id=f"sp-{uuid4().hex[:10]}",
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            steps=steps,
        )

    def _build_summary_steps(
        self,
        *,
        source_file: str,
        source_sheet: str,
        target_column: str,
        summary_sheet: str,
        output_file: str,
    ) -> list[Step]:
        return [
            Step(
                step_id="open_file",
                name="Open Excel file",
                description=f"Open workbook {source_file}",
                tool_ref="excel.open_file",
                resolved_input={"file": source_file},
                risk_level=RiskLevel.LOW,
                backup_scope=BackupScope.NONE,
            ),
            Step(
                step_id="read_sheet",
                name="Read worksheet",
                description=f"Read sheet {source_sheet}",
                tool_ref="excel.read_sheet",
                resolved_input={"file": source_file, "sheet": source_sheet},
                depends_on=["open_file"],
                risk_level=RiskLevel.LOW,
                backup_scope=BackupScope.NONE,
            ),
            Step(
                step_id="calculate_totals",
                name="Calculate totals",
                description=f"Sum values in column {target_column}",
                tool_ref="excel.calculate_sum",
                resolved_input={
                    "file": source_file,
                    "sheet": source_sheet,
                    "column": target_column,
                },
                depends_on=["read_sheet"],
                risk_level=RiskLevel.MEDIUM,
                backup_scope=BackupScope.NONE,
            ),
            Step(
                step_id="create_summary_sheet",
                name="Create summary worksheet",
                description=f"Create sheet {summary_sheet}",
                tool_ref="excel.create_sheet",
                resolved_input={
                    "file": source_file,
                    "sheet": summary_sheet,
                },
                depends_on=["calculate_totals"],
                risk_level=RiskLevel.MEDIUM,
                backup_scope=BackupScope.FILE,
            ),
            Step(
                step_id="write_summary",
                name="Write summary result",
                description="Write total to summary sheet",
                tool_ref="excel.write_cells",
                resolved_input={
                    "file": source_file,
                    "sheet": summary_sheet,
                    "cells": {
                        "A1": "Column",
                        "B1": "Total",
                        "A2": target_column,
                        "B2": "{{calculate_totals.total}}",
                    },
                },
                depends_on=["create_summary_sheet"],
                risk_level=RiskLevel.MEDIUM,
                backup_scope=BackupScope.FILE,
            ),
            Step(
                step_id="save_file",
                name="Export output file",
                description=f"Save summary file as {output_file}",
                tool_ref="excel.save_file",
                resolved_input={
                    "file": source_file,
                    "output": output_file,
                },
                depends_on=["write_summary"],
                risk_level=RiskLevel.HIGH,
                requires_approval=True,
                backup_scope=BackupScope.FILE,
            ),
        ]

    def _build_cell_write_steps(self, request: CellWriteRequest) -> list[Step]:
        opening_step = Step(
            step_id="create_excel_file" if request.create_file else "open_file",
            name="Create workbook" if request.create_file else "Open workbook",
            description=(
                f"Create workbook {request.file}"
                if request.create_file
                else f"Open workbook {request.file}"
            ),
            tool_ref="excel.create_file" if request.create_file else "excel.open_file",
            resolved_input=(
                {"file": request.file, "sheet": request.sheet}
                if request.create_file
                else {"file": request.file}
            ),
            risk_level=RiskLevel.LOW,
            backup_scope=BackupScope.NONE,
        )

        return [
            opening_step,
            Step(
                step_id="write_cells",
                name="Write worksheet cells",
                description=f"Write {request.value} to {request.sheet}!{request.cell}",
                tool_ref="excel.write_cells",
                resolved_input={
                    "file": request.file,
                    "sheet": request.sheet,
                    "cells": {request.cell: request.value},
                },
                depends_on=[opening_step.step_id],
                risk_level=RiskLevel.MEDIUM,
                backup_scope=BackupScope.FILE,
            ),
            Step(
                step_id="save_file",
                name="Save workbook",
                description=f"Save workbook as {request.output}",
                tool_ref="excel.save_file",
                resolved_input={
                    "file": request.file,
                    "output": request.output,
                },
                depends_on=["write_cells"],
                risk_level=RiskLevel.HIGH,
                requires_approval=True,
                backup_scope=BackupScope.FILE,
            ),
        ]

    def _extract_cell_write_request(self, text: str) -> CellWriteRequest | None:
        source_file = self._extract_excel_file(text)
        cell_ref = self._extract_cell_reference(text)
        cell_value = self._extract_cell_value(text)
        if source_file is None or cell_ref is None or cell_value is None:
            return None
        sheet = self._extract_sheet(text) or self._defaults.source_sheet
        output = self._extract_output_file(text, source_file, default_output=source_file)
        return CellWriteRequest(
            file=source_file,
            output=output,
            sheet=sheet,
            cell=cell_ref,
            value=cell_value,
            create_file=self._is_create_workbook_request(text),
        )

    @staticmethod
    def _extract_excel_files(text: str) -> list[str]:
        return [
            cast(str, item)
            for item in re.findall(r"([A-Za-z0-9_./\\-]+\.xlsx)", text, re.I)
        ]

    @classmethod
    def _extract_excel_file(cls, text: str) -> str | None:
        matches = cls._extract_excel_files(text)
        if not matches:
            return None
        return cast(str, matches[0])

    @staticmethod
    def _extract_output_file(
        text: str,
        default_input: str,
        *,
        default_output: str | None = None,
    ) -> str:
        export_match = re.search(
            r"(?:export|save|output)\s+(?:as\s+)?([A-Za-z0-9_./\\-]+\.xlsx)",
            text,
            re.I,
        )
        if export_match is not None:
            return cast(str, export_match.group(1))
        all_files = LlmPlanner._extract_excel_files(text)
        if len(all_files) >= 2:
            return cast(str, all_files[-1])
        if default_output is not None:
            return default_output
        stem = default_input.rsplit(".", maxsplit=1)[0]
        return f"{stem}_summary.xlsx"

    @staticmethod
    def _extract_sheet(text: str) -> str | None:
        patterns = (
            re.compile(r"\b(Sheet[0-9A-Za-z_-]*)\b", re.I),
            re.compile(r"sheet\s*[:=]?\s*([A-Za-z0-9_-]+)", re.I),
            re.compile(r"([A-Za-z0-9_-]+)\s*シート"),
        )
        for pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue
            sheet = match.group(1).strip()
            if not sheet:
                continue
            if sheet.isdigit():
                return f"Sheet{sheet}"
            return sheet
        return None

    @staticmethod
    def _extract_column(text: str) -> str | None:
        match = re.search(r"column\s*([A-Za-z]{1,3})", text, re.I)
        if match is not None:
            return match.group(1).upper()
        jp_match = re.search(r"([A-Za-z]{1,3})\s*列", text)
        if jp_match is None:
            return None
        return jp_match.group(1).upper()

    @staticmethod
    def _extract_cell_reference(text: str) -> str | None:
        match = re.search(r"\b([A-Za-z]{1,3}[1-9][0-9]*)\b", text)
        if match is None:
            return None
        return match.group(1).upper()

    @staticmethod
    def _extract_cell_value(text: str) -> str | None:
        patterns = (
            re.compile(
                r"\b(?P<cell>[A-Za-z]{1,3}[1-9][0-9]*)\b\s*(?:セル)?\s*に\s*[\"'`“”「『]?"
                r"(?P<value>.+?)[\"'`“”」』]?\s*(?:と)?(?:書き込|入力|記入|入れ|セット)",
                re.I,
            ),
            re.compile(
                r"(?:write|put|set)\s+[\"'`“”]?(?P<value>.+?)[\"'`“”]?\s+"
                r"(?:to|into|in)\s+(?P<cell>[A-Za-z]{1,3}[1-9][0-9]*)\b",
                re.I,
            ),
            re.compile(
                r"\b(?P<cell>[A-Za-z]{1,3}[1-9][0-9]*)\b\s*(?:=|:)\s*"
                r"[\"'`“”]?(?P<value>[^,.;]+)",
                re.I,
            ),
        )
        for pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue
            value = match.group("value").strip()
            if value:
                return LlmPlanner._clean_cell_value(value)
        return None

    @staticmethod
    def _clean_cell_value(value: str) -> str:
        cleaned = value.strip().strip(".,。")
        return cleaned.strip("\"'`“”「」『』")

    @staticmethod
    def _is_create_workbook_request(text: str) -> bool:
        return bool(re.search(r"\b(create|new|make)\b|作成|新規|作る", text, re.I))
