from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService

openpyxl = pytest.importorskip("openpyxl")
PILImage = pytest.importorskip("PIL.Image")


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


def test_excel_service_creates_new_workbook_file(workspace_dir: Path) -> None:
    service = ExcelWorkspaceService(workspace_dir)

    created = service.create_file("output/new_report.xlsx", sheet="Data")

    assert created["file"] == "output/new_report.xlsx"
    assert created["sheet_names"] == ["Data"]
    assert created["created"] is True
    assert created["overwritten"] is False
    assert (workspace_dir / "output" / "new_report.xlsx").is_file()

    opened = service.open_file("output/new_report.xlsx")
    assert opened["sheet_names"] == ["Data"]


def test_excel_service_reads_specific_cells_and_greps_content(workspace_dir: Path) -> None:
    workbook_path = workspace_dir / "instructions.xlsx"

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "Keyword"
    sheet["B2"] = "Review this program"
    sheet["C3"] = "会いう"
    workbook.save(workbook_path)
    workbook.close()

    service = ExcelWorkspaceService(workspace_dir)

    cells = service.read_cells("instructions.xlsx", "Sheet1", ["A1", "B2", "C3"])
    assert cells["cells"] == {
        "A1": "Keyword",
        "B2": "Review this program",
        "C3": "会いう",
    }

    grep = service.grep_cells("instructions.xlsx", "会いう")
    assert grep["matches"] == [{"sheet": "Sheet1", "cell": "C3", "value": "会いう"}]
    assert grep["truncated"] is False


def test_excel_service_lists_and_extracts_embedded_images(workspace_dir: Path) -> None:
    workbook_path = workspace_dir / "images.xlsx"
    image_path = workspace_dir / "sample.png"
    output_path = workspace_dir / "artifacts" / "extracted.png"

    PILImage.new("RGB", (12, 12), color="red").save(image_path)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    embedded = openpyxl.drawing.image.Image(str(image_path))
    sheet.add_image(embedded, "B2")
    workbook.save(workbook_path)
    workbook.close()

    service = ExcelWorkspaceService(workspace_dir)

    listed = service.list_images("images.xlsx")
    assert listed["images"] == [
        {
            "sheet": "Sheet1",
            "image_index": 1,
            "anchor_cell": "B2",
            "extension": ".png",
            "zip_path": "xl/media/image1.png",
        }
    ]

    extracted = service.extract_image(
        "images.xlsx",
        sheet="Sheet1",
        image_index=1,
        output="artifacts/extracted.png",
    )
    assert extracted["output"] == "artifacts/extracted.png"
    assert output_path.is_file()
    assert output_path.read_bytes() == image_path.read_bytes()


def test_excel_service_blocks_paths_outside_workspace(workspace_dir: Path) -> None:
    service = ExcelWorkspaceService(workspace_dir)

    with pytest.raises(PermissionError):
        service.open_file("../outside.xlsx")
