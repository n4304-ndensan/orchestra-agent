from __future__ import annotations

import re
from dataclasses import dataclass
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


class LlmPlanner(IPlanner):
    """
    Deterministic planner adapter with LLM-compatible interface.
    In production this adapter can delegate to an LLM and then validate output.
    """

    def __init__(self, defaults: PlannerDefaults | None = None) -> None:
        self._defaults = defaults or PlannerDefaults()

    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        objective = workflow.objective
        source_file = self._extract_excel_file(objective) or "input.xlsx"
        source_sheet = self._extract_sheet(objective) or self._defaults.source_sheet
        target_column = self._extract_column(objective) or self._defaults.target_column
        summary_sheet = self._defaults.summary_sheet
        output_file = self._extract_output_file(objective, source_file)

        steps = [
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

        return StepPlan(
            step_plan_id=f"sp-{uuid4().hex[:10]}",
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            steps=steps,
        )

    @staticmethod
    def _extract_excel_file(text: str) -> str | None:
        match = re.search(r"([A-Za-z0-9_.-]+\.xlsx)", text)
        return match.group(1) if match is not None else None

    @staticmethod
    def _extract_output_file(text: str, default_input: str) -> str:
        export_match = re.search(
            r"(?:export|save|output)\s+(?:as\s+)?([A-Za-z0-9_.-]+\.xlsx)",
            text,
            re.I,
        )
        if export_match is not None:
            return export_match.group(1)
        stem = default_input.rsplit(".", maxsplit=1)[0]
        return f"{stem}_summary.xlsx"

    @staticmethod
    def _extract_sheet(text: str) -> str | None:
        match = re.search(r"sheet\s*[:=]?\s*([A-Za-z0-9_ -]+)", text, re.I)
        if match is None:
            return None
        sheet = match.group(1).strip()
        return sheet if sheet else None

    @staticmethod
    def _extract_column(text: str) -> str | None:
        match = re.search(r"column\s*([A-Za-z]{1,3})", text, re.I)
        if match is None:
            return None
        return match.group(1).upper()
