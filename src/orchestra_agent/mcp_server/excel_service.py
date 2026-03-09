from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any


class ExcelWorkspaceService:
    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def open_file(self, path: str) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=False)
        try:
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet_names": list(workbook.sheetnames),
                "active_sheet": workbook.active.title,
            }
        finally:
            workbook.close()

    def read_sheet(self, path: str, sheet: str) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=True)
        try:
            worksheet = self._get_sheet(workbook, sheet)
            rows: list[dict[str, Any]] = []
            for row in worksheet.iter_rows():
                row_payload = {
                    cell.column_letter: cell.value
                    for cell in row
                    if cell.value is not None
                }
                if row_payload:
                    rows.append(row_payload)
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet": sheet,
                "rows": rows,
                "row_count": len(rows),
            }
        finally:
            workbook.close()

    def calculate_sum(
        self,
        path: str,
        sheet: str,
        column: str,
        start_row: int | None = None,
        end_row: int | None = None,
    ) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=True)
        try:
            worksheet = self._get_sheet(workbook, sheet)
            column_letter = self._normalize_column(column)
            effective_start = start_row or self._infer_start_row(worksheet, column_letter)
            effective_end = end_row or worksheet.max_row

            total = 0.0
            counted_cells = 0
            ignored_cells = 0
            for row_index in range(effective_start, effective_end + 1):
                value = worksheet[f"{column_letter}{row_index}"].value
                numeric = self._coerce_number(value)
                if numeric is None:
                    if value is not None:
                        ignored_cells += 1
                    continue
                total += numeric
                counted_cells += 1

            normalized_total: int | float = int(total) if total.is_integer() else total

            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet": sheet,
                "column": column_letter,
                "start_row": effective_start,
                "end_row": effective_end,
                "total": normalized_total,
                "counted_cells": counted_cells,
                "ignored_cells": ignored_cells,
            }
        finally:
            workbook.close()

    def create_sheet(self, path: str, sheet: str, overwrite: bool = False) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=False)
        try:
            created = False
            if sheet in workbook.sheetnames:
                if overwrite:
                    existing = workbook[sheet]
                    workbook.remove(existing)
                    workbook.create_sheet(title=sheet)
                    created = True
            else:
                workbook.create_sheet(title=sheet)
                created = True

            workbook.save(workbook_path)
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet": sheet,
                "created": created,
            }
        finally:
            workbook.close()

    def write_cells(self, path: str, sheet: str, cells: dict[str, Any]) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=False)
        try:
            worksheet = (
                workbook[sheet]
                if sheet in workbook.sheetnames
                else workbook.create_sheet(sheet)
            )
            for cell_ref, value in cells.items():
                if not isinstance(cell_ref, str) or not cell_ref.strip():
                    raise ValueError("cells keys must be non-empty Excel cell references.")
                worksheet[cell_ref] = value
            workbook.save(workbook_path)
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet": sheet,
                "written_cells": len(cells),
            }
        finally:
            workbook.close()

    def save_file(self, path: str, output: str, overwrite: bool = True) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        output_path = self._resolve_path_inside_workspace(output)
        if output_path.exists() and not overwrite and output_path != workbook_path:
            raise FileExistsError(
                f"Output file '{output}' already exists. Set overwrite=True to replace it."
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path == workbook_path:
            workbook = self._load_workbook(workbook_path, data_only=False)
            try:
                workbook.save(workbook_path)
            finally:
                workbook.close()
        else:
            shutil.copy2(workbook_path, output_path)

        return {
            "file": workbook_path.relative_to(self._workspace_root).as_posix(),
            "output": output_path.relative_to(self._workspace_root).as_posix(),
        }

    def _resolve_workbook_path(self, relative_path: str) -> Path:
        workbook_path = self._resolve_path_inside_workspace(relative_path)
        if not workbook_path.exists():
            raise FileNotFoundError(f"Workbook '{relative_path}' does not exist.")
        if workbook_path.suffix.lower() != ".xlsx":
            raise ValueError(f"Workbook '{relative_path}' must be an .xlsx file.")
        if not workbook_path.is_file():
            raise IsADirectoryError(f"Workbook path '{relative_path}' is not a file.")
        return workbook_path

    def _resolve_path_inside_workspace(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise PermissionError(
                f"Path '{relative_path}' is outside workspace root '{self._workspace_root}'."
            ) from exc
        return resolved

    @staticmethod
    def _normalize_column(column: str) -> str:
        normalized = column.strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}", normalized):
            raise ValueError(f"Invalid Excel column reference: '{column}'.")
        return normalized

    @staticmethod
    def _infer_start_row(worksheet: Any, column_letter: str) -> int:
        first_value = worksheet[f"{column_letter}1"].value
        if first_value is None:
            return 1
        if ExcelWorkspaceService._coerce_number(first_value) is None:
            return 2
        return 1

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip().replace(",", "")
            if not stripped:
                return None
            try:
                return float(stripped)
            except ValueError:
                return None
        return None

    @staticmethod
    def _get_sheet(workbook: Any, sheet: str) -> Any:
        if sheet not in workbook.sheetnames:
            raise KeyError(f"Worksheet '{sheet}' does not exist.")
        return workbook[sheet]

    @staticmethod
    def _load_workbook(path: Path, data_only: bool) -> Any:
        try:
            from openpyxl import load_workbook  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "Missing dependency 'openpyxl'. Install optional extras with "
                "`pip install \"orchestra-agent[mcp-server]\"`."
            ) from exc
        return load_workbook(filename=path, data_only=data_only)
