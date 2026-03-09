from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture()
def workspace_dir() -> Iterator[Path]:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_excel_service_reads_sums_writes_and_exports(workspace_dir: Path) -> None:
    workbook_path = workspace_dir / "sales.xlsx"
    output_path = workspace_dir / "summary.xlsx"

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["C1"] = "Amount"
    sheet["C2"] = 10
    sheet["C3"] = "20"
    sheet["C4"] = 30
    workbook.save(workbook_path)
    workbook.close()

    service = ExcelWorkspaceService(workspace_dir)

    opened = service.open_file("sales.xlsx")
    assert opened["sheet_names"] == ["Sheet1"]

    rows = service.read_sheet("sales.xlsx", "Sheet1")
    assert rows["row_count"] == 4
    assert rows["rows"][1]["C"] == 10

    total = service.calculate_sum("sales.xlsx", "Sheet1", "C")
    assert total["total"] == 60
    assert total["counted_cells"] == 3

    created = service.create_sheet("sales.xlsx", "Summary")
    assert created["created"] is True

    written = service.write_cells(
        "sales.xlsx",
        "Summary",
        {"A1": "Column", "B1": "Total", "A2": "C", "B2": 60},
    )
    assert written["written_cells"] == 4

    exported = service.save_file("sales.xlsx", "summary.xlsx")
    assert exported["output"] == "summary.xlsx"
    assert output_path.is_file()

    exported_workbook = openpyxl.load_workbook(output_path)
    try:
        assert exported_workbook["Summary"]["B2"].value == 60
    finally:
        exported_workbook.close()


def test_excel_service_blocks_paths_outside_workspace(workspace_dir: Path) -> None:
    service = ExcelWorkspaceService(workspace_dir)

    with pytest.raises(PermissionError):
        service.open_file("../outside.xlsx")
