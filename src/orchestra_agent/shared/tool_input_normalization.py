from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestra_agent.domain.step_plan import StepPlan

_EXCEL_FILE_ALIASES = ("path", "workbook", "workbook_path")
_EXCEL_SHEET_ALIASES = ("sheet_name", "worksheet", "worksheet_name", "tab")
_EXCEL_OUTPUT_ALIASES = ("destination", "destination_path", "save_as", "target")
_EXCEL_IMAGE_INDEX_ALIASES = ("index", "image_number")


def normalize_tool_input(tool_ref: str, resolved_input: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(resolved_input)
    if not tool_ref.startswith("excel."):
        return normalized

    _promote_alias(normalized, "file", _EXCEL_FILE_ALIASES)
    _promote_alias(normalized, "sheet", _EXCEL_SHEET_ALIASES)

    if tool_ref == "excel.save_file":
        _promote_alias(normalized, "output", _EXCEL_OUTPUT_ALIASES)
        file_value = normalized.get("file")
        if "output" not in normalized and isinstance(file_value, str):
            normalized["output"] = file_value

    if tool_ref == "excel.extract_image":
        _promote_alias(normalized, "image_index", _EXCEL_IMAGE_INDEX_ALIASES)

    return normalized


def normalize_step_plan_inputs(step_plan: StepPlan) -> StepPlan:
    for step in step_plan.steps:
        step.resolved_input = normalize_tool_input(step.tool_ref, step.resolved_input)
    return step_plan


def _promote_alias(
    payload: dict[str, Any],
    canonical_key: str,
    aliases: tuple[str, ...],
) -> None:
    if canonical_key in payload:
        for alias in aliases:
            payload.pop(alias, None)
        return
    for alias in aliases:
        if alias in payload:
            payload[canonical_key] = payload[alias]
            break
    for alias in aliases:
        payload.pop(alias, None)
