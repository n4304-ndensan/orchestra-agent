from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile


class ExcelWorkspaceService:
    _main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    _rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    _pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    _xdr_ns = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    _a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"

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

    def read_cells(self, path: str, sheet: str, cells: list[str]) -> dict[str, Any]:
        if not cells:
            raise ValueError("cells must contain at least one cell reference.")
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=True)
        try:
            worksheet = self._get_sheet(workbook, sheet)
            resolved_cells: dict[str, Any] = {}
            for cell_ref in cells:
                normalized_ref = self._normalize_cell_ref(cell_ref)
                resolved_cells[normalized_ref] = worksheet[normalized_ref].value
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "sheet": sheet,
                "cells": resolved_cells,
            }
        finally:
            workbook.close()

    def grep_cells(
        self,
        path: str,
        pattern: str,
        *,
        sheet: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        exact: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero.")
        workbook_path = self._resolve_workbook_path(path)
        workbook = self._load_workbook(workbook_path, data_only=True)
        try:
            sheet_names = [sheet] if sheet is not None else list(workbook.sheetnames)
            matches: list[dict[str, Any]] = []
            for sheet_name in sheet_names:
                worksheet = self._get_sheet(workbook, sheet_name)
                for row in worksheet.iter_rows():
                    for cell in row:
                        if cell.value is None:
                            continue
                        text = str(cell.value)
                        if not self._matches_text(
                            text=text,
                            pattern=pattern,
                            case_sensitive=case_sensitive,
                            regex=regex,
                            exact=exact,
                        ):
                            continue
                        matches.append(
                            {
                                "sheet": sheet_name,
                                "cell": cell.coordinate,
                                "value": cell.value,
                            }
                        )
                        if len(matches) >= max_results:
                            return {
                                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                                "pattern": pattern,
                                "matches": matches,
                                "truncated": True,
                            }
            return {
                "file": workbook_path.relative_to(self._workspace_root).as_posix(),
                "pattern": pattern,
                "matches": matches,
                "truncated": False,
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

    def create_file(
        self,
        path: str,
        sheet: str = "Sheet1",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        workbook_path = self._resolve_path_inside_workspace(path)
        self._validate_workbook_extension(path, workbook_path)

        sheet_name = sheet.strip()
        if not sheet_name:
            raise ValueError("sheet must be a non-empty worksheet name.")

        existed = workbook_path.exists()
        if existed:
            if workbook_path.is_dir():
                raise IsADirectoryError(f"Workbook path '{path}' is not a file.")
            if not overwrite:
                raise FileExistsError(
                    f"Workbook '{path}' already exists. Set overwrite=True to replace it."
                )

        workbook_path.parent.mkdir(parents=True, exist_ok=True)
        workbook = self._new_workbook()
        try:
            workbook.active.title = sheet_name
            workbook.save(workbook_path)
        finally:
            workbook.close()

        return {
            "file": workbook_path.relative_to(self._workspace_root).as_posix(),
            "sheet_names": [sheet_name],
            "created": True,
            "overwritten": existed,
        }

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

    def list_images(self, path: str, sheet: str | None = None) -> dict[str, Any]:
        workbook_path = self._resolve_workbook_path(path)
        image_refs = self._load_image_refs(workbook_path, sheet=sheet)
        return {
            "file": workbook_path.relative_to(self._workspace_root).as_posix(),
            "images": image_refs,
        }

    def extract_image(
        self,
        path: str,
        *,
        sheet: str,
        image_index: int,
        output: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if image_index <= 0:
            raise ValueError("image_index must be greater than zero.")
        workbook_path = self._resolve_workbook_path(path)
        image_refs = self._load_image_refs(workbook_path, sheet=sheet)
        image_ref = next(
            (
                item
                for item in image_refs
                if item["sheet"] == sheet and item["image_index"] == image_index
            ),
            None,
        )
        if image_ref is None:
            raise KeyError(f"Image index {image_index} was not found on sheet '{sheet}'.")

        extension = str(image_ref["extension"])
        if output is None:
            safe_sheet = re.sub(r"[^A-Za-z0-9_.-]+", "_", sheet).strip("_") or "sheet"
            output_path = self._resolve_path_inside_workspace(
                ".orchestra_artifacts/excel_images/"
                f"{workbook_path.stem}_{safe_sheet}_{image_index}{extension}"
            )
        else:
            output_path = self._resolve_path_inside_workspace(output)
        if output_path.exists() and not overwrite:
            raise FileExistsError(
                f"Output file '{output_path.relative_to(self._workspace_root).as_posix()}' "
                "already exists. Set overwrite=True to replace it."
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with ZipFile(workbook_path) as archive:
            media_bytes = archive.read(str(image_ref["zip_path"]))
        output_path.write_bytes(media_bytes)

        return {
            "file": workbook_path.relative_to(self._workspace_root).as_posix(),
            "sheet": sheet,
            "image_index": image_index,
            "anchor_cell": image_ref["anchor_cell"],
            "output": output_path.relative_to(self._workspace_root).as_posix(),
        }

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
        self._validate_workbook_extension(relative_path, workbook_path)
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
    def _normalize_cell_ref(cell_ref: str) -> str:
        normalized = cell_ref.strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", normalized):
            raise ValueError(f"Invalid Excel cell reference: '{cell_ref}'.")
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

    @staticmethod
    def _new_workbook() -> Any:
        try:
            from openpyxl import Workbook  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "Missing dependency 'openpyxl'. Install optional extras with "
                "`pip install \"orchestra-agent[mcp-server]\"`."
            ) from exc
        return Workbook()

    @staticmethod
    def _validate_workbook_extension(path_label: str, workbook_path: Path) -> None:
        if workbook_path.suffix.lower() != ".xlsx":
            raise ValueError(f"Workbook '{path_label}' must be an .xlsx file.")

    def _load_image_refs(
        self,
        workbook_path: Path,
        *,
        sheet: str | None = None,
    ) -> list[dict[str, Any]]:
        with ZipFile(workbook_path) as archive:
            sheet_targets = self._sheet_targets(archive)
            if sheet is not None and sheet not in sheet_targets:
                raise KeyError(f"Worksheet '{sheet}' does not exist.")

            image_refs: list[dict[str, Any]] = []
            for sheet_name, sheet_xml_path in sheet_targets.items():
                if sheet is not None and sheet_name != sheet:
                    continue
                image_refs.extend(
                    self._sheet_image_refs(
                        archive=archive,
                        sheet_name=sheet_name,
                        sheet_xml_path=sheet_xml_path,
                    )
                )
        return image_refs

    def _sheet_targets(self, archive: ZipFile) -> dict[str, str]:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_rels = self._relationships(archive, "xl/_rels/workbook.xml.rels")

        sheet_targets: dict[str, str] = {}
        sheet_path = f"{{{self._main_ns}}}sheets/{{{self._main_ns}}}sheet"
        for sheet_elem in workbook_root.findall(sheet_path):
            sheet_name = sheet_elem.attrib.get("name")
            rel_id = sheet_elem.attrib.get(f"{{{self._rel_ns}}}id")
            if not isinstance(sheet_name, str) or not isinstance(rel_id, str):
                continue
            target = workbook_rels.get(rel_id)
            if target is None:
                continue
            sheet_targets[sheet_name] = self._resolve_zip_target("xl/workbook.xml", target)
        return sheet_targets

    def _sheet_image_refs(
        self,
        *,
        archive: ZipFile,
        sheet_name: str,
        sheet_xml_path: str,
    ) -> list[dict[str, Any]]:
        if sheet_xml_path not in archive.namelist():
            return []
        sheet_root = ET.fromstring(archive.read(sheet_xml_path))
        sheet_rels = self._relationships(archive, self._rels_path(sheet_xml_path))

        drawing_rel_ids = [
            elem.attrib.get(f"{{{self._rel_ns}}}id")
            for elem in sheet_root.findall(f"{{{self._main_ns}}}drawing")
        ]
        image_refs: list[dict[str, Any]] = []
        image_index = 0
        for drawing_rel_id in drawing_rel_ids:
            if not isinstance(drawing_rel_id, str):
                continue
            drawing_target = sheet_rels.get(drawing_rel_id)
            if drawing_target is None:
                continue
            drawing_xml_path = self._resolve_zip_target(sheet_xml_path, drawing_target)
            image_refs_from_drawing = self._drawing_image_refs(
                archive=archive,
                sheet_name=sheet_name,
                drawing_xml_path=drawing_xml_path,
                image_index_offset=image_index,
            )
            image_refs.extend(image_refs_from_drawing)
            image_index += len(image_refs_from_drawing)
        return image_refs

    def _drawing_image_refs(
        self,
        *,
        archive: ZipFile,
        sheet_name: str,
        drawing_xml_path: str,
        image_index_offset: int,
    ) -> list[dict[str, Any]]:
        if drawing_xml_path not in archive.namelist():
            return []
        drawing_root = ET.fromstring(archive.read(drawing_xml_path))
        drawing_rels = self._relationships(archive, self._rels_path(drawing_xml_path))

        image_refs: list[dict[str, Any]] = []
        for anchor in drawing_root:
            embed_id = self._embedded_image_rel_id(anchor)
            if embed_id is None:
                continue
            media_target = drawing_rels.get(embed_id)
            if media_target is None:
                continue
            media_path = self._resolve_zip_target(drawing_xml_path, media_target)
            image_refs.append(
                {
                    "sheet": sheet_name,
                    "image_index": image_index_offset + len(image_refs) + 1,
                    "anchor_cell": self._anchor_cell(anchor),
                    "extension": Path(media_path).suffix.lower(),
                    "zip_path": media_path,
                }
            )
        return image_refs

    def _relationships(self, archive: ZipFile, rels_path: str) -> dict[str, str]:
        if rels_path not in archive.namelist():
            return {}
        rels_root = ET.fromstring(archive.read(rels_path))
        relationships: dict[str, str] = {}
        for rel_elem in rels_root.findall(f"{{{self._pkg_rel_ns}}}Relationship"):
            rel_id = rel_elem.attrib.get("Id")
            target = rel_elem.attrib.get("Target")
            if isinstance(rel_id, str) and isinstance(target, str):
                relationships[rel_id] = target
        return relationships

    @staticmethod
    def _rels_path(xml_path: str) -> str:
        xml_file = PurePosixPath(xml_path)
        return str(xml_file.parent / "_rels" / f"{xml_file.name}.rels")

    @staticmethod
    def _resolve_zip_target(base_xml_path: str, target: str) -> str:
        target_path = PurePosixPath(target)
        if target.startswith("/"):
            return str(PurePosixPath(target.lstrip("/")))

        normalized_parts: list[str] = []
        for part in (PurePosixPath(base_xml_path).parent / target_path).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if normalized_parts:
                    normalized_parts.pop()
                continue
            normalized_parts.append(part)
        return str(PurePosixPath(*normalized_parts))

    def _embedded_image_rel_id(self, anchor: ET.Element) -> str | None:
        blip = anchor.find(f".//{{{self._a_ns}}}blip")
        if blip is None:
            return None
        embed_id = blip.attrib.get(f"{{{self._rel_ns}}}embed")
        return embed_id if isinstance(embed_id, str) else None

    def _anchor_cell(self, anchor: ET.Element) -> str | None:
        from_elem = anchor.find(f"{{{self._xdr_ns}}}from")
        if from_elem is None:
            return None
        row_elem = from_elem.find(f"{{{self._xdr_ns}}}row")
        col_elem = from_elem.find(f"{{{self._xdr_ns}}}col")
        if row_elem is None or col_elem is None:
            return None
        row_index = int(row_elem.text or "0") + 1
        col_index = int(col_elem.text or "0")
        return f"{self._column_letters(col_index)}{row_index}"

    @staticmethod
    def _column_letters(index: int) -> str:
        if index < 0:
            raise ValueError("Excel column index must be zero or greater.")
        value = index + 1
        letters = ""
        while value > 0:
            value, remainder = divmod(value - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    @staticmethod
    def _matches_text(
        text: str,
        pattern: str,
        *,
        case_sensitive: bool,
        regex: bool,
        exact: bool,
    ) -> bool:
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            return re.search(pattern, text, flags=flags) is not None
        left = text if case_sensitive else text.lower()
        right = pattern if case_sensitive else pattern.lower()
        if exact:
            return left == right
        return right in left
